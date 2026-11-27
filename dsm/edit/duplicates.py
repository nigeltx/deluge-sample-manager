"""Duplicates: the same audio in more than one file (spec v2.2, 0.10).

Same audio means the same identity (sources.identity): the same channels, bit depth, rate and audio data, whatever
the file is called and whatever else is in its header.

Library: the index already holds every copy's identity, so the groups come straight from db.duplicate_groups().
The library is never changed; the list is there to look at.

Card: finding them must read the files. To keep that quick (and possible over USB) only files whose header facts
match another file's (length in frames, rate, channels, bits) are read in full. For each group the app picks a copy
to keep — a factory copy if there is one (factory samples are locked), else the copy used by the most kits, synths
and songs, else the shortest path — and tidy() makes an edit that points every use of the other copies at the kept
one and moves the other copies to the card trash. Factory copies, and copies in the Deluge's own folders (CLIPS,
RECORD, RESAMPLE …), are never moved or trashed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from dsm.card.card import DELUGE_OWN_SAMPLE_DIRS, TRASH_DIR, Card
from dsm.edit.edits import EditBatch
from dsm.edit.factory import is_factory_card
from dsm.library.sources import identity


@dataclass
class CardGroup:
    files: list[str]                      # card rels with the same audio, the one to keep first
    keep: str
    size: int = 0                         # bytes of one copy
    uses: dict = field(default_factory=dict)   # rel -> number of references

    @property
    def extras(self) -> list[str]:
        """The copies tidy() would remove: every copy but the kept one, except factory copies (locked)."""
        return [f for f in self.files if f != self.keep and not locked(f)]


def deluge_own(rel: str) -> bool:
    """In one of the Deluge's own folders (CLIPS, RECORD, RESAMPLE, EXPORTS …): the Deluge's working files."""
    parts = rel.upper().split("/")
    return len(parts) > 2 and parts[0] == "SAMPLES" and parts[1] in DELUGE_OWN_SAMPLE_DIRS


def locked(rel: str) -> bool:
    """Copies tidy() never removes: factory samples, and the Deluge's own recordings and clips."""
    return is_factory_card(rel) or deluge_own(rel)


def card_groups(card: Card, idx=None, progress=None) -> list[CardGroup]:
    files = [f for f in card.sample_files() if not f.upper().startswith(f"SAMPLES/{TRASH_DIR}/")]
    by_key = defaultdict(list)
    for k, rel in enumerate(files):
        if progress and k % 50 == 0:
            progress(k, len(files) * 2, f"Reading headers: {rel}")
        try:
            info = card.inspect(rel)
        except OSError:
            continue
        if info.error or not info.frames:
            continue
        by_key[(info.frames, info.rate, info.channels, info.bits)].append(rel)
    candidates = [rels for rels in by_key.values() if len(rels) > 1]
    todo = sum(len(r) for r in candidates)
    done = 0
    groups = []
    for rels in candidates:
        by_id = defaultdict(list)
        for rel in rels:
            done += 1
            if progress:
                progress(len(files) + done * len(files) // max(1, todo), len(files) * 2, f"Comparing: {rel}")
            try:
                by_id[identity(card.read_bytes(rel))].append(rel)
            except OSError:
                continue
        for same in by_id.values():
            if len(same) > 1:
                groups.append(_group(card, idx, same))
    if progress:
        progress(1, 1, "")
    return sorted(groups, key=lambda g: g.keep.lower())


def _group(card: Card, idx, rels: list[str]) -> CardGroup:
    uses = {r: len(idx.uses(r)) if idx else 0 for r in rels}
    keep = best_copy(rels, uses)
    try:
        size = card.stat(rels[0])[0]
    except (OSError, FileNotFoundError):
        size = 0
    return CardGroup(files=[keep] + sorted((r for r in rels if r != keep), key=str.lower), keep=keep, size=size,
                     uses=uses)


def best_copy(rels: list[str], uses: dict) -> str:
    return sorted(rels, key=lambda r: (not is_factory_card(r), -uses.get(r, 0), deluge_own(r), len(r), r.lower()))[0]


def tidy(groups: list[CardGroup]) -> EditBatch | None:
    """One edit for these groups: point every use of the extra copies at the kept one, extras to the card trash."""
    repoints, trash = [], []
    for g in groups:
        for extra in g.extras:
            repoints.append((extra, g.keep))
            trash.append(extra)
    if not trash:
        return None
    n = len(trash)
    return EditBatch(f"Remove {n} duplicate sample{'s' if n != 1 else ''} from the card "
                     f"(their kits, synths and songs use the copy kept)", repoints=repoints, trash=trash)
