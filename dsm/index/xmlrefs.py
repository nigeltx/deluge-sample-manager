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
    top = rel.split("/")[0].upper()
    kind = {"KITS": "kit", "SYNTHS": "synth", "SONGS": "song"}.get(top, "other")
    st = os.stat(abs_path)
    doc = XmlDoc(rel=rel, kind=kind, name=os.path.splitext(os.path.basename(rel))[0], size=st.st_size, mtime=st.st_mtime)
    try:
        with open(abs_path, "rb") as f:
            data = f.read()
    except OSError as e:
        doc.error = str(e)
        return doc
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
    try:
        from lxml import etree
        etree.fromstring(data, etree.XMLParser(recover=False, huge_tree=True))
    except Exception as e:  # noqa: BLE001
        doc.error = f"Not well-formed XML: {e}"
    return doc
