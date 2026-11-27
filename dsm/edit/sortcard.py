"""Sort the card by type (0.14): every sample on the card moves to the folder of its type, as TARGETS › DELUGE shows
it — SAMPLES/DRUMS/KICK/…, SAMPLES/BASS/… (naming.TYPE_DIRS).

It is an ordinary M4 edit (EditBatch moves): every kit, synth and song is rewritten byte-exactly, it goes through
the pending tray, History can undo it, and folders left empty are removed. A sample that has an ID is named after
it on the way (HO0046_FUN_HAT.WAV); others keep their names. Factory samples (locked in SAMPLES/FACTORY), the
Deluge's own folders (CLIPS, RECORD, RESAMPLE, EXPORTS) and the card trash stay where they are.
"""
from __future__ import annotations

from dsm.edit.duplicates import deluge_own
from dsm.edit.edits import EditBatch
from dsm.edit.factory import is_factory_card
from dsm.importer.naming import card_name, type_dir, unique


def sort_batch(card, card_types: dict, card_audio: dict, labels: dict) -> tuple[EditBatch | None, str]:
    """card_types: {card rel: its type list}; card_audio: {lower rel: audio}; labels: {audio: ID label}."""
    moves, taken = [], set(card.files)
    stays = already = dup = 0
    for rel in sorted(card_types, key=str.lower):
        if is_factory_card(rel) or deluge_own(rel) or rel.upper().startswith("SAMPLES/DSM_TRASH/"):
            stays += 1
            continue
        a = card_audio.get(rel.lower())
        label = labels.get(a) if a else None
        name = card_name(label + ".wav") if label else rel.rsplit("/", 1)[-1]
        new = f"{type_dir(card_types[rel])}/{name}"
        if new.lower() == rel.lower():
            already += 1
            continue
        if new.lower() in taken:
            if a and card_audio.get(new.lower()) == a:
                dup += 1                        # the same sound is there already: a duplicate, left for Tidy
                continue
            new = unique(new, taken)
        taken.discard(rel.lower())
        taken.add(new.lower())
        moves.append((rel, new))
    notes = []
    if stays:
        notes.append(f"{stays:,} factory or Deluge-made samples stay where they are")
    if already:
        notes.append(f"{already:,} are in their type's folder already")
    if dup:
        notes.append(f"{dup:,} are duplicates of a sample already in place (Duplicates on the card can tidy them)")
    note = "; ".join(notes) + ("." if notes else "")
    if not moves:
        return None, note
    return EditBatch(f"Sort {len(moves):,} sample{'s' if len(moves) != 1 else ''} on the card by type",
                     moves=moves), note
