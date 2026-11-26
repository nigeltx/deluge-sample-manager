"""Get from device (spec 4.5): read a device and keep, in Sources › User, the samples that are in no source.

For the Deluge: every audio file under SAMPLES/ except SAMPLES/FACTORY (locked factory samples) and CLIPS/TEMP
(unsaved takes). A file the mapping already links to a library sample is skipped without reading it; every other
file is read and its audio identity compared with Sources, so exact copies of anything in Sources — factory
samples included — are never kept twice. What is left is offered with a tick each; the ticked ones are copied
as they are into Sources/User/<date> Deluge read/<path below SAMPLES>.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from dsm.edit import factory
from dsm.library import layout
from dsm.library import sources as src


@dataclass
class Found:
    card_rel: str
    size: int
    audio: str
    where: str          # RECORD, RESAMPLE, CLIPS, EXPORTS or "by hand"
    keep: bool = True


def candidates(card) -> list[str]:
    out = []
    for rel in card.files.values():
        up = rel.upper()
        if not up.startswith("SAMPLES/") or not src.is_audio(rel):
            continue
        if factory.is_factory_card(rel) or up.startswith("SAMPLES/CLIPS/TEMP/"):
            continue
        out.append(rel)
    return sorted(out, key=str.lower)


def where(rel: str) -> str:
    top = rel.split("/")[1].upper() if rel.count("/") > 1 else ""
    return top if top in ("RECORD", "RESAMPLE", "CLIPS", "EXPORTS") else "put there by hand"


def find_new(card, db, mapping, progress=None) -> list[Found]:
    """Samples on the card whose audio is in no source."""
    have = db.source_audios()
    linked = {x.card.lower() for x in mapping.links if x.library}
    rels = [r for r in candidates(card) if r.lower() not in linked]
    out = []
    for k, rel in enumerate(rels):
        if progress:
            progress(k, len(rels), f"Reading the Deluge: {rel}")
        try:
            data = card.read_bytes(rel)
        except (OSError, KeyError, RuntimeError):
            continue
        a = src.identity(data)
        if a in have:
            continue
        have.add(a)                     # two copies on the card are offered once
        out.append(Found(rel, len(data), a, where(rel)))
    if progress:
        progress(len(rels), len(rels), "")
    return out


def folder_name(device: str = "Deluge") -> str:
    return f"{time.strftime('%Y-%m-%d')} {device} read"


def keep(card, root: str, found: list[Found], device: str = "Deluge", progress=None) -> dict:
    """Copy the ticked samples into Sources/User/<date> <device> read/…. Never overwrites."""
    base = os.path.join(root, layout.find_child(root, layout.SOURCES) or layout.SOURCES, "User", folder_name(device))
    out = {"kept": 0, "problems": []}
    todo = [f for f in found if f.keep]
    for k, f in enumerate(todo):
        if progress:
            progress(k, len(todo), f"Keeping {f.card_rel}")
        dst = os.path.join(base, *f.card_rel.split("/")[1:])
        if os.path.exists(dst):
            out["problems"].append(f"Already there, left as it is: {dst}")
            continue
        try:
            data = card.read_bytes(f.card_rel)
            if src.identity(data) != f.audio:
                out["problems"].append(f"Changed on the card since it was read: {f.card_rel}")
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".smtmp"
            with open(tmp, "wb") as fo:
                fo.write(data)
            os.replace(tmp, dst)
            out["kept"] += 1
        except (OSError, KeyError, RuntimeError) as e:
            out["problems"].append(f"{f.card_rel}: {e}")
    if progress:
        progress(len(todo), len(todo), "")
    return out
