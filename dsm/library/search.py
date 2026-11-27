"""Search filters (spec v2.4 8.4) and saved smart lists (8.5).

A search is words; every word must match. A word with a colon is a filter (type:kick); a minus in front turns any
word round (-loop, -on:any). Plain words match the ID, the name, the pack (the whole library path) and the tags.

parse() turns the text into Terms; matches() checks one sample, described by a plain dict (see Ctx in
the window: id, name, path, tags, types, loop, bpm, key, seconds, on, source, dupe, plays).
Saved smart lists are kept in _Sample Manager\\smart-lists.json as [{"name": …, "query": …}].
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from dsm.library import tempokey
from dsm.library import types as stypes

FILTERS = ["type", "tag", "bpm", "key", "len", "on", "src", "pack", "id"]
FLAGS = ["loop", "dupe", "wontplay", "untagged"]
HELP = [  # (filter, example, matches) — the table shown by the ? button
    ("text", "roll kick   kk0212", "ID, name, pack and tags; all words"),
    ("type", "type:kick   type:hat   type:drums", "the type; hat covers open, closed and unsure, drums every drum type"),
    ("loop", "loop   -loop", "loops, or one-shots"),
    ("tag", "tag:psychill tag:dark", "every tag listed (confirmed ones)"),
    ("bpm", "bpm:145   bpm:140-150", "a tempo or a range"),
    ("key", "key:F#m   key:Gbm", "a key, spelled any way"),
    ("length", "len:<1s   len:>4s   len:1-2s", "duration"),
    ("device", "on:deluge   on:syntakt   on:any   -on:any", "where copies are"),
    ("source", 'src:paid   src:factory   pack:"dark mass"', "where it came from"),
    ("state", "dupe   wontplay   untagged", "more than one copy in the library; won't play on the Deluge as it is; "
                                            "no confirmed tag"),
]

_TYPE_ALIASES = {"hat": ["Closed hat", "Open hat", "Hat"], "hats": ["Closed hat", "Open hat", "Hat"],
                 "drums": stypes.DRUMS, "drum": stypes.DRUMS, "closed": ["Closed hat"], "open": ["Open hat"],
                 "closed-hat": ["Closed hat"], "open-hat": ["Open hat"], "closedhat": ["Closed hat"],
                 "openhat": ["Open hat"], "perc": ["Percussion"], "vox": ["Vocal"], "vocals": ["Vocal"],
                 "keys": ["Keys"], "unknown": [stypes.UNKNOWN]}


@dataclass
class Term:
    kind: str            # text, type, tag, bpm, key, len, on, src, pack, id, loop, dupe, wontplay, untagged
    value: object
    neg: bool = False
    raw: str = ""


def _split(text: str) -> list[str]:
    """Words; double quotes keep spaces in one word (pack:"dark mass"). An apostrophe is just a letter."""
    return [w.replace('"', "") for w in re.findall(r'[^\s"]*"[^"]*"?[^\s"]*|\S+', text or "") if w.replace('"', "")]


def _range(v: str, unit: str = "") -> tuple[float, float] | None:
    """'145' → (145, 145); '140-150' → (140, 150); '<1s' → (0, 1); '>4' → (4, inf)."""
    v = v.strip().lower()
    if unit:
        v = v.replace(unit, "")
    try:
        if v.startswith("<="):
            return 0.0, float(v[2:])
        if v.startswith(">="):
            return float(v[2:]), float("inf")
        if v.startswith("<"):
            return 0.0, float(v[1:]) - 1e-9
        if v.startswith(">"):
            return float(v[1:]) + 1e-9, float("inf")
        if "-" in v:
            a, b = v.split("-", 1)
            return float(a), float(b)
        x = float(v)
        return x, x
    except ValueError:
        return None


def _types_for(v: str) -> list[str] | None:
    v = v.lower().replace("_", " ")
    if v in _TYPE_ALIASES:
        return _TYPE_ALIASES[v]
    for t in stypes.ALL_TYPES + [stypes.UNKNOWN]:
        if v in (t.lower(), t.lower() + "s", t.lower().replace(" ", "")):
            return [t]
    return None


def parse(text: str) -> tuple[list[Term], list[str]]:
    """(terms, words not understood as filters — searched as text, and the box turns amber)."""
    terms, unknown = [], []
    for w in _split(text):
        neg = w.startswith("-") and len(w) > 1
        body = w[1:] if neg else w
        low = body.lower()
        if ":" in body:
            k, v = body.split(":", 1)
            k = k.lower()
            if k in ("source",):
                k = "src"
            if k == "length":
                k = "len"
            val = None
            if k == "type":
                val = _types_for(v)
            elif k == "tag":
                from dsm.library.tags import norm
                val = norm(v)
            elif k == "bpm":
                val = _range(v, "bpm")
            elif k == "key":
                val = tempokey.norm_key(v)
            elif k == "len":
                val = _range(v, "s")
            elif k == "on":
                val = v.lower() if v.lower() in ("deluge", "syntakt", "any") else None
            elif k == "src":
                val = v.lower() if v.lower() in ("factory", "paid", "free", "user") else None
            elif k in ("pack", "id"):
                val = v.lower() or None
            if k in FILTERS and val is not None:
                terms.append(Term(k, val, neg, w))
                continue
            unknown.append(w)
            terms.append(Term("text", low, neg, w))
            continue
        if low in FLAGS:
            terms.append(Term(low, True, neg, w))
            continue
        terms.append(Term("text", low, neg, w))
    return terms, unknown


def is_plain(terms: list[Term]) -> bool:
    return all(t.kind == "text" and not t.neg for t in terms)


def _hit(t: Term, c: dict) -> bool:
    k, v = t.kind, t.value
    if k == "text":
        return (v in c["text"]) or (v == (c.get("id") or "").lower())
    if k == "type":
        return any(x in v for x in c["types"])
    if k == "tag":
        return v in c["tags"]
    if k == "bpm":
        b = c.get("bpm")
        return b is not None and v[0] - 0.5 <= b <= v[1] + 0.5
    if k == "key":
        return c.get("key") == v
    if k == "len":
        s = c.get("seconds")
        return s is not None and v[0] <= s <= v[1]
    if k == "on":
        on = c.get("on") or set()
        return bool(on) if v == "any" else v in on
    if k == "src":
        return (c.get("source") or "").lower() == v
    if k == "pack":
        return v in (c.get("path") or "").lower()
    if k == "id":
        return (c.get("id") or "").lower().startswith(v)
    if k == "loop":
        return bool(c.get("loop"))
    if k == "dupe":
        return bool(c.get("dupe"))
    if k == "wontplay":
        return c.get("plays") is False
    if k == "untagged":
        return not c["tags"]
    return False


def matches(terms: list[Term], c: dict) -> bool:
    return all(_hit(t, c) != t.neg for t in terms)


# ---------------------------------------------------------------- saved smart lists
def load_lists(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return [x for x in d if isinstance(x, dict) and x.get("name") and x.get("query")]
    except (OSError, ValueError, TypeError):
        return []


def save_lists(path: str, lists: list[dict]) -> bool:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lists, f, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
