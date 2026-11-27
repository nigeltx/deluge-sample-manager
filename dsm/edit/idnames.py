"""Rename card samples to their library IDs (0.11): 'Fun_Hat.wav' → 'HO0046_FUN_HAT.WAV' in the same folder.

It is an ordinary M4 edit (EditBatch moves), so every kit, synth and song is rewritten byte-exactly, it goes through
the pending tray, and History can undo it. Factory samples (locked) and the Deluge's own folders are left alone, and
so are card files that have no copy in the library (no ID yet).
"""
from __future__ import annotations

from dsm.edit.duplicates import deluge_own
from dsm.edit.edits import EditBatch
from dsm.edit.factory import is_factory_card
from dsm.importer.naming import card_name, unique


def rename_batch(card, rels, card_audio: dict, labels: dict) -> tuple[EditBatch | None, str]:
    moves, taken = [], set(card.files)
    no_id = locked = dup = 0
    for rel in sorted(rels, key=str.lower):
        if is_factory_card(rel) or deluge_own(rel):
            locked += 1
            continue
        label = labels.get(card_audio.get(rel.lower()))
        if not label:
            no_id += 1
            continue
        folder, old = rel.rsplit("/", 1)
        new = f"{folder}/{card_name(label + '.wav')}"
        if new.lower() == rel.lower():
            continue
        if new.lower() in taken:
            if card_audio.get(new.lower()) == card_audio.get(rel.lower()):
                dup += 1                       # the same sound is already there under its ID: a duplicate
                continue
            new = unique(new, taken)
        taken.discard(rel.lower())
        taken.add(new.lower())
        moves.append((rel, new))
    notes = []
    if locked:
        notes.append(f"{locked:,} factory or Deluge-made samples keep their names")
    if no_id:
        notes.append(f"{no_id:,} have no copy in the library, so no ID yet")
    if dup:
        notes.append(f"{dup:,} are duplicates of a sample already named after its ID (Duplicates on card can tidy them)")
    note = "; ".join(notes) + ("." if notes else "")
    if not moves:
        return None, note
    return EditBatch(f"Rename {len(moves):,} sample{'s' if len(moves) != 1 else ''} to their IDs", moves=moves), note
