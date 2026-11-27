"""Sample IDs (0.11): two letters for the type, a number, and the sample's name — e.g. "HO0046 Fun Hat".

- The ID belongs to the sound (its audio identity), so it stays the same when a file is renamed or moved, and every
  copy of the same sound shares one ID.
- Numbers count up per type and are never reused, even when a sample's type changes and it gets a new ID.
- The name is the file name of the copy that counts, tidied: no extension, "_" as spaces, no old ID in front.
"""
from __future__ import annotations

import re

from dsm.library import types as stypes

CODES = {
    "Kick": "KK", "Snare": "SN", "Clap": "CP", "Closed hat": "HC", "Open hat": "HO", "Hat": "HH", "Cymbal": "CY",
    "Tom": "TM", "Percussion": "PC", "Other drums": "DR",
    "Bass": "BS", "Synth": "SY", "Keys": "KY", "Pad": "PD", "Drone": "DN", "Vocal": "VX", "FX": "FX",
    "Instrument": "IN", stypes.UNKNOWN: "UN",
}
_OLD_ID = re.compile(r"^[A-Z]{2}\d{4,}[ _-]+")


def code_for(types: list[str]) -> str:
    """The two letters for a sample with these types (the first type counts)."""
    for t in types or []:
        if t in CODES:
            return CODES[t]
    return CODES[stypes.UNKNOWN]


def tidy_name(file_name: str) -> str:
    """'HESOS_Kick_Oneshot_222.wav' → 'HESOS Kick Oneshot 222'."""
    stem = file_name.rsplit("/", 1)[-1]
    stem = re.sub(r"\.(wav|aiff?|flac|mp3|ogg)$", "", stem, flags=re.I)
    stem = _OLD_ID.sub("", stem)
    stem = re.sub(r"[_\s]+", " ", stem).strip(" -.")
    return stem or "Sample"


def label(code: str, num: int, name: str) -> str:
    return f"{code}{num:04d} {name}"


def short(code: str, num: int) -> str:
    return f"{code}{num:04d}"
