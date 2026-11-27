"""Tempo (BPM) and key for a sample (spec v2.4 8.3).

Where they come from, first that has one wins:
1. what you typed (db table tempo_key, columns bpm / key; kept in _Sample Manager\\tags.json);
2. the WAV file's acid chunk — the tempo and root note a loop pack's maker stored in the file
   (columns acid_bpm / acid_root, read at scan time or when a sample is auditioned);
3. the file name, then the nearest folder name.

Names: a tempo is a number written with "bpm" (145bpm, 145 BPM, 145_bpm, bpm145); for loops only, a number from
60 to 200 standing alone also counts ("Bass Loop 145 F#m"). A key is a note A-G with an optional sharp or flat and
m / min / minor for minor (F#m, Fsharp minor, Am, C maj), standing alone.
Keys are written one way: sharps, "m" for minor — Gbm is F#m, "A major" is A.
"""
from __future__ import annotations

import re
import struct
from functools import lru_cache

NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLAT = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#", "Cb": "B", "Fb": "E"}
_SHARP_ODD = {"E#": "F", "B#": "C"}

_BPM_TAGGED = re.compile(r"(?<![0-9])(\d{2,3}(?:\.\d+)?)\s*bpm(?![a-z])|(?<![a-z])bpm\s*(\d{2,3}(?:\.\d+)?)(?![0-9])")
_BARE_NUM = re.compile(r"(?<![0-9a-z.#])(\d{2,3})(?![0-9a-z.])")
# a note, then an optional accidental, then an optional mode, as a word of its own
_KEY = re.compile(r"(?<![a-z0-9#])([a-g])(#|♯|b|♭|sharp|flat)?\s*(m(?:in(?:or)?)?|maj(?:or)?|minor|major)?"
                  r"(?![a-z0-9#])")


_EXT = re.compile(r"\.(wav|aiff?|flac|ogg|mp3)$")
_SEPS = re.compile(r"[_\-\[\]\(\)\{\},+]+")


def _words(text: str) -> str:
    """Lower case, separators as spaces, but keep # and decimal points."""
    t = text.lower()
    t = _EXT.sub("", t)
    return _SEPS.sub(" ", t)


def norm_key(text: str) -> str | None:
    """'gb minor' / 'F#m' / 'f sharp min' / 'C' → 'F#m' / 'F#m' / 'F#m' / 'C'. None when it isn't a key."""
    if not text:
        return None
    m = _KEY.fullmatch(_words(text.strip()).strip())
    if not m:
        return None
    return _key_from_match(m, strict=False)


_KEY_OF = re.compile(r"(?<![a-z])key (?:of )?([a-g])(#|♯|b|♭|sharp|flat)?\s*(m(?:in(?:or)?)?|maj(?:or)?|minor|major)?"
                     r"(?![a-z0-9#])")


def _key_from_match(m, strict: bool) -> str | None:
    note, acc, mode = m.group(1).upper(), m.group(2) or "", m.group(3) or ""
    if acc in ("#", "♯", "sharp"):
        note += "#"
    elif acc in ("b", "♭", "flat"):
        note += "b"
    if strict and not acc and not mode:
        return None                    # a lone letter in a name ("a", "e") is too often not a key
    note = _FLAT.get(note, _SHARP_ODD.get(note, note))
    if note not in NOTES:
        return None
    return note + ("m" if mode.startswith("m") and not mode.startswith("maj") else "")


def key_from_name(text: str, tonal: bool = False) -> str | None:
    """The key a file or folder name gives, or None. A lone letter needs a sharp or flat, or a mode, with it
    ("Snare B" is a variation, not a key) — except in the name of a tonal sample (bass, synth, pad …), where a
    capital A-G standing alone ("HDR_E_Syn_Bass_Oneshot_Demonic") is taken as its key."""
    words = _words(text)
    for m in _KEY.finditer(words):
        k = _key_from_match(m, strict=True)
        if k:
            return k
    m = _KEY_OF.search(words)                    # "Key of D"
    if m:
        return _key_from_match(m, strict=False)
    if tonal:
        m = re.search(r"(?<![A-Za-z0-9#])([A-G])(?![A-Za-z0-9#])", re.sub(r"\.\w+$", "", text))
        if m:
            return m.group(1)
    return None


def bpm_from_name(text: str, is_loop: bool) -> float | None:
    words = _words(text)
    m = _BPM_TAGGED.search(words)
    if m:
        v = float(m.group(1) or m.group(2))
        return v if 40 <= v <= 300 else None
    if is_loop:
        for m in _BARE_NUM.finditer(words):
            v = int(m.group(1))
            if 60 <= v <= 200:
                return float(v)
    return None


@lru_cache(maxsize=400_000)
def from_path(rel: str, is_loop: bool, tonal: bool = False) -> tuple[float | None, str | None, str | None, str | None]:
    """(bpm, where, key, where) from the file name, then the folder holding it. rel may hold '::' for zips."""
    parts = [p for p in rel.replace("::", "/").split("/") if p]
    name, folder = parts[-1] if parts else "", parts[-2] if len(parts) > 1 else ""
    bpm, bhow = bpm_from_name(name, is_loop), "name"
    if bpm is None:
        bpm, bhow = bpm_from_name(folder, is_loop), "folder"
    key, khow = key_from_name(name, tonal), "name"
    if key is None:
        key, khow = key_from_name(folder), "folder"
    return bpm, (bhow if bpm is not None else None), key, (khow if key is not None else None)


def resolve(rel: str, is_loop: bool, row=None, tonal: bool = False) -> dict:
    """{'bpm', 'bpm_how', 'key', 'key_how'} for one sample. row: its tempo_key row (or None); tonal: not a drum."""
    row = dict(row) if row else {}
    out = {"bpm": None, "bpm_how": None, "key": None, "key_how": None}
    nb, nbh, nk, nkh = from_path(rel, is_loop, tonal)
    if row.get("bpm"):
        out["bpm"], out["bpm_how"] = row["bpm"], "set by you"
    elif row.get("acid_bpm") and is_loop:
        out["bpm"], out["bpm_how"] = row["acid_bpm"], "from the acid chunk"
    elif nb:
        out["bpm"], out["bpm_how"] = nb, f"from the {nbh}"
    if row.get("key"):
        out["key"], out["key_how"] = row["key"], "set by you"
    elif nk:
        out["key"], out["key_how"] = nk, f"from the {nkh}"
    elif row.get("acid_root") is not None:
        out["key"], out["key_how"] = NOTES[int(row["acid_root"]) % 12], "root note from the acid chunk"
    return out


def fmt_bpm(v) -> str:
    if not v:
        return ""
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def parse_bpm(text: str) -> float | None:
    """What the user typed in a BPM box: a number from 20 to 400, or None."""
    try:
        v = float(text.strip().lower().replace("bpm", "").strip())
    except ValueError:
        return None
    return v if 20 <= v <= 400 else None


# ---------------------------------------------------------------- the acid chunk
def acid_from_bytes(data: bytes) -> tuple[float | None, int | None]:
    """(tempo, root note) from a WAV's acid chunk, or (None, None). Root note is a MIDI note number, only when
    the file says it has one (flag 0x02) — the chunk has no major/minor, so it gives the note alone."""
    if len(data) < 12 or data[:4] not in (b"RIFF", b"RF64") or data[8:12] != b"WAVE":
        return None, None
    pos, end = 12, len(data)
    while pos + 8 <= end:
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"acid" and size >= 24 and body + 24 <= end:
            flags, root = struct.unpack("<IH", data[body:body + 6])
            tempo = struct.unpack("<f", data[body + 20:body + 24])[0]
            t = round(tempo, 2) if 20 <= tempo <= 400 else None
            r = root if flags & 0x02 and 0 < root < 128 else None
            return t, r
        if cid == b"data" and size > 0 and body + size > end:
            break
        pos = body + size + (size & 1)
    return None, None


def acid_from_file(path: str, limit: int = 4 << 20) -> tuple[float | None, int | None]:
    """The acid chunk of a WAV on disk: chunks before the audio are read from its start; one after the audio is
    found by skipping the data chunk without reading it."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
                return None, None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    return None, None
                cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
                if cid == b"acid":
                    body = f.read(min(size, 64))
                    return acid_from_bytes(b"RIFF\0\0\0\0WAVEacid" + struct.pack("<I", len(body)) + body)
                f.seek(size + (size & 1), 1)
    except OSError:
        return None, None
