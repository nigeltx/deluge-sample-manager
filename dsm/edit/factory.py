"""The factory record: which samples came with the Deluge, where they were, and an untouched copy of each.

Made when the factory library is moved under SAMPLES/FACTORY. Each entry holds the sample's original
path, its path now, its size and date, and (once copied) a SHA-1 checksum and the path of the copy in the
library's Factory › Deluge folder. The record is saved in the library (_Sample Manager/factory-record.json,
(_DSM before 0.9), synced with
Drive) and on the card (DSM/FACTORY_RECORD.JSON, outside SAMPLES so the Deluge's browser never shows it).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field

LIB_NAME = os.path.join("_Sample Manager", "factory-record.json")
OLD_LIB_NAME = os.path.join("_DSM", "factory-record.json")
LIB_PREFIX = "Factory/Deluge/"                # where the untouched copies live in the library (spec v2.2 2.1)
CARD_NAME = "DSM/FACTORY_RECORD.JSON"
PREFIX = "SAMPLES/FACTORY/"


@dataclass
class Entry:
    original: str            # where the factory put it, e.g. SAMPLES/DRUMS/KICK/808 KICK.WAV
    card: str                # where it is now, e.g. SAMPLES/FACTORY/DRUMS/KICK/808 KICK.WAV
    size: int
    mtime: float
    sha1: str = ""           # filled when the copy is made (needs the whole file)
    library: str = ""        # copy in the library, e.g. Sources/Factory/Deluge/DRUMS/KICK/808 KICK.WAV


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


# Factory samples the record knows by their place on the open card. Since 0.15 Sort the card by type can move them
# out of SAMPLES/FACTORY (into SAMPLES/DRUMS/KICK …), so a path alone no longer says a sample is factory; the
# window calls remember() whenever it loads the record.
_KNOWN: set[str] = set()


def remember(rec: "FactoryRecord | None") -> None:
    """Make is_factory_card() know the record's samples wherever they are on the card."""
    _KNOWN.clear()
    if rec and not rec.undone:
        _KNOWN.update(e.card.lower() for e in rec.entries)


def is_factory_card(rel: str) -> bool:
    """A factory sample: under SAMPLES/FACTORY, or in the factory record wherever it now sits. They are locked:
    only Initialize Factory Samples and Sort the card by type may move them."""
    return rel.upper().startswith(PREFIX) or rel.lower() in _KNOWN


def home(e: "Entry") -> str:
    """Where Initialize put a factory sample: SAMPLES/FACTORY/ + its original place. Its copy in the library follows
    this path, wherever the sample is on the card now."""
    o = e.original.replace("\\", "/")
    return PREFIX + o[len("SAMPLES/"):] if o.upper().startswith("SAMPLES/") else e.card


def follow_moves(rec: "FactoryRecord", moves) -> list[list[str]]:
    """Point the record's entries at their new places after moves [(old, new)]. Returns the moves that touched it."""
    by = {e.card.lower(): e for e in rec.entries}
    done = []
    for old, new in moves:
        e = by.get(old.lower())
        if e is not None:
            e.card = new
            done.append([old, new])
    return done


def is_factory_library(rel: str) -> bool:
    return rel.replace("\\", "/").upper().startswith("FACTORY/")


def origin(rel: str, on_card: bool = True) -> str:
    """Factory, Free, Paid, Deluge (the Deluge's own folders) or Other, from a card or library path."""
    parts = rel.replace("\\", "/").split("/")
    top = (parts[1] if on_card and len(parts) > 2 else parts[0] if not on_card else "").upper()
    if on_card and is_factory_card(rel):
        return "Factory"
    if top in ("FACTORY", "FREE", "PAID"):
        return top.title()
    if on_card and top in ("CLIPS", "RECORD", "RESAMPLE", "EXPORTS"):
        return "Deluge"
    return "Other"


def record_from_existing(card, journal_id: str = "") -> "FactoryRecord":
    """For a card whose factory samples are already under SAMPLES/FACTORY but has no record yet: their original
    place was the same path without FACTORY/."""
    moved = [("SAMPLES/" + v[len(PREFIX):], v) for k, v in sorted(card.files.items()) if k.startswith(PREFIX.lower())]
    return make(card, moved, journal_id)


def library_copy_path(card_rel: str) -> str:
    """SAMPLES/FACTORY/DRUMS/X.WAV -> Factory/Deluge/DRUMS/X.WAV (relative to the library root)."""
    rest = card_rel[len(PREFIX):] if card_rel.upper().startswith(PREFIX) else card_rel
    return LIB_PREFIX + rest


def missing_from_library(rec: FactoryRecord, library_root: str) -> list[Entry]:
    """Factory samples with no copy in Factory › Deluge (spec 2.1: the folder must be complete).
    Matched by path, without regard to case, as FAT32 and Windows do."""
    have = set()
    base = library_root
    for part in LIB_PREFIX.strip("/").split("/"):          # Factory/Deluge, matched without regard to case
        try:
            names = {n.lower(): n for n in os.listdir(base)}
        except OSError:
            names = {}
        if part.lower() not in names:
            return list(rec.entries)
        base = os.path.join(base, names[part.lower()])
    for dp, _dns, fns in os.walk(base):
        for fn in fns:
            have.add(os.path.relpath(os.path.join(dp, fn), library_root).replace(os.sep, "/").lower())
    return [e for e in rec.entries if library_copy_path(home(e)).lower() not in have]


def is_audio(rel: str) -> bool:
    from dsm.audio.inspect import AUDIO_EXTS
    return os.path.splitext(rel)[1].lower() in AUDIO_EXTS


def drop_non_audio(rec: FactoryRecord) -> list[str]:
    """Take files that aren't samples out of the record — such as REAPER's .reapeaks files that sat beside the
    factory claps (0.15.2). Initialize recorded every file in the factory folders; the Deluge reads only audio.
    Returns the card paths dropped."""
    gone = [e.card for e in rec.entries if not is_audio(e.card)]
    if gone:
        rec.entries = [e for e in rec.entries if is_audio(e.card)]
    return gone


def make(card, moved: list[tuple[str, str]], journal_id: str) -> FactoryRecord:
    rec = FactoryRecord(made=time.strftime("%Y-%m-%d %H:%M"), journal_id=journal_id, card_label=card.facts.label)
    for old, new in moved:
        if not new.upper().startswith(PREFIX) or not is_audio(new):
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
        for name in (LIB_NAME, OLD_LIB_NAME):
            p = os.path.join(library_root, name)
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
    """Copy each factory sample, untouched, into Factory › Deluge, with a checksum. A copy already there
    at the same size is kept (Sources are never overwritten) and its checksum recorded; a copy made now is checked
    against the record's checksum when there is one. Returns counts and problems. Saves the record as it goes."""
    from dsm.importer.mapping import Link
    out = {"copied": 0, "skipped": 0, "problems": []}
    n = len(rec.entries)
    done_bytes = 0
    for k, e in enumerate(rec.entries):
        dst_rel = library_copy_path(home(e))
        dst = os.path.join(library_root, *dst_rel.split("/"))
        found = _existing(library_root, dst_rel)
        if found:
            if os.path.getsize(found) == e.size or not e.size:
                if not (e.sha1 and e.library):
                    e.sha1 = e.sha1 or _sha1(found)
                    e.library = os.path.relpath(found, library_root).replace(os.sep, "/")
                out["skipped"] += 1
                done_bytes += e.size
                continue
            out["problems"].append(f"In Sources at another size, left as it is: {dst_rel}")
            continue
        if not card.exists(e.card):
            out["problems"].append(f"Not on the card: {e.card}")
            continue
        if progress:
            progress(k, n, f"Copying factory sample {k + 1} of {n}: {e.card.rsplit('/', 1)[-1]}")
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
            if e.sha1 and h.hexdigest() != e.sha1:
                os.remove(tmp)
                out["problems"].append(f"Not the factory file (checksum differs): {e.card}")
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


def _existing(root: str, rel: str) -> str | None:
    """The file at rel below root, found without regard to case, or None."""
    cur = root
    for part in rel.split("/"):
        try:
            names = {n.lower(): n for n in os.listdir(cur)}
        except OSError:
            return None
        if part.lower() not in names:
            return None
        cur = os.path.join(cur, names[part.lower()])
    return cur if os.path.isfile(cur) else None


def _sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


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
