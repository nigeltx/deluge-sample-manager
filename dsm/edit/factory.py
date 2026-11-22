"""The factory record: which samples came with the Deluge, where they were, and an untouched copy of each.

Made when the factory library is moved under SAMPLES/FACTORY. Each entry holds the sample's original
path, its path now, its size and date, and (once copied) a SHA-1 checksum and the path of the copy in the
library's FACTORY folder. The record is saved in the library (_DSM/factory-record.json, synced with
Drive) and on the card (DSM/FACTORY_RECORD.JSON, outside SAMPLES so the Deluge's browser never shows it).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field

LIB_NAME = os.path.join("_DSM", "factory-record.json")
CARD_NAME = "DSM/FACTORY_RECORD.JSON"
PREFIX = "SAMPLES/FACTORY/"


@dataclass
class Entry:
    original: str            # where the factory put it, e.g. SAMPLES/DRUMS/KICK/808 KICK.WAV
    card: str                # where it is now, e.g. SAMPLES/FACTORY/DRUMS/KICK/808 KICK.WAV
    size: int
    mtime: float
    sha1: str = ""           # filled when the copy is made (needs the whole file)
    library: str = ""        # copy in the library, e.g. FACTORY/DRUMS/KICK/808 KICK.WAV


@dataclass
class FactoryRecord:
    made: str = ""
    journal_id: str = ""
    card_label: str = ""
    undone: bool = False
    entries: list[Entry] = field(default_factory=list)

    def by_card(self) -> dict[str, Entry]:
        return {e.card.lower(): e for e in self.entries}

    @property
    def copied(self) -> int:
        return sum(1 for e in self.entries if e.sha1 and e.library)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "FactoryRecord":
        d = json.loads(text)
        d["entries"] = [Entry(**e) for e in d.get("entries", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def library_copy_path(card_rel: str) -> str:
    """SAMPLES/FACTORY/DRUMS/X.WAV -> FACTORY/DRUMS/X.WAV (relative to the library root)."""
    return "FACTORY/" + card_rel[len(PREFIX):] if card_rel.upper().startswith(PREFIX) else "FACTORY/" + card_rel


def make(card, moved: list[tuple[str, str]], journal_id: str) -> FactoryRecord:
    rec = FactoryRecord(made=time.strftime("%Y-%m-%d %H:%M"), journal_id=journal_id, card_label=card.facts.label)
    for old, new in moved:
        if not new.upper().startswith(PREFIX):
            continue
        try:
            size, mtime = card.stat(new)
        except (OSError, FileNotFoundError, KeyError):
            size, mtime = 0, 0.0
        rec.entries.append(Entry(original=old, card=new, size=size, mtime=mtime))
    return rec


def load(library_root: str | None, card=None) -> FactoryRecord | None:
    """The library's copy if there is one, else the card's."""
    if library_root:
        p = os.path.join(library_root, LIB_NAME)
        try:
            with open(p, encoding="utf-8") as f:
                return FactoryRecord.from_json(f.read())
        except (OSError, ValueError):
            pass
    if card is not None and card.exists(CARD_NAME):
        try:
            return FactoryRecord.from_json(card.read_bytes(CARD_NAME, fresh=True).decode("utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return None


def save(rec: FactoryRecord, library_root: str | None, card=None) -> list[str]:
    """Write both copies. Returns problems (empty = both saved)."""
    text = rec.to_json()
    problems = []
    if library_root and os.path.isdir(library_root):
        p = os.path.join(library_root, LIB_NAME)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    else:
        problems.append("The library folder isn't reachable, so the record was saved on the card only.")
    if card is not None:
        try:
            card.write_bytes(CARD_NAME, text.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            problems.append(f"Couldn't save the record on the card: {e}")
    return problems


def copy_to_library(rec: FactoryRecord, card, library_root: str, mapping=None, progress=None) -> dict:
    """Copy each factory sample, untouched, into LIBRARY/FACTORY/…, with a checksum. Files already copied and
    matching are skipped. Returns counts and problems. Saves the record as it goes."""
    from dsm.importer.mapping import Link
    out = {"copied": 0, "skipped": 0, "problems": []}
    n = len(rec.entries)
    done_bytes = 0
    for k, e in enumerate(rec.entries):
        dst_rel = library_copy_path(e.card)
        dst = os.path.join(library_root, *dst_rel.split("/"))
        if e.sha1 and e.library and os.path.exists(dst) and os.path.getsize(dst) == e.size:
            out["skipped"] += 1
            done_bytes += e.size
            continue
        if not card.exists(e.card):
            out["problems"].append(f"Not on the card: {e.card}")
            continue
        if progress:
            progress(k, n, f"Copying factory sample {k + 1} of {n}: {e.card[len(PREFIX):]}")
        try:
            local = card.local_path(e.card)
            h = hashlib.sha1()
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".dsmtmp"
            if local:
                with open(local, "rb") as fi, open(tmp, "wb") as fo:
                    while True:
                        b = fi.read(1 << 20)
                        if not b:
                            break
                        h.update(b)
                        fo.write(b)
            else:
                data = card.read_bytes(e.card)
                h.update(data)
                with open(tmp, "wb") as fo:
                    fo.write(data)
            if os.path.getsize(tmp) != e.size and e.size:
                os.remove(tmp)
                out["problems"].append(f"Size changed while copying: {e.card}")
                continue
            os.replace(tmp, dst)
            e.sha1, e.library = h.hexdigest(), dst_rel
            if mapping is not None:
                mapping.add(Link(card=e.card, library=dst_rel, card_serial=card.facts.serial,
                                 converted="factory original, copied untouched", profile="factory"))
            out["copied"] += 1
            done_bytes += e.size
        except OSError as ex:
            out["problems"].append(f"{e.card}: {ex}")
        if out["copied"] and out["copied"] % 50 == 0:
            save(rec, library_root, None)
            if mapping is not None:
                mapping.save()
    save(rec, library_root, card)
    if mapping is not None:
        mapping.save()
    if progress:
        progress(n, n, "")
    return out


def check(rec: FactoryRecord, card) -> list[tuple[str, str]]:
    """[(kind, text)]: factory samples that are missing from the card or no longer the same size."""
    out = []
    for e in rec.entries:
        if not card.exists(e.card):
            out.append(("missing", f"{e.card} (originally {e.original})"))
            continue
        try:
            size, _ = card.stat(e.card)
        except (OSError, FileNotFoundError, KeyError):
            continue
        if e.size and size != e.size:
            out.append(("changed", f"{e.card} is {size:,} bytes; the factory file was {e.size:,}"))
    return out
