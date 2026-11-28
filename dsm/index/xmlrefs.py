"""Find every sample reference in Deluge KITS, SYNTHS and SONGS files.

References are found in the raw bytes so their exact byte positions are known — a
later rename (milestone M4) changes only those bytes and leaves the rest of the file
exactly as the Deluge wrote it. Three forms are read (spec Table 5.1):

  fileName="SAMPLES/..."            attribute
  <fileName>SAMPLES/...</fileName>  element
  filePath="SAMPLES/..."            attribute, used by audio clips in songs
"""
from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, field

_ATTR = re.compile(rb'\b(fileName|filePath)\s*=\s*"([^"]*)"')
_ELEM = re.compile(rb"<fileName>([^<]*)</fileName>")
_SOUND_OPEN = re.compile(rb"<(sound|audioTrack|instrument)\b[^>]*>", re.S)
_NAME_ATTR = re.compile(rb'\bname\s*=\s*"([^"]*)"')
_NAME_ELEM = re.compile(rb"<name>([^<]*)</name>")


@dataclass
class Reference:
    xml: str               # card-relative path of the XML file
    form: str              # attr, element, filePath
    start: int             # byte offset of the path value
    end: int
    raw: str               # path exactly as written (XML-escaped)
    row: str = ""          # kit row / sound name where known
    resolved: str | None = None

    @property
    def path(self) -> str:
        return html.unescape(self.raw).replace("\\", "/").lstrip("/")


@dataclass
class XmlDoc:
    rel: str
    kind: str              # kit, synth, song
    name: str
    size: int
    mtime: float
    refs: list[Reference] = field(default_factory=list)
    error: str | None = None


def _row_name(data: bytes, pos: int) -> str:
    """Name of the nearest enclosing <sound> before pos, if it can be found cheaply."""
    last = None
    for m in _SOUND_OPEN.finditer(data, 0, pos):
        last = m
    if not last:
        return ""
    tag = last.group(0)
    m = _NAME_ATTR.search(tag)
    if m:
        return html.unescape(m.group(1).decode("utf-8", "replace"))
    m = _NAME_ELEM.search(data, last.end(), min(pos, last.end() + 400))
    return html.unescape(m.group(1).decode("utf-8", "replace")) if m else ""


def scan_xml(abs_path: str, rel: str) -> XmlDoc:
    st = os.stat(abs_path)
    with open(abs_path, "rb") as f:
        return scan_data(f.read(), rel, st.st_size, st.st_mtime)


def scan_card(card, rel: str, fresh: bool = False) -> XmlDoc:
    try:
        size, mtime = card.stat(rel)
        return scan_data(card.read_bytes(rel, fresh=fresh), rel, size, mtime)
    except Exception as e:  # noqa: BLE001 — an unreadable preset is reported, not fatal
        doc = _doc(rel, 0, 0.0)
        doc.error = f"Can't read: {e}"
        return doc


def _doc(rel, size, mtime) -> XmlDoc:
    top = rel.split("/")[0].upper()
    kind = {"KITS": "kit", "SYNTHS": "synth", "SONGS": "song"}.get(top, "other")
    return XmlDoc(rel=rel, kind=kind, name=os.path.splitext(os.path.basename(rel))[0], size=size, mtime=mtime)


def scan_data(data: bytes, rel: str, size: int, mtime: float) -> XmlDoc:
    doc = _doc(rel, size, mtime)
    found = []
    for m in _ATTR.finditer(data):
        form = "attr" if m.group(1) == b"fileName" else "filePath"
        found.append((m.start(2), m.end(2), form, m.group(2)))
    for m in _ELEM.finditer(data):
        found.append((m.start(1), m.end(1), "element", m.group(1)))
    found.sort()
    for s, e, form, raw in found:
        val = raw.decode("utf-8", "replace").strip()
        if not val:
            continue                       # empty fileName = unused oscillator
        doc.refs.append(Reference(xml=rel, form=form, start=s, end=e, raw=val, row=_row_name(data, s)))
    doc.error = well_formed_error(data) or None
    return doc


_DECL = re.compile(rb"^(\xef\xbb\xbf)?\s*<\?xml[^>]*\?>")


def well_formed_error(data: bytes) -> str:
    """'' when the file is well-formed as the Deluge writes it, else the reason. Deluge files from firmware 1–3 have
    several elements at the top (<firmwareVersion>, <earliestCompatibleFirmware>, then <sound> or <kit>), which strict
    XML calls "extra content" — so the check wraps the file in one outer element first (0.16.1; before, 135 good
    factory presets were reported damaged). The declaration is blanked to spaces so line numbers stay right."""
    m = _DECL.match(data)
    body = (b" " * len(m.group(0)) + data[m.end():]) if m else data
    try:
        from lxml import etree
        etree.fromstring(b"<deluge>" + body + b"</deluge>", etree.XMLParser(recover=False, huge_tree=True))
    except Exception as e:  # noqa: BLE001
        return f"Not well-formed XML: {e}"
    return ""
