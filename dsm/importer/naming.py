"""Where an imported sample goes on the card, and what it is called (spec 3.6, 6.3).

Card path (0.14, as the left pane's TARGETS › DELUGE): SAMPLES/DRUMS/{DRUM TYPE}/{NAME}.WAV for drums,
SAMPLES/{TYPE}/{NAME}.WAV for the rest — e.g. SAMPLES/DRUMS/KICK/KK0212_BD_DEEP_01.WAV, SAMPLES/BASS/…
The name is the sample's ID when it has one. Factory samples keep their own layout under SAMPLES/FACTORY (locked).
Before 0.14 it was SAMPLES/{SOURCE}/{VENDOR}/{PACK}/{CATEGORY}/{SUB}/{NAME}.WAV; Sort the card by type moves old ones.
"""
from __future__ import annotations

import os
import re

SOURCES = ("FREE", "FACTORY", "PAID")
MAX_NAME = 40

# The card folder of each type list (layout.TYPE_FOLDERS), below SAMPLES. Drums are grouped as in the left pane.
TYPE_DIRS = {"Kick": "DRUMS/KICK", "Snare": "DRUMS/SNARE", "Clap": "DRUMS/CLAP", "Closed hat": "DRUMS/CLOSED HAT",
             "Open hat": "DRUMS/OPEN HAT", "Hat": "DRUMS/HATS", "Cymbal": "DRUMS/CYMBAL", "Tom": "DRUMS/TOM",
             "Percussion": "DRUMS/PERCUSSION", "Other drums": "DRUMS/OTHER", "Bass": "BASS", "Synth": "SYNTH",
             "Keys": "KEYS", "Pad": "PAD", "Drone": "DRONE", "Vocal": "VOCAL", "FX": "FX", "Instrument": "INSTRUMENT",
             "Unknown": "UNKNOWN"}
TYPE_TOPS = {v.split("/")[0] for v in TYPE_DIRS.values()}        # DRUMS, BASS, SYNTH … directly under SAMPLES


def type_dir(type_folder: str | None) -> str:
    """'Kick' → 'SAMPLES/DRUMS/KICK'; an unknown list → 'SAMPLES/UNKNOWN'."""
    return "SAMPLES/" + TYPE_DIRS.get(type_folder or "Unknown", "UNKNOWN")

# (category, sub, keywords). Order matters: first match wins; drums are listed in kit order.
CATEGORY_RULES = [
    ("DRUMS", "KICK", ["kick", "kicks", "bd", "bassdrum", "kik"]),
    ("DRUMS", "SNARE-CLAP-RIM", ["snare", "snares", "sd", "clap", "claps", "clp", "rim", "rimshot", "snr"]),
    ("DRUMS", "HAT-OPEN", ["ohh", "openhat", "open", "oh"]),
    ("DRUMS", "HAT-CLOSED", ["chh", "closedhat", "closed", "hat", "hats", "hihat", "hh", "ch"]),
    ("DRUMS", "TOMS", ["tom", "toms"]),
    ("DRUMS", "CYMBALS", ["crash", "ride", "cymbal", "cymbals", "splash", "china"]),
    ("DRUMS", "PERC", ["perc", "percussion", "shaker", "conga", "bongo", "tamb", "tambourine", "cowbell",
                       "clave", "woodblock", "triangle", "zap"]),
    ("LOOPS", "", ["loop", "loops", "break", "breaks", "groove"]),
    ("BASS", "", ["bass", "sub", "808", "reese"]),
    ("SYNTH", "ACID", ["acid", "303"]),
    ("SYNTH", "STAB", ["stab", "stabs", "chord", "chords"]),
    ("SYNTH", "ARP", ["arp", "arps"]),
    ("SYNTH", "LEAD", ["lead", "leads", "synth", "pluck", "plucks"]),
    ("DRONES", "", ["drone", "drones"]),
    ("PADS", "", ["pad", "pads", "atmos", "atmosphere", "texture", "ambient"]),
    ("FX", "RISE", ["riser", "rise", "uplifter", "sweep", "buildup"]),
    ("FX", "IMPACT", ["impact", "hit", "boom", "downlifter"]),
    ("FX", "NOISE", ["noise", "fx", "sfx", "glitch"]),
    ("VOX", "", ["vox", "vocal", "vocals", "voice", "chant", "speech"]),
    ("DRUMS", "", ["drum", "drums", "kit", "oneshot", "oneshots"]),
]
OTHER = ("OTHER", "")

_TOKEN = re.compile(r"[a-z]+|\d+")
_BPM = re.compile(r"(?<!\d)(\d{2,3})\s*-?\s*bpm\b", re.I)
_KEY = re.compile(r"(?<![A-Za-z])([A-G])(#|b|sharp|flat)?\s*(maj|min|m)(?![a-z])", re.I)


def tokens(text: str) -> list[str]:
    # split camelCase too: "OpenHat" -> open hat
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return _TOKEN.findall(text.lower())


def classify(path_parts: list[str]) -> tuple[str, str]:
    """Category for a file from its own name, then its folders nearest first."""
    for part in path_parts:
        toks = set(tokens(part))
        for cat, sub, words in CATEGORY_RULES:
            if toks.intersection(words):
                return cat, sub
    return OTHER


def clean(text: str, upper: bool = True) -> str:
    t = text.upper() if upper else text
    t = t.replace("&", " AND ")
    t = re.sub(r"[^A-Za-z0-9 _\-#()]", "_", t)
    t = re.sub(r"_+", "_", t)
    t = re.sub(r" +", " ", t)
    return t.strip(" _-") or "SAMPLE"


def bpm_key(name: str) -> tuple[str, str | None, str | None]:
    """Pull a BPM ('145bpm') and a key ('F#m', 'A min') out of a name.
    Returns (name without them, bpm, key) — key in the form 'F#M' / 'CMAJ'."""
    bpm = key = None
    m = _BPM.search(name)
    if m:
        bpm = m.group(1)
        name = name[:m.start()] + " " + name[m.end():]
    m = _KEY.search(name)
    if m:
        acc = {"sharp": "#", "flat": "B", "#": "#", "b": "B"}.get((m.group(2) or "").lower(), "")
        mode = "M" if m.group(3).lower() in ("m", "min") else "MAJ"
        key = f"{m.group(1).upper()}{acc}{mode}"
        name = name[:m.start()] + " " + name[m.end():]
    return name, bpm, key


def card_name(original_filename: str) -> str:
    stem = os.path.splitext(original_filename)[0]
    rest, bpm, key = bpm_key(stem)
    base = clean(rest).replace(" ", "_")
    base = re.sub(r"_+", "_", base).strip("_") or "SAMPLE"
    suffix = "".join(f"_{x}" for x in (key, bpm) if x)
    base = base[: MAX_NAME - len(suffix)].rstrip("_-")
    return base + suffix + ".WAV"


def guess_vendor_pack(src: str, library_root: str | None, drop_root: str | None) -> tuple[str | None, str, str]:
    """(source, vendor, pack) guessed from where the file is.
    In the library: LIBRARY/{SOURCE}/{VENDOR}/{PACK}/... is read exactly.
    Elsewhere: the dropped folder's name is the pack and its parent the vendor."""
    src = os.path.abspath(src)
    if library_root:
        lr = os.path.abspath(library_root)
        if src.lower().startswith(lr.lower() + os.sep):
            parts = os.path.relpath(src, lr).split(os.sep)
            if parts and parts[0].upper() in SOURCES:
                vendor = parts[1] if len(parts) > 2 else ""
                pack = parts[2] if len(parts) > 3 else ""
                return parts[0].upper(), vendor, pack
    base = drop_root or os.path.dirname(src)
    pack = os.path.basename(base.rstrip("\\/"))
    vendor = os.path.basename(os.path.dirname(base.rstrip("\\/")))
    return None, vendor, pack


def card_path(source: str, vendor: str, pack: str, original: str, rel_in_pack: str | None = None,
              name: str | None = None, type_folder: str | None = None) -> str:
    """Card-relative destination for one file (before clash handling). name: the sample's ID label
    ("HO0046 Fun Hat"), used for the file name in place of the original's. type_folder: its type list (Kick …);
    guessed from its path in the pack when not given."""
    source = source.upper()
    if source == "FACTORY":
        sub = [clean(p) for p in (rel_in_pack or os.path.basename(original)).replace("\\", "/").split("/")[:-1]]
        return "/".join(["SAMPLES", "FACTORY", *sub, card_name(os.path.basename(original))])
    if not type_folder:
        from dsm.library import layout, types as stypes
        where = "/".join(x for x in ("Pack", vendor, pack, (rel_in_pack or "").replace("\\", "/") or os.path.basename(original)) if x)
        type_folder = layout.type_folder(stypes.guess(where)[0])
    return type_dir(type_folder) + "/" + card_name(name + ".wav" if name else os.path.basename(original))


def unique(path: str, taken) -> str:
    """Add _2, _3 … before the extension until the path is not in `taken` (lower-case set)."""
    if path.lower() not in taken:
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while f"{stem}_{i}{ext}".lower() in taken:
        i += 1
    return f"{stem}_{i}{ext}"
