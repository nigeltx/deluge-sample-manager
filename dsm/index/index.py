"""The card index: every XML document, every reference, and the reverse map
sample -> references (the 'Used by' list)."""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field

from dsm.card.card import Card
from dsm.index.xmlrefs import Reference, XmlDoc, scan_card


@dataclass
class CardIndex:
    card: Card
    docs: list[XmlDoc] = field(default_factory=list)
    used_by: dict[str, list[Reference]] = field(default_factory=lambda: defaultdict(list))  # key: lower rel path
    missing: list[Reference] = field(default_factory=list)

    @property
    def refs(self) -> list[Reference]:
        return [r for d in self.docs for r in d.refs]

    def uses(self, rel: str) -> list[Reference]:
        return self.used_by.get(rel.lower(), [])


def build_index(card: Card, progress=None) -> CardIndex:
    """Scan the card (if not yet scanned) and index every XML file. progress(i, n, rel) is
    called as files are read so the UI can show a bar."""
    if not card.files:
        card.scan()
    idx = CardIndex(card=card)
    xmls = card.xml_files()
    for i, rel in enumerate(xmls):
        if progress:
            progress(i, len(xmls), rel)
        doc = scan_card(card, rel)
        for r in doc.refs:
            r.resolved = card.resolve(r.path)
            if r.resolved:
                idx.used_by[r.resolved.lower()].append(r)
            else:
                idx.missing.append(r)
        idx.docs.append(doc)
    if progress:
        progress(len(xmls), len(xmls), "")
    return idx


def unused_samples(idx: CardIndex) -> list[str]:
    return [s for s in idx.card.sample_files() if s.lower() not in idx.used_by]


def snippet(card: Card, ref: Reference, context: int = 260) -> tuple[str, int, int]:
    """Text around a reference, and the reference's position within that text."""
    data = card.read_bytes(ref.xml)
    a = max(0, ref.start - context)
    b = min(len(data), ref.end + context)
    pre = data[a:ref.start].decode("utf-8", "replace")
    mid = data[ref.start:ref.end].decode("utf-8", "replace")
    post = data[ref.end:b].decode("utf-8", "replace")
    return pre + mid + post, len(pre), len(pre) + len(mid)


def basename_map(paths) -> dict[str, list[str]]:
    m = defaultdict(list)
    for p in paths:
        m[os.path.basename(p).lower()].append(p)
    return m
