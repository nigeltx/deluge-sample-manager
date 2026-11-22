"""Rename, move, relink and trash — changes to files already on the card (spec 5.3–5.6, 7.2–7.4).

A pending EditBatch holds simple requests (file moves, folder moves, relinks, trash). plan() turns
all pending batches into concrete file moves plus, for every kit / synth / song that refers to a
moved sample, the exact byte ranges to change. XML is never re-serialised: only the bytes of each
path value change, so every other byte stays exactly as the Deluge wrote it.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from dsm.card.card import DELUGE_OWN_SAMPLE_DIRS, SOURCE_DIRS, TRASH_DIR, Card
from dsm.importer.apply import Journal, backup_card
from dsm.index.index import CardIndex
from dsm.index.xmlrefs import scan_card, scan_xml

QUOTES = {'"': "&quot;"}


def xml_value(path: str) -> str:
    """A card path as it must appear inside a fileName attribute or element."""
    return escape(path, QUOTES)


@dataclass
class EditBatch:
    title: str
    moves: list[tuple[str, str]] = field(default_factory=list)      # card files: old rel -> new rel
    relinks: list[tuple[str, str]] = field(default_factory=list)    # missing path as written -> existing card rel
    trash: list[str] = field(default_factory=list)                  # unused card files to DSM_TRASH
    kind: str = "edit"

    @property
    def active(self):
        return self.moves + self.relinks + [(t, "") for t in self.trash]

    def summary(self) -> str:
        return self.title


def folder_move(card: Card, old_dir: str, new_dir: str, title: str | None = None) -> EditBatch:
    """Every file under old_dir moves to the same place under new_dir."""
    pre = old_dir.rstrip("/").lower() + "/"
    moves = [(rel, new_dir.rstrip("/") + "/" + rel[len(pre):]) for k, rel in sorted(card.files.items())
             if k.startswith(pre)]
    return EditBatch(title or f"Move folder {old_dir} → {new_dir} ({len(moves)} files)", moves=moves)


def factory_candidates(card: Card) -> tuple[list[str], list[str]]:
    """(folders, loose files) directly under SAMPLES that are not FREE/FACTORY/PAID or the Deluge's own."""
    known = {d.upper() for d in SOURCE_DIRS + DELUGE_OWN_SAMPLE_DIRS}
    tops = sorted({d.split("/")[1] for d in card.dirs if d.upper().startswith("SAMPLES/") and d.count("/") >= 1},
                  key=str.lower)
    folders = [t for t in tops if t.upper() not in known]
    loose = sorted(v for k, v in card.files.items() if k.startswith("samples/") and k.count("/") == 1)
    return folders, loose


def factory_move(card: Card, folders: list[str], loose: list[str]) -> EditBatch:
    moves = []
    for f in folders:
        moves += folder_move(card, f"SAMPLES/{f}", f"SAMPLES/FACTORY/{f}").moves
    moves += [(rel, "SAMPLES/FACTORY/" + rel.split("/", 1)[1]) for rel in loose]
    return EditBatch(f"Move the factory library under SAMPLES/FACTORY ({len(folders)} folder{'s' if len(folders) != 1 else ''}, "
                     f"{len(moves)} file{'s' if len(moves) != 1 else ''})",
                     moves=moves)


# ------------------------------------------------------------------ plan
@dataclass
class XmlChange:
    xml: str
    size: int
    mtime: float
    edits: list[tuple[int, int, str, str]] = field(default_factory=list)   # start, end, old raw, new raw


@dataclass
class EditPlan:
    moves: list[tuple[str, str]] = field(default_factory=list)
    trash: list[str] = field(default_factory=list)
    xml: list[XmlChange] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ref_count(self):
        return sum(len(x.edits) for x in self.xml)

    def lines(self, limit_per_xml: int = 3) -> list[str]:
        out = []
        for o, n in self.moves:
            out.append(f"move   {o}\n    →  {n}")
        for t in self.trash:
            out.append(f"trash  {t}  (to {TRASH_DIR}, recoverable)")
        if self.xml:
            out.append(f"\n{len(self.xml)} preset and song files change, {self.ref_count} references:")
        for x in self.xml:
            out.append(f"  {x.xml}")
            for s, e, old, new in x.edits[:limit_per_xml]:
                out.append(f"      − {old}\n      + {new}")
            if len(x.edits) > limit_per_xml:
                out.append(f"      … {len(x.edits) - limit_per_xml} more in this file")
        return out


def plan(batches: list[EditBatch], idx: CardIndex) -> EditPlan:
    card = idx.card
    p = EditPlan()
    moves: dict[str, str] = {}
    for b in batches:
        for old, new in b.moves:
            if old.lower() not in card.files:
                p.problems.append(f"Not on the card any more: {old}")
                continue
            if old.lower() in moves:
                p.problems.append(f"Moved twice in the pending list: {old}")
                continue
            moves[old.lower()] = new
    relinks = {old.lower(): new for b in batches for old, new in b.relinks}
    trash = [t for b in batches for t in b.trash]

    # destinations must be free and distinct (a destination may be a file that is itself moving away)
    leaving = set(moves)
    seen = {}
    for old_l, new in moves.items():
        nl = new.lower()
        if nl in seen:
            p.problems.append(f"Two files would both become {new}")
        seen[nl] = old_l
        if nl in card.files and nl not in leaving and nl != old_l:
            p.problems.append(f"Already on the card: {new}")
        if not nl.startswith("samples/"):
            p.problems.append(f"Samples must stay inside SAMPLES: {new}")
    for t in trash:
        if idx.uses(t):
            p.problems.append(f"Still used by {len(idx.uses(t))} presets or songs, so it can't go to the trash: {t}")
    for old_l, new in relinks.items():
        if new.lower() not in card.files and new.lower() not in seen:
            p.problems.append(f"Relink target is not on the card: {new}")

    # final card path of every file
    def final(rel_l: str) -> str | None:
        return moves.get(rel_l)

    by_xml: dict[str, XmlChange] = {}
    for doc in idx.docs:
        for r in doc.refs:
            target = None
            if r.resolved and r.resolved.lower() in moves:
                target = moves[r.resolved.lower()]
            elif not r.resolved and r.path.lower() in relinks:
                tgt = relinks[r.path.lower()]
                target = final(tgt.lower()) or tgt
            if target is None:
                continue
            new_raw = xml_value(target)
            if new_raw == r.raw:
                continue
            xc = by_xml.get(doc.rel)
            if not xc:
                xc = by_xml[doc.rel] = XmlChange(doc.rel, doc.size, doc.mtime)
            xc.edits.append((r.start, r.end, r.raw, new_raw))
    p.moves = [(card.files[o], n) for o, n in moves.items() if card.files[o] != n]
    p.trash = trash
    p.xml = sorted(by_xml.values(), key=lambda x: x.xml.lower())
    return p


# ------------------------------------------------------------------ apply
def _sha1(card: Card, rel: str) -> str:
    return hashlib.sha1(card.read_bytes(rel)).hexdigest()


def _rewrite(data: bytes, edits) -> bytes:
    out = data
    for s, e, old, new in sorted(edits, key=lambda t: t[0], reverse=True):
        if out[s:e].decode("utf-8", "replace") != old:
            raise RuntimeError("The preset file changed since it was read")
        out = out[:s] + new.encode("utf-8") + out[e:]
    return out


def _move(card: Card, old: str, new: str):
    card.move(old, new)


def _prune_empty(card: Card, rels):
    """Remove folders left empty by a move (only empty ones, deepest first)."""
    dirs = set()
    for rel in rels:
        parts = rel.split("/")[:-1]
        for i in range(len(parts), 1, -1):
            dirs.add("/".join(parts[:i]))
    for d in sorted(dirs, key=lambda x: -x.count("/")):
        card.rmdir_if_empty(d)


def apply(p: EditPlan, batches: list[EditBatch], card: Card, mapping, backup_root: str, full_backup: bool,
          dry_run: bool = False, progress=None) -> Journal:
    j = Journal(id=time.strftime("%Y%m%d-%H%M%S"), when=time.strftime("%Y-%m-%d %H:%M"), card_root=card.root,
                card_serial=card.facts.serial, card_label=card.facts.label, dry_run=dry_run, kind="edit",
                summary=[b.summary() for b in batches])
    if p.problems:
        raise RuntimeError("Can't apply:\n" + "\n".join(p.problems[:20]))
    if not card.is_usb and not os.path.isdir(card.root):
        raise RuntimeError("The card is no longer there.")
    for x in p.xml:                                     # nothing changed since the card was read?
        size, mtime = card.stat(x.xml, fresh=True) if card.is_usb else card.stat(x.xml)
        if size != x.size or abs(mtime - x.mtime) > 0.01:
            raise RuntimeError(f"{x.xml} changed since the card was read. Press Refresh and try again.")
    if dry_run:
        j.log = p.lines(limit_per_xml=1000)
        return j

    j.backup = backup_card(card, backup_root, full_backup)
    xml_dir = os.path.join(j.backup, "changed presets and songs")
    for x in p.xml:
        dst = os.path.join(xml_dir, *x.xml.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(card.read_bytes(x.xml))
        j.xml_backups.append([x.xml, dst])
    j.save()
    total = len(p.moves) + len(p.trash) + len(p.xml)
    n = 0
    try:
        for old, new in p.moves:
            _move(card, old, new)
            j.moved.append([old, new])
            n += 1
            if progress and n % 10 == 0:
                progress(n, total, f"Moving {old}")
        for t in p.trash:
            dest = f"{TRASH_DIR}/{j.id}/{t}"
            _move(card, t, dest)
            j.trashed.append([t, dest])
            n += 1
        j.save()
        for x in p.xml:
            data = card.read_bytes(x.xml)
            new = _rewrite(data, x.edits)
            card.write_bytes(x.xml, new)
            j.xml_after[x.xml] = hashlib.sha1(new).hexdigest()
            n += 1
            if progress:
                progress(n, total, f"Updating {x.xml}")
        j.save()
        _prune_empty(card, [o for o, _ in j.moved] + [t for t, _ in j.trashed])
        # verify: every changed file is still well-formed (if it was) and every rewritten path resolves
        if not card.is_usb:          # over USB the card's file list is kept up to date as files move
            card.scan()
        for x in p.xml:
            doc = scan_card(card, x.xml, fresh=True)
            new_paths = {html_unescape(e[3]).lower() for e in x.edits}
            bad = [r.path for r in doc.refs if r.path.lower() in new_paths and not card.resolve(r.path)]
            if bad:
                raise RuntimeError(f"Check failed in {x.xml}: {bad[0]} doesn't resolve")
            if doc.error and "Not well-formed" in doc.error:
                before = scan_xml(dict(j.xml_backups)[x.xml], x.xml)
                if not before.error:
                    raise RuntimeError(f"Check failed: {x.xml} is no longer well-formed")
        for old, new in j.moved:
            if mapping.move_card(old, new):
                j.mapping_moves.append([old, new])
        mapping.save()
        j.log = [f"moved {o} → {nw}" for o, nw in j.moved] + [f"trashed {t}" for t, _ in j.trashed] + \
                [f"updated {x.xml} ({len(x.edits)} references)" for x in p.xml]
        j.save()
    except Exception:
        _rollback(j, card)
        j.log.append("FAILED — the card was put back as it was")
        j.undone = True
        j.save()
        raise
    if progress:
        progress(total, total, "")
    return j


def html_unescape(s: str) -> str:
    import html
    return html.unescape(s)


def _rollback(j: Journal, card: Card) -> list[str]:
    problems = []
    for rel, bak in j.xml_backups:
        try:
            with open(bak, "rb") as f:
                card.write_bytes(rel, f.read())
        except (OSError, RuntimeError) as e:
            problems.append(f"Couldn't restore {rel}: {e}")
    for t, dest in reversed(j.trashed):
        try:
            _move(card, dest, t)
        except Exception as e:  # noqa: BLE001
            problems.append(f"Couldn't bring back {t}: {e}")
    for old, new in reversed(j.moved):
        try:
            _move(card, new, old)
        except Exception as e:  # noqa: BLE001
            problems.append(f"Couldn't move back {new}: {e}")
    _prune_empty(card, [n for _, n in j.moved] + [d for _, d in j.trashed])
    return problems


def undo(j: Journal, card: Card, mapping) -> list[str]:
    """Put back an edit: preset and song files restored from the backup, files moved back.
    A preset changed on the Deluge since is not overwritten; it is reported instead."""
    if j.card_serial and card.facts.serial and j.card_serial != card.facts.serial:
        return [f"That change was made on another card ({j.card_label or j.card_serial})."]
    problems = []
    keep = []
    for rel, bak in j.xml_backups:
        if card.exists(rel) and j.xml_after.get(rel) and _sha1(card, rel) != j.xml_after[rel]:
            problems.append(f"{rel} was saved again after this change, so it was left as it is now.")
            keep.append(rel)
    restore = Journal(**{**j.__dict__, "xml_backups": [x for x in j.xml_backups if x[0] not in keep]})
    problems += _rollback(restore, card)
    for old, new in j.mapping_moves:
        mapping.move_card(new, old)
    mapping.save()
    j.undone = True
    j.log.append(f"undone {time.strftime('%Y-%m-%d %H:%M')}")
    j.save()
    return problems
