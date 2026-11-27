"""Sort the card by type (0.14): every sample on the card moves to the folder of its type, as TARGETS › DELUGE shows
it — SAMPLES/DRUMS/KICK/…, SAMPLES/BASS/… (naming.TYPE_DIRS).

It is an ordinary M4 edit (EditBatch moves): every kit, synth and song is rewritten byte-exactly, it goes through
the pending tray, History can undo it, and folders left empty are removed. A sample that has an ID is named after
it on the way (HO0046_FUN_HAT.WAV); others keep their names. The Deluge's own folders (CLIPS, RECORD, RESAMPLE,
EXPORTS) and the card trash stay where they are.

Factory samples (0.15, Nigel: they are backed up in the library, so sort them like everything else): with
with_factory=True they move too, named after their IDs, and the factory record follows them (EditBatch.factory_sort,
edits.apply). They stay locked otherwise — factory.is_factory_card knows them by the record wherever they are.
"""
from __future__ import annotations

from dsm.edit.duplicates import deluge_own
from dsm.edit.edits import EditBatch
from dsm.edit.factory import is_factory_card
from dsm.importer.naming import card_name, type_dir, unique


def sort_batch(card, card_types: dict, card_audio: dict, labels: dict,
               with_factory: bool = False) -> tuple[EditBatch | None, str]:
    """card_types: {card rel: its type list}; card_audio: {lower rel: audio}; labels: {audio: ID label}.
    with_factory: move factory samples too (only when the card has a factory record to follow them)."""
    moves, taken = [], set(card.files)
    stays = already = dup = factory_n = 0
    for rel in sorted(card_types, key=str.lower):
        fac = is_factory_card(rel)
        if (fac and not with_factory) or deluge_own(rel) or rel.upper().startswith("SAMPLES/DSM_TRASH/"):
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
        factory_n += fac
    notes = []
    if stays:
        notes.append(f"{stays:,} {'Deluge-made' if with_factory else 'factory or Deluge-made'} samples stay where "
                     "they are")
    if already:
        notes.append(f"{already:,} are in their type's folder already")
    if dup:
        notes.append(f"{dup:,} are duplicates of a sample already in place (Duplicates on the card can tidy them)")
    note = "; ".join(notes) + ("." if notes else "")
    if not moves:
        return None, note
    title = f"Sort {len(moves):,} sample{'s' if len(moves) != 1 else ''} on the card by type"
    if factory_n:
        title += f" ({factory_n:,} factory)"
    return EditBatch(title, moves=moves, factory_sort=bool(factory_n)), note
