"""Reorganize library… — finish the move to Sources / Samples / Backups once (spec 2.6, Table 2.3).

plan() looks and changes nothing; apply() does the steps in order and reports each one. Running it again does
only what is still left, so it is safe to run twice.

Steps
  folders    make Sources\\Factory\\Deluge and \\Syntakt, Samples, Backups\\Deluge and \\Syntakt, _Sample Manager
  rename     Sources\\Factory\\Digitakt II → Deluge, if it is still there (they are the Deluge's factory samples)
  appfolder  move _DSM's files into _Sample Manager; the empty _DSM goes on the Studio Cleanup list
  mapping    every library path in mapping.json that no longer exists is pointed at the file now in Sources with
             the same name and size (or the same name, when only one has it)
  record     the factory record's library paths point into Sources\\Factory\\Deluge
  factory    factory samples missing from Sources\\Factory\\Deluge are copied from the card, checked against
             their SHA-1 (needs the card; without it the step says so and waits)
  backups    the app's old backups on the PC move to Backups\\Deluge, and the history follows them
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field

from dsm.edit import factory
from dsm.importer.apply import journal_dir
from dsm.library import layout
from dsm.library import sources as src
from dsm.settings import app_dir as pc_app_dir


@dataclass
class Step:
    kind: str
    text: str
    data: dict = field(default_factory=dict)


def _folders(root):
    return [os.path.join(root, *p) for p in
            ((layout.SOURCES,), (layout.SOURCES, "Factory"), (layout.SOURCES, "Factory", "Deluge"),
             (layout.SOURCES, "Factory", "Syntakt"), (layout.SAMPLES,), (layout.BACKUPS,),
             (layout.BACKUPS, "Deluge"), (layout.BACKUPS, "Syntakt"), (layout.APP_FOLDER,))]


def _real(root, *parts):
    """The path with each part matched without regard to case, or None."""
    cur = root
    for p in parts:
        n = layout.find_child(cur, p)
        if not n:
            return None
        cur = os.path.join(cur, n)
    return cur


def _source_files_by_name(root) -> dict[str, list[str]]:
    """lower-case file name → [rel] for every audio file in Sources (not inside zips)."""
    out: dict[str, list[str]] = {}
    sdir = _real(root, layout.SOURCES)
    if sdir:
        for rel, _full in src.walk_files(sdir, root):
            if not src.is_zip(rel):
                out.setdefault(os.path.basename(rel).lower(), []).append(rel)
    return out


def _mapping_moves(root, mapping, by_name) -> tuple[list, int]:
    moves, lost = [], 0
    for link in mapping.links:
        if not link.library or os.path.exists(os.path.join(root, *link.library.split(src.SEP)[0].split("/"))):
            continue
        cands = by_name.get(os.path.basename(link.library).lower(), [])
        if len(cands) == 1:
            moves.append((link.library, cands[0]))
        else:
            lost += 1
    return moves, lost


def _old_backups() -> list[str]:
    d = os.path.join(pc_app_dir(), "Backups")
    try:
        return sorted(e.path for e in os.scandir(d) if e.is_dir())
    except OSError:
        return []


def plan(root: str, mapping=None, rec=None, card=None) -> list[Step]:
    steps = []
    missing = [p for p in _folders(root) if not _real(root, *os.path.relpath(p, root).split(os.sep))]
    old = _real(root, layout.SOURCES, "Factory", "Digitakt II")
    if old and not _real(root, layout.SOURCES, "Factory", "Deluge"):
        steps.append(Step("rename", "Rename Sources › Factory › Digitakt II to Deluge — they are the Deluge's factory "
                                    "samples", {"src": old, "dst": os.path.join(os.path.dirname(old), "Deluge")}))
        missing = [p for p in missing if not p.lower().endswith(os.path.join("factory", "deluge"))]
    if missing:
        steps.append(Step("folders", "Make " + ", ".join(os.path.relpath(p, root).replace(os.sep, " › ")
                                                          for p in missing), {"paths": missing}))
    dsm = _real(root, layout.OLD_APP_FOLDER)
    if dsm and os.listdir(dsm):
        names = sorted(os.listdir(dsm))
        steps.append(Step("appfolder", f"Move {len(names)} item(s) from _DSM ({', '.join(names[:4])}"
                                       f"{'…' if len(names) > 4 else ''}) into _Sample Manager; _DSM goes on the "
                                       "Studio Cleanup list", {"src": dsm, "names": names}))
    by_name = None
    if mapping is not None and mapping.links:
        by_name = _source_files_by_name(root)
        moves, lost = _mapping_moves(root, mapping, by_name)
        if moves or lost:
            steps.append(Step("mapping", f"Point {len(moves)} link(s) in mapping.json at their sample's place in "
                                         f"Sources" + (f"; {lost} can't be matched and are left as they are" if lost
                                                       else ""), {"moves": moves}))
    if rec is not None:
        stale = [e for e in rec.entries if e.library and not e.library.lower().startswith(factory.LIB_PREFIX.lower())]
        if stale:
            steps.append(Step("record", f"Point {len(stale)} factory-record entries at Sources › Factory › Deluge"))
        gone = factory.missing_from_library(rec, root)
        if gone:
            if card is not None:
                steps.append(Step("factory", f"Copy the {len(gone)} factory samples missing from Sources › Factory › "
                                             f"Deluge from the card's SAMPLES/FACTORY, checked against their SHA-1 "
                                             f"(the record lists {len(rec.entries)})", {"n": len(gone)}))
            else:
                steps.append(Step("factory-wait", f"{len(gone)} of the {len(rec.entries)} factory samples are missing "
                                                  "from Sources › Factory › Deluge. Open the Deluge's card (or USB) "
                                                  "and run Reorganize again to copy them", {"n": len(gone)}))
    olds = _old_backups()
    if olds:
        steps.append(Step("backups", f"Move {len(olds)} old backup(s) from this PC into Backups › Deluge; History "
                                     "follows them", {"paths": olds}))
    return steps


def apply(root: str, steps: list[Step], mapping=None, rec=None, card=None, settings=None, progress=None,
          log=lambda s: None) -> dict:
    out = {"done": [], "problems": [], "cleanup": []}
    for st in steps:
        try:
            if st.kind == "folders":
                for p in st.data["paths"]:
                    os.makedirs(p, exist_ok=True)
            elif st.kind == "rename":
                os.rename(st.data["src"], st.data["dst"])
            elif st.kind == "appfolder":
                dst = layout.app_dir(root)
                os.makedirs(dst, exist_ok=True)
                for n in st.data["names"]:
                    s, d = os.path.join(st.data["src"], n), os.path.join(dst, n)
                    if os.path.exists(d):
                        out["problems"].append(f"_Sample Manager already has {n}; the one in _DSM was left")
                        continue
                    shutil.move(s, d)
                if not os.listdir(st.data["src"]):
                    out["cleanup"].append(st.data["src"])
                if mapping is not None:
                    mapping.path = os.path.join(dst, "mapping.json")
            elif st.kind == "mapping" and mapping is not None:
                moves = dict((a.lower(), b) for a, b in st.data["moves"])
                for link in mapping.links:
                    if link.library and link.library.lower() in moves:
                        link.library = moves[link.library.lower()]
                mapping.save()
            elif st.kind == "record" and rec is not None:
                for e in rec.entries:
                    if e.library and not e.library.lower().startswith(factory.LIB_PREFIX.lower()):
                        e.library = factory.library_copy_path(e.card)
                factory.save(rec, root, None)
            elif st.kind == "factory" and rec is not None and card is not None:
                r = factory.copy_to_library(rec, card, root, mapping, progress=progress)
                out["problems"] += r["problems"]
                log(f"copied {r['copied']} factory samples; {r['skipped']} were already there")
            elif st.kind == "backups":
                dst = layout.backups_dir(root, "Deluge")
                os.makedirs(dst, exist_ok=True)
                moved = {}
                for p in st.data["paths"]:
                    d = os.path.join(dst, os.path.basename(p))
                    if os.path.exists(d):
                        continue
                    shutil.move(p, d)
                    moved[p] = d
                _follow_backups(moved)
                if settings is not None:
                    settings["backup_root"] = dst
                    settings.save()
            out["done"].append(st.text)
            log(st.text)
        except OSError as e:
            out["problems"].append(f"{st.text}: {e.strerror or e}")
    if out["cleanup"]:
        if not layout.add_to_cleanup(root, out["cleanup"]):
            out["problems"].append("The Studio Cleanup list wasn't found, so these empty folders were left: "
                                   + ", ".join(out["cleanup"]))
    return out


def _follow_backups(moved: dict[str, str]):
    """Rewrite the backup paths in the history so Undo still finds them."""
    if not moved:
        return
    d = journal_dir()
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(d, fn)
        try:
            with open(p, encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError):
            continue
        changed = False
        for old, new in moved.items():
            b = j.get("backup") or ""
            if b == old or b.startswith(old + os.sep):
                j["backup"] = new + b[len(old):]
                changed = True
            for xb in j.get("xml_backups") or []:
                if len(xb) > 1 and isinstance(xb[1], str) and xb[1].startswith(old + os.sep):
                    xb[1] = new + xb[1][len(old):]
                    changed = True
        if changed:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(j, f, indent=1)


def needed(root: str) -> bool:
    """True when the library is not yet in the v2 layout (offer Reorganize at start)."""
    return bool(root and os.path.isdir(root) and (
        not _real(root, layout.SAMPLES) or not _real(root, layout.APP_FOLDER) or _real(root, layout.OLD_APP_FOLDER)
        or not _real(root, layout.BACKUPS)))
