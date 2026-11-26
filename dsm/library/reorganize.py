"""Reorganize library… — move to the one-folder library once (spec v2.2 2.6, Table 2.2).

plan() looks and changes nothing; apply() does the steps in order and reports each one. Running it again does
only what is still left, so it is safe to run twice. It works from either older layout:

  0.8    Studio\\Samples holds Sources\\… (or Factory/Free/Paid at the top), _DSM, maybe Factory\\Digitakt II
  0.9.0  Studio\\Samples holds Sources\\Factory, Paid, Free, User; Samples\\<type> (copies); Backups; _Sample Manager

Steps
  rename     Factory\\Digitakt II → Deluge, if it is still there (they are the Deluge's factory samples)
  flatten    move each folder in Sources up one level (Sources\\Paid → Paid); the database follows, so nothing is
             read again; the empty Sources goes on the Studio Cleanup list
  folders    make Factory\\Deluge and \\Syntakt, Backups\\Deluge and \\Syntakt, _Sample Manager
  appfolder  move _DSM's files into _Sample Manager; the empty _DSM goes on the Studio Cleanup list
  samples    0.9.0's Samples folder held copies: a file in it whose audio is nowhere else in the library is moved to
             User\\Added by hand first; then Samples goes on the Studio Cleanup list (Clean up Studio.cmd moves it to
             Archive). Nothing is deleted.
  mapping    every library path in mapping.json loses its Sources/ prefix; one that still does not exist is pointed
             at the file with the same name, when only one has it
  record     the factory record's library paths point into Factory\\Deluge
  factory    factory samples missing from Factory\\Deluge are copied from the card, checked against their SHA-1
             (needs the card; without it the step says so and waits)
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

HAND_FOLDER = "Added by hand"


@dataclass
class Step:
    kind: str
    text: str
    data: dict = field(default_factory=dict)


def _real(root, *parts):
    """The path with each part matched without regard to case, or None."""
    cur = root
    for p in parts:
        n = layout.find_child(cur, p)
        if not n:
            return None
        cur = os.path.join(cur, n)
    return cur


def _wanted_folders(root):
    return [os.path.join(root, *p) for p in
            (("Factory",), ("Factory", "Deluge"), ("Factory", "Syntakt"), ("Paid",), ("Free",), ("User",),
             (layout.BACKUPS,), (layout.BACKUPS, "Deluge"), (layout.BACKUPS, "Syntakt"), (layout.APP_FOLDER,))]


def _files_by_name(root) -> dict[str, list[str]]:
    """lower-case file name → [rel] for every audio file in the library (not inside zips), after flattening."""
    out: dict[str, list[str]] = {}
    for _t, d in layout.source_dirs(root):
        for rel, _full in src.walk_files(d, root):
            if not src.is_zip(rel):
                out.setdefault(os.path.basename(rel).lower(), []).append(rel)
    return out


def _strip(rel: str) -> str:
    return rel[8:] if rel.lower().startswith("sources/") else rel


def _old_backups() -> list[str]:
    d = os.path.join(pc_app_dir(), "Backups")
    try:
        return sorted(e.path for e in os.scandir(d) if e.is_dir())
    except OSError:
        return []


def plan(root: str, mapping=None, rec=None, card=None, known_audio: set | None = None) -> list[Step]:
    """known_audio: every audio identity in the library (from the database), to tell copies in 0.9.0's Samples
    from files you dropped there yourself."""
    steps = []
    sources = _real(root, layout.OLD_SOURCES)
    fac_parent = _real(sources, "Factory") if sources else _real(root, "Factory")
    dig = _real(fac_parent, "Digitakt II") if fac_parent else None
    if dig and not _real(fac_parent, "Deluge"):
        steps.append(Step("rename", "Rename Factory › Digitakt II to Deluge — they are the Deluge's factory samples",
                          {"src": dig, "dst": os.path.join(os.path.dirname(dig), "Deluge")}))
    if sources:
        kids = sorted(os.listdir(sources))
        clash = [k for k in kids if layout.find_child(root, k)]
        steps.append(Step("flatten", f"Move {', '.join(kids) or 'nothing'} up out of Sources, so Studio › Samples holds "
                                     "them directly; the empty Sources goes on the Studio Cleanup list"
                                     + (f" ({', '.join(clash)} already there: merged)" if clash else ""),
                          {"src": sources, "names": kids}))
    def there(p):
        parts = os.path.relpath(p, root).split(os.sep)
        return _real(root, *parts) or (sources and _real(sources, *parts))
    missing = [p for p in _wanted_folders(root) if not there(p)]
    if dig:                                            # the rename makes Factory › Deluge
        missing = [p for p in missing if not p.lower().endswith(os.path.join("factory", "deluge"))]
    if missing:
        steps.append(Step("folders", "Make " + ", ".join(os.path.relpath(p, root).replace(os.sep, " › ")
                                                          for p in missing), {"paths": missing}))
    dsm = _real(root, layout.OLD_APP_FOLDER)
    if dsm:
        names = sorted(os.listdir(dsm))
        steps.append(Step("appfolder", (f"Move {len(names)} item(s) from _DSM ({', '.join(names[:4])}"
                                        f"{'…' if len(names) > 4 else ''}) into _Sample Manager; " if names else "")
                          + "_DSM goes on the Studio Cleanup list", {"src": dsm, "names": names}))
    smp = _real(root, layout.OLD_SAMPLES)
    if smp:
        files = [(rel, full) for rel, full in src.walk_files(smp, root) if not src.is_zip(rel)]
        hand = []
        if known_audio:
            for rel, full in files:
                try:
                    with open(full, "rb") as f:
                        if src.identity(f.read()) not in known_audio:
                            hand.append(full)
                except OSError:
                    continue
        steps.append(Step("samples", f"Put the Samples folder ({len(files):,} cop{'y' if len(files) == 1 else 'ies'} made by 0.9.0's Rebuild) on the "
                                     "Studio Cleanup list; Clean up Studio.cmd moves it to Archive, where you can delete it"
                          + (f". First, {len(hand)} file(s) in it that are nowhere else move to User › {HAND_FOLDER}"
                             if hand else "") + ("" if known_audio else " (not checked for files you added yourself: "
                                                                        "scan first)"),
                          {"src": smp, "hand": hand}))
    if mapping is not None and mapping.links:
        by_name = None
        moves, lost = [], 0
        for link in mapping.links:
            if not link.library:
                continue
            new = _strip(link.library)
            file_part, _, inner = new.partition(src.SEP)
            found = None
            for base in ([sources] if sources else []) + [root]:
                p = _real(base, *file_part.split("/"))
                if p and os.path.isfile(p):                 # its place as spelled on disk, after any flatten
                    found = os.path.relpath(p, base).replace(os.sep, "/") + (src.SEP + inner if inner else "")
                    break
            if found:
                if found != link.library:
                    moves.append((link.library, found))
                continue
            if by_name is None:
                by_name = _files_by_name(sources or root) if sources else _files_by_name(root)
            cands = by_name.get(os.path.basename(new).lower(), [])
            if len(cands) == 1:
                moves.append((link.library, _strip(cands[0])))
            else:
                lost += 1
        if moves or lost:
            steps.append(Step("mapping", f"Point {len(moves)} link(s) in mapping.json at their sample's place in the "
                                         "library" + (f"; {lost} can't be matched and are left as they are" if lost
                                                      else ""), {"moves": moves}))
    if rec is not None:
        stale = [e for e in rec.entries if e.library and not e.library.lower().startswith(factory.LIB_PREFIX.lower())]
        if stale:
            steps.append(Step("record", f"Point {len(stale)} factory-record entries at Factory › Deluge"))
        gone = factory.missing_from_library(rec, sources if sources and _real(sources, "Factory") else root)
        if gone:
            if card is not None:
                steps.append(Step("factory", f"Copy the {len(gone)} factory samples missing from Factory › Deluge from "
                                             f"the card's SAMPLES/FACTORY, checked against their SHA-1 (the record "
                                             f"lists {len(rec.entries)})", {"n": len(gone)}))
            else:
                steps.append(Step("factory-wait", f"{len(gone)} of the {len(rec.entries)} factory samples are missing "
                                                  "from Factory › Deluge. Open the Deluge's card (or USB) and run "
                                                  "Reorganize again to copy them", {"n": len(gone)}))
    olds = _old_backups()
    if olds:
        steps.append(Step("backups", f"Move {len(olds)} old backup(s) from this PC into Backups › Deluge; History "
                                     "follows them", {"paths": olds}))
    return steps


def _merge_move(s: str, d: str, problems: list):
    """Move s to d; when d is already a folder, move s's contents in, leaving anything that clashes."""
    if not os.path.exists(d):
        os.rename(s, d)
        return
    if os.path.isdir(s) and os.path.isdir(d):
        for n in os.listdir(s):
            _merge_move(os.path.join(s, n), os.path.join(d, n), problems)
        if not os.listdir(s):
            os.rmdir(s)
        return
    problems.append(f"Both there, left as they are: {s} and {d}")


def apply(root: str, steps: list[Step], mapping=None, rec=None, card=None, settings=None, db=None, progress=None,
          log=lambda s: None) -> dict:
    out = {"done": [], "problems": [], "cleanup": []}
    for st in steps:
        try:
            if st.kind == "rename":
                os.rename(st.data["src"], st.data["dst"])
            elif st.kind == "flatten":
                srcdir = st.data["src"]
                for n in st.data["names"]:
                    _merge_move(os.path.join(srcdir, n), os.path.join(root, n), out["problems"])
                if db is not None:
                    db.flatten_paths()
                out["cleanup"].append(srcdir)
            elif st.kind == "folders":
                for p in st.data["paths"]:
                    os.makedirs(p, exist_ok=True)
            elif st.kind == "appfolder":
                dst = layout.app_dir(root)
                os.makedirs(dst, exist_ok=True)
                for n in st.data["names"]:
                    s, d = os.path.join(st.data["src"], n), os.path.join(dst, n)
                    if os.path.exists(d):
                        out["problems"].append(f"_Sample Manager already has {n}; the one in _DSM was left")
                        continue
                    shutil.move(s, d)
                out["cleanup"].append(st.data["src"])
                if mapping is not None:
                    mapping.path = os.path.join(dst, "mapping.json")
            elif st.kind == "samples":
                hand_dir = os.path.join(root, layout.find_child(root, "User") or "User", HAND_FOLDER)
                for full in st.data["hand"]:
                    os.makedirs(hand_dir, exist_ok=True)
                    d = os.path.join(hand_dir, os.path.basename(full))
                    stem, ext = os.path.splitext(d)
                    k = 2
                    while os.path.exists(d):
                        d = f"{stem} ({k}){ext}"
                        k += 1
                    shutil.move(full, d)
                out["cleanup"].append(st.data["src"])
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
            else:
                continue
            out["done"].append(st.text)
            log(st.text)
        except OSError as e:
            out["problems"].append(f"{st.text}: {e.strerror or e}")
    if out["cleanup"]:
        if not layout.add_to_cleanup(root, out["cleanup"]):
            out["problems"].append("The Studio Cleanup list wasn't found, so these old folders were left: "
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
    """True when the library is not yet in the one-folder layout (offer Reorganize at start). Old folders that are
    already on the Studio Cleanup list but not yet moved don't count."""
    if not (root and os.path.isdir(root)):
        return False
    listed = _cleanup_listed(root)
    for name in (layout.OLD_SOURCES, layout.OLD_SAMPLES, layout.OLD_APP_FOLDER):
        p = _real(root, name)
        if p and os.path.normcase(os.path.abspath(p)) not in listed:
            return True
    return not _real(root, layout.APP_FOLDER) or not _real(root, layout.BACKUPS)


def _cleanup_listed(root: str) -> set[str]:
    p = layout.cleanup_list_path(root)
    studio = os.path.dirname(os.path.normpath(root))
    try:
        with open(p, encoding="utf-8-sig") as f:
            return {os.path.normcase(os.path.abspath(os.path.join(studio, *ln.strip().split("\\"))))
                    for ln in f if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()
