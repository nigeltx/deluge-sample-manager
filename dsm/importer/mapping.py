"""Which card file came from which library original.

The library keeps only originals, exactly as bought or downloaded; the card holds the
converted copies. This file records the link both ways. It lives in the library root
(_DSM/mapping.json) so it is synced with the library and survives a PC reinstall.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

from dsm.settings import app_dir


@dataclass
class Link:
    card: str            # card-relative path, e.g. SAMPLES/PAID/X/Y/DRUMS/KICK/BD.WAV
    library: str         # path relative to the library root, e.g. PAID/X/Y/Kicks/BD 01.wav
    card_serial: str = ""
    converted: str = ""  # what was done, e.g. "resampled 48 → 44.1 kHz"
    profile: str = ""
    when: str = ""


class Mapping:
    def __init__(self, library_root: str | None):
        if library_root and os.path.isdir(library_root):
            self.path = os.path.join(library_root, "_DSM", "mapping.json")
        else:
            self.path = os.path.join(app_dir(), "mapping.json")
        self.links: list[Link] = []
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.links = [Link(**d) for d in json.load(f).get("links", [])]
        except (OSError, ValueError, TypeError):
            self.links = []

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "links": [asdict(x) for x in self.links]}, f, indent=1)
        os.replace(tmp, self.path)

    def add(self, link: Link):
        link.when = link.when or time.strftime("%Y-%m-%d %H:%M")
        self.links = [x for x in self.links if not (x.card.lower() == link.card.lower() and x.card_serial == link.card_serial)]
        self.links.append(link)

    def remove_card(self, card_rel: str, serial: str = ""):
        self.links = [x for x in self.links if not (x.card.lower() == card_rel.lower() and x.card_serial == serial)]

    def for_card(self, card_rel: str) -> Link | None:
        for x in self.links:
            if x.card.lower() == card_rel.lower():
                return x
        return None

    def for_library(self, lib_rel: str) -> list[Link]:
        return [x for x in self.links if x.library.lower() == lib_rel.lower()]
