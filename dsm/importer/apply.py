"""Write planned imports to the card: backup, atomic write, verify, journal, undo (spec 7.2–7.4).

Nothing existing on the card is changed by an import — it only adds files. Undo moves
the added files into DSM_TRASH at the card root (never deletes).
"""
from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field

from dsm.card.card import TRASH_DIR, Card
from dsm.importer.convert import convert
from dsm.importer.mapping import Link, Mapping
from dsm.importer.plan import ImportBatch, bytes_needed
from dsm.settings import app_dir

KEEP_BACKUPS = 30


@dataclass
class Journal:
    id: str
    when: str
    card_root: str
    card_serial: str
    card_label: str
    summary: list[str] = field(default_factory=list)
    created_card: list[str] = field(default_factory=list)
    created_library: list[str] = field(default_factory=list)
    backup: str = ""
    undone: bool = False
    dry_run: bool = False
    log: list[str] = field(default_factory=list)
    kind: str = "import"                                          # import or edit
    moved: list = field(default_factory=list)                     # edit: [old_rel, new_rel] file moves
    trashed: list = field(default_factory=list)                   # edit: [rel, trash_rel]
    xml_backups: list = field(default_factory=list)               # edit: [xml_rel, backup file path]
    xml_after: dict = field(default_factory=dict)                 # edit: xml_rel -> sha1 written
    mapping_moves: list = field(default_factory=list)             # edit: [old_card_rel, new_card_rel]

    def save(self):
        d = journal_dir()
        with open(os.path.join(d, f"{self.id}.json"), "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=1)


def journal_dir() -> str:
    d = os.path.join(app_dir(), "journal")
    os.makedirs(d, exist_ok=True)
    return d


def history() -> list[Journal]:
    out = []
    for fn in sorted(os.listdir(journal_dir()), reverse=True):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(journal_dir(), fn), encoding="utf-8") as f:
                    d = json.load(f)
                out.append(Journal(**{k: v for k, v in d.items() if k in Journal.__dataclass_fields__}))
            except (OSError, ValueError, TypeError):
                pass
    return out


def backup_card(card: Card, backup_root: str, full_xml: bool) -> str:
    """Manifest of every file on the card, plus (when full_xml) every XML file zipped."""
    stamp = time.strftime("%Y-%m-%d %H%M%S")
    label = (card.facts.label or "CARD").replace(os.sep, "_")
    d = os.path.join(backup_root, f"{label} {stamp}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.txt"), "w", encoding="utf-8") as f:
        for rel in sorted(card.files.values(), key=str.lower):
            try:
                st = os.stat(card.abs(rel))
                f.write(f"{rel}\t{st.st_size}\t{int(st.st_mtime)}\n")
            except OSError:
                f.write(f"{rel}\t?\t?\n")
    if full_xml:
        with zipfile.ZipFile(os.path.join(d, "presets-and-songs.zip"), "w", zipfile.ZIP_DEFLATED) as z:
            for rel in card.xml_files():
                z.write(card.abs(rel), rel)
    _prune(backup_root)
    return d


def _prune(root: str):
    try:
        dirs = sorted((e for e in os.scandir(root) if e.is_dir()), key=lambda e: e.stat().st_mtime)
        for e in dirs[:-KEEP_BACKUPS]:
            shutil.rmtree(e.path, ignore_errors=True)   # our own old backups only
    except OSError:
        pass


def apply(batches: list[ImportBatch], card: Card, library_root: str | None, mapping: Mapping, backup_root: str,
          full_backup: bool, dry_run: bool = False, progress=None) -> Journal:
    j = Journal(id=time.strftime("%Y%m%d-%H%M%S"), when=time.strftime("%Y-%m-%d %H:%M"), card_root=card.root,
                card_serial=card.facts.serial, card_label=card.facts.label, dry_run=dry_run,
                summary=[b.summary() for b in batches])
    items = [(b, i) for b in batches for i in b.active]
    # ---- plan check: card still there, destinations still free, room on the card
    if not os.path.isdir(card.root):
        raise RuntimeError("The card is no longer there.")
    for _b, i in items:
        if os.path.exists(card.abs(i.card_rel)):
            raise RuntimeError(f"{i.card_rel} already exists on the card. Refresh and import again.")
        if not os.path.exists(i.src):
            raise RuntimeError(f"The original is gone: {i.src}")
    need = bytes_needed(batches)
    free = shutil.disk_usage(card.root).free
    if need > free * 0.98:
        raise RuntimeError(f"Not enough room on the card: about {need // 1_000_000} MB needed, {free // 1_000_000} MB free.")
    if dry_run:
        for b, i in items:
            j.log.append(f"would write {i.card_rel}  ({i.note})")
            if i.copy_original:
                j.log.append(f"would copy original into the library: {i.library_rel}")
        return j
    # ---- back up
    j.backup = backup_card(card, backup_root, full_backup)
    j.save()
    # ---- write
    try:
        for n, (b, i) in enumerate(items):
            if progress:
                progress(n, len(items), f"Writing {i.card_rel}")
            dest = card.abs(i.card_rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = os.path.join(os.path.dirname(dest), ".dsmtmp-" + os.path.basename(dest))
            out = convert(i.src, tmp, b.profile)
            if not out.deluge_plays or out.frames == 0:
                os.remove(tmp)
                raise RuntimeError(f"Check failed after writing {i.card_rel}: " + "; ".join(out.problems))
            with open(tmp, "rb+") as f:
                os.fsync(f.fileno())
            os.replace(tmp, dest)
            j.created_card.append(i.card_rel)
            if i.copy_original and library_root:
                ldest = os.path.join(library_root, *i.library_rel.split("/"))
                if not os.path.exists(ldest):
                    os.makedirs(os.path.dirname(ldest), exist_ok=True)
                    ltmp = ldest + ".dsmtmp"
                    shutil.copy2(i.src, ltmp)
                    os.replace(ltmp, ldest)
                    j.created_library.append(i.library_rel)
            mapping.add(Link(card=i.card_rel, library=i.library_rel, card_serial=card.facts.serial,
                             converted=i.note, profile=b.profile.name))
            j.log.append(f"wrote {i.card_rel}")
            j.save()
        mapping.save()
    except Exception:
        # roll back: remove only what this run created (they did not exist before)
        for rel in j.created_card:
            try:
                os.remove(card.abs(rel))
            except OSError:
                pass
        for rel in j.created_library:
            try:
                os.remove(os.path.join(library_root, *rel.split("/")))
            except OSError:
                pass
        for rel in j.created_card:
            mapping.remove_card(rel, card.facts.serial)
        j.log.append("FAILED — everything this run wrote was removed again")
        j.created_card, j.created_library, j.undone = [], [], True
        j.save()
        raise
    if progress:
        progress(len(items), len(items), "")
    j.save()
    return j


def undo(j: Journal, card: Card, library_root: str | None, mapping: Mapping) -> list[str]:
    """Move what an Apply added into the trash folders. Returns problems (empty = all fine)."""
    problems = []
    if j.card_serial and card.facts.serial and j.card_serial != card.facts.serial:
        return [f"That change was made on another card ({j.card_label or j.card_serial})."]
    for rel in j.created_card:
        src = card.abs(rel)
        if not os.path.exists(src):
            problems.append(f"Already gone: {rel}")
            continue
        dst = os.path.join(card.root, TRASH_DIR, j.id, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        mapping.remove_card(rel, j.card_serial)
    if library_root:
        for rel in j.created_library:
            src = os.path.join(library_root, *rel.split("/"))
            if os.path.exists(src):
                dst = os.path.join(library_root, "_DSM", "trash", j.id, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
    mapping.save()
    j.undone = True
    j.log.append(f"undone {time.strftime('%Y-%m-%d %H:%M')}")
    j.save()
    return problems
