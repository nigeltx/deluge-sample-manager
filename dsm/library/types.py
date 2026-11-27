"""Best-guess sample types (Kick, Snare, Closed hat …) from folder and file names.

How a guess is made, in plain steps:
1. The path is split into words: "HDHS_ClosedHat-03.wav" → "hdhs closed hat 03".
2. The file name is checked first. If it names a type, that is the answer.
3. Otherwise the folders are checked from the one holding the file upwards; the first folder that names
   a type gives the answer. So ".../Snares/Bright 01.wav" is a Snare.
4. A hat that doesn't say open or closed is judged by length: short = closed, long = open.
5. A sample is a loop when its name or folder says loop, or gives a tempo (120 bpm), else a one-shot.

The user's own choice (Set type…) is kept apart from the guess and always wins (see db.py).
Change RULES_VERSION whenever the rules change, so every guess is worked out again.
"""
from __future__ import annotations

import re

RULES_VERSION = 2                # 2: Drone added (26 Nov 2026)

# The groups as the left pane shows them. Each type belongs to one group.
DRUMS = ["Kick", "Snare", "Clap", "Closed hat", "Open hat", "Hat", "Cymbal", "Tom", "Percussion", "Other drums"]
MUSIC = ["Bass", "Synth", "Keys", "Pad", "Drone", "Vocal", "FX", "Instrument"]
ALL_TYPES = DRUMS + MUSIC
UNKNOWN = "Unknown"
LABELS = {"Hat": "Hats, open or closed?"}       # how a type is shown when its name alone is unclear

# (type, pattern). Patterns match whole words in the lower-case, space-separated path.
RULES = [
    ("Kick", r"kicks?|kik|kck|bd|bass ?drums?|kickdrum"),
    ("Snare", r"snares?|snr|sd|rims?|rimshots?|sn"),
    ("Clap", r"claps?|cp|snaps?|finger ?snaps?"),
    ("Closed hat", r"closed ?(hi ?)?hats?|c ?hats?|(hi ?)?hats? ?c|closed ?hh|chh?|hh ?c(losed|l)?|hats? ?closed|cl ?hats?"),
    ("Open hat", r"open ?(hi ?)?hats?|o ?hats?|(hi ?)?hats? ?o|open ?hh|ohh?|hh ?o(pen|p)?|hats? ?open|op ?hats?"),
    ("Hat", r"(hi ?)?hats?|hh|hihats?"),
    ("Cymbal", r"cymbals?|cym|crash(es)?|rides?|splash|china"),
    ("Tom", r"toms?|floor ?toms?|hi ?toms?|lo ?toms?"),
    ("Percussion", r"percs?|percussions?|shakers?|congas?|bongos?|tambs?|tambourines?|cowbells?|claves?|"
                   r"wood ?blocks?|blocks?|triangles?|guiros?|cabasas?|timbales?|djembes?|tablas?|cajon|gongs?|synares?"),
    ("Other drums", r"drums?|drumkits?|kits?|tops?|breaks?|breakbeats?|grooves?|beats?"),
    ("Bass", r"bass(es)?|basslines?|sub ?bass|subs?|808s?|reese"),
    ("Synth", r"synths?|leads?|plucks?|arps?|arpeggios?|stabs?|chords?|seqs?|sequences?|saws?|hoovers?"),
    ("Keys", r"pianos?|keys|rhodes|organs?|e ?pianos?|wurli(tzer)?|clavs?"),
    ("Drone", r"drones?|droning|drone ?pads?"),
    ("Pad", r"pads?|atmos|atmospheres?|atmospherics?|atomspheric|ambien(ce|t)s?|ambiances?|textures?|"
            r"soundscapes?|grains?|granular|granulations?"),
    ("Vocal", r"vocals?|vox|voices?|chants?|choirs?|acapellas?|vocal ?chops?"),
    ("FX", r"fx|sfx|risers?|impacts?|sweeps?|whoosh(es)?|noises?|uplifters?|downlifters?|reverses?|"
           r"glitch(es)?|foley|transitions?"),
    ("Instrument", r"guitars?|strings?|violins?|cellos?|brass|trumpets?|sax(ophone)?s?|flutes?|horns?|"
                   r"accordions?|marimbas?|kalimbas?|harps?|bells?|mallets?|xylophones?|vibes|sitars?|banjos?"),
]
_COMPILED = [(t, re.compile(rf"(?<![a-z0-9])(?:{p})(?![a-z])")) for t, p in RULES]
_LOOP = re.compile(r"(?<![a-z])(loops?|looped|\d{2,3} ?bpm|bpm ?\d{2,3})(?![a-z])")
_ONESHOT = re.compile(r"(?<![a-z])(one ?shots?|oneshots?|single ?hits?|hits?)(?![a-z])")
_GENRES = re.compile(r"(?<![a-z])(drum (and |n )?bass|dnb|d n b|drum step|drumstep)(?![a-z])")  # genres, not types
_BASS_DRUM = re.compile(r"(?<![a-z])bass ?drums?(?![a-z])")
_BASS_WORD = re.compile(r"(?<![a-z])(bass(es)?|basslines?|sub ?bass|reese)(?![a-z])")

CLOSED_MAX_SECONDS = 0.35        # a hat shorter than this is taken as closed
OPEN_MIN_SECONDS = 0.6           # a hat longer than this is taken as open


def words(text: str) -> str:
    """'HDHS_ClosedHat-03.wav' → 'hdhs closed hat 03'."""
    text = re.sub(r"\.(wav|aiff?|flac|mp3|ogg)$", "", text, flags=re.I)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)           # camelCase → camel Case
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)            # Kick01 → Kick 01
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
    text = re.sub(r"[^A-Za-z0-9#]+", " ", text)
    return " ".join(text.lower().split())


def _types_in(text: str) -> list[str]:
    text = _GENRES.sub(" ", text)
    found = [t for t, rx in _COMPILED if rx.search(text)]
    if "Kick" in found and "Bass" in found and not _BASS_WORD.search(_BASS_DRUM.sub(" ", text)):
        found.remove("Bass")                       # "bass drum" or a plain "808 kick" is a kick, not a bass
    if "Closed hat" in found or "Open hat" in found:
        found = [t for t in found if t != "Hat"]
    specific = [t for t in found if t != "Other drums"]
    if any(t in DRUMS for t in specific):
        found = specific                           # "Drums/Kicks/…" is a kick, not "other drums"
    return found


def guess(rel: str, seconds: float | None = None) -> tuple[list[str], bool, str]:
    """(types, is_loop, why) for a library path like 'Paid/Vendor/Pack/Kicks/Kick 01.wav'."""
    parts = rel.split("/")
    name, folders = parts[-1], parts[1:-1]          # the top folder (Factory/Free/Paid) says nothing about type
    name_w = words(name)
    types, why = _types_in(name_w), "file name"
    if not types:
        for f in reversed(folders):
            types = _types_in(words(f))
            if types:
                why = f"folder “{f}”"
                break
    if types == ["Hat"] and seconds:
        if seconds < CLOSED_MAX_SECONDS:
            types, why = ["Closed hat"], why + "; short, so taken as closed"
        elif seconds > OPEN_MIN_SECONDS:
            types, why = ["Open hat"], why + "; long, so taken as open"
    all_w = " ".join([words(f) for f in folders] + [name_w])
    if _ONESHOT.search(name_w):
        loop = False
    else:
        loop = bool(_LOOP.search(name_w) or _LOOP.search(all_w) and not _ONESHOT.search(all_w))
    return types or [UNKNOWN], loop, (why if types else "no type word in the name or folders")


def label(t: str) -> str:
    return LABELS.get(t, t)


def pack(types: list[str]) -> str:
    """Stored form: ',Kick,Snare,' so a query can look for ',Kick,'."""
    return "," + ",".join(types) + "," if types else ""


def unpack(s: str | None) -> list[str]:
    return [t for t in (s or "").split(",") if t]
