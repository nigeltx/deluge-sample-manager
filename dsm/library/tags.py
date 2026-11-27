"""Tags (spec v2.4 8.2) and the file that keeps them, _Sample Manager\\tags.json (8.6).

A tag belongs to the sound: it is kept by audio identity, like the sample ID, so every copy of a sound has it.
Tags are lower case letters, digits and hyphens ("Dark Ambient" → "dark-ambient").

Guessed tags: a word of the vocabulary in a pack or folder name ("Psytrance Kicks") gives the samples in it that tag
as a guess. Guesses are not stored — they are worked out from the path each time — but your answer to one is:
the db table tag holds (audio, tag, state) with state 'yes' (yours or a confirmed guess) or 'no' (a guess you took
off, so it stays off). Only 'yes' tags count in searches.

tags.json holds everything you typed: {"tags": {audio: {"yes": [...], "no": [...]}},
"tempo": {audio: {"bpm": 145, "key": "F#m"}}, "vocab": {"style": [...], "character": [...]}}.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache

VOCAB = {
    "style": ["psytrance", "psychill", "psybient", "techno", "ambient", "downtempo"],
    "character": ["punchy", "dark", "bright", "warm", "dry", "wet", "long", "short", "analog", "lo-fi"],
}


def norm(tag: str) -> str | None:
    """'Dark Ambient' → 'dark-ambient'; None when nothing is left."""
    t = re.sub(r"[\s_]+", "-", (tag or "").strip().lower())
    t = re.sub(r"[^a-z0-9-]", "", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    return t or None


def split_input(text: str) -> list[str]:
    """What the user typed in a Tags box: tags separated by commas (or semicolons)."""
    out = []
    for part in re.split(r"[,;]+", text or ""):
        t = norm(part)
        if t and t not in out:
            out.append(t)
    return out


def vocab_words(vocab: dict | None = None) -> list[str]:
    v = vocab or VOCAB
    return list(_vocab_tuple(tuple((g, tuple(ws)) for g, ws in v.items())))


@lru_cache(maxsize=64)
def _vocab_tuple(v: tuple) -> tuple:
    out = []
    for _g, words in v:
        for w in words:
            t = norm(w)
            if t and t not in out:
                out.append(t)
    return tuple(out)


def _patterns(word: str) -> list[str]:
    """'lo-fi' is written 'lo-fi', 'lo fi' or 'lofi' in folder names."""
    return sorted({word.replace("-", " "), word.replace("-", "")})


@lru_cache(maxsize=64)
def _compiled(words: tuple) -> tuple:
    return tuple((w, tuple(f" {p} " for p in _patterns(w))) for w in words)


@lru_cache(maxsize=200_000)
def _folder_guesses(folder: str, words: tuple) -> tuple:
    text = " " + re.sub(r"[^a-z0-9]+", " ", folder.lower().replace(".zip", "")) + " "
    return tuple(w for w, pats in _compiled(words) if any(p in text for p in pats))


def guesses(rel: str, vocab: dict | None = None) -> list[str]:
    """Tags guessed from the pack and folder names of a library path (never from the file name itself)."""
    parts = [p for p in rel.replace("::", "/").split("/") if p][:-1]
    return list(_folder_guesses("/".join(parts), tuple(vocab_words(vocab))))


# ---------------------------------------------------------------- tags.json
def load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def save(path: str, data: dict) -> bool:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=0, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
