"""Reading the library's files: plain files and samples inside zips, the audio identity, and the audition cache.

Library paths ("rel") are relative to the library root (Studio\\Samples), with forward slashes. A sample inside
a zip is written  Paid/Dark Mass.zip::Kicks/BD 01.wav  and one in a zip inside a zip has two "::".
Zips are never unpacked in the library (spec 2.2); auditioning or sending one unpacks it to a cache folder on the
PC that keeps the most recently used 2 GB.

The identity of a sample is a hash of its audio alone (spec 2.3): the channels, bit depth and rate and the
bytes of the audio data — not the header's other chunks — so a copy whose header was fixed or whose
metadata differs is still the same sample. FLAC, OGG and MP3 have no separate data chunk, so their identity is
a hash of the whole file.
"""
from __future__ import annotations

import hashlib
import io
import os
import struct
import zipfile

from dsm.audio.inspect import AUDIO_EXTS
from dsm.library import layout

SEP = "::"
CACHE_LIMIT = 2 * 1024 ** 3


def is_audio(name: str) -> bool:
    base = os.path.basename(name)
    return os.path.splitext(base)[1].lower() in AUDIO_EXTS and not base.startswith("._")


def is_zip(name: str) -> bool:
    return name.lower().endswith(".zip")


# ------------------------------------------------------------------ identity
def identity(data: bytes) -> str:
    """The audio identity of a file's bytes (see the module note)."""
    h = hashlib.sha1()
    try:
        if data[:4] in (b"RIFF", b"RF64") and data[8:12] == b"WAVE":
            fmt, body = _wav_parts(data)
            if body is not None:
                h.update(b"wav" + fmt)
                h.update(body)
                return h.hexdigest()
        elif data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
            fmt, body = _aiff_parts(data)
            if body is not None:
                h.update(b"aif" + fmt)
                h.update(body)
                return h.hexdigest()
    except (struct.error, IndexError):
        pass
    h.update(b"file")
    h.update(data)
    return h.hexdigest()


def _wav_parts(d: bytes):
    pos, fmt, body = 12, b"", None
    while pos + 8 <= len(d):
        cid, size = d[pos:pos + 4], struct.unpack("<I", d[pos + 4:pos + 8])[0]
        start = pos + 8
        if cid == b"fmt ":
            _tag, ch, rate, _bps, _align, bits = struct.unpack("<HHIIHH", d[start:start + 16])
            if size >= 20 and _tag == 0xFFFE:
                valid = struct.unpack("<H", d[start + 18:start + 20])[0]
                bits = valid or bits
            fmt = struct.pack("<HIH", ch, rate, bits)
        elif cid == b"data":
            body = d[start:start + size]
        pos = start + size + (size & 1)
    return fmt, body


def _aiff_parts(d: bytes):
    pos, fmt, body = 12, b"", None
    while pos + 8 <= len(d):
        cid, size = d[pos:pos + 4], struct.unpack(">I", d[pos + 4:pos + 8])[0]
        start = pos + 8
        if cid == b"COMM":
            fmt = d[start:start + 18]
            fmt = fmt[:2] + fmt[6:]                       # channels, bits, rate (not the frame count)
        elif cid == b"SSND":
            off = struct.unpack(">I", d[start:start + 4])[0]
            body = d[start + 8 + off:start + size]
        pos = start + size + (size & 1)
    return fmt, body


def file_sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------ walking
def walk_files(top: str, root: str):
    """(rel, full path) of every audio file and zip below top. Skips the app's folder, Archive and Backups."""
    for dp, dns, fns in os.walk(top):
        dns[:] = sorted(d for d in dns if not d.startswith(".") and d.lower() not in layout.SKIP_DIRS)
        for fn in sorted(fns):
            if is_audio(fn) or is_zip(fn):
                full = os.path.join(dp, fn)
                yield os.path.relpath(full, root).replace(os.sep, "/"), full


def zip_members(z: zipfile.ZipFile, prefix: str):
    """(rel, bytes) of every audio file in an open zip, going into zips inside it."""
    for info in z.infolist():
        if info.is_dir() or info.filename.startswith("__MACOSX/"):
            continue
        name = info.filename
        if is_audio(name):
            yield prefix + SEP + name, z.read(info)
        elif is_zip(name):
            try:
                with zipfile.ZipFile(io.BytesIO(z.read(info))) as inner:
                    yield from zip_members(inner, prefix + SEP + name)
            except zipfile.BadZipFile:
                continue


def read_bytes(root: str, rel: str) -> bytes:
    """The bytes of a library file or of a sample inside a zip."""
    parts = rel.split(SEP)
    path = os.path.join(root, *parts[0].split("/"))
    if len(parts) == 1:
        with open(path, "rb") as f:
            return f.read()
    z = zipfile.ZipFile(path)
    try:
        for name in parts[1:-1]:
            z2 = zipfile.ZipFile(io.BytesIO(z.read(name)))
            z.close()
            z = z2
        return z.read(parts[-1])
    finally:
        z.close()


def disk_path(root: str, rel: str) -> str:
    """The file on disk that holds rel: the file itself, or the outer zip."""
    return os.path.join(root, *rel.split(SEP)[0].split("/"))


def base_name(rel: str) -> str:
    return rel.split(SEP)[-1].rsplit("/", 1)[-1]


# ------------------------------------------------------------------ the audition cache
def materialize(root: str, rel: str, cache_dir: str) -> str:
    """A real file for rel: the file itself, or for a sample in a zip a copy in the cache (unpacked once)."""
    if SEP not in rel:
        return os.path.join(root, *rel.split("/"))
    key = hashlib.sha1(rel.lower().encode("utf-8")).hexdigest()[:16]
    path = os.path.join(cache_dir, key, base_name(rel))
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(read_bytes(root, rel))
        os.replace(tmp, path)
        prune_cache(cache_dir)
    else:
        os.utime(path)
    return path


def prune_cache(cache_dir: str, limit: int = CACHE_LIMIT) -> int:
    """Keep the most recently used files up to limit bytes. Returns how many were removed."""
    files = []
    for dp, _dns, fns in os.walk(cache_dir):
        for fn in fns:
            p = os.path.join(dp, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
    files.sort(reverse=True)
    total, gone = 0, 0
    for _t, size, p in files:
        total += size
        if total > limit:
            try:
                os.remove(p)
                gone += 1
            except OSError:
                pass
    return gone


# ------------------------------------------------------------------ where a path sits
AREA = "Library"


def split_rel(rel: str):
    """(area, source, level1, level2) for the left pane.
    Paid/Dark Mass.zip::Snares/Snare 1.wav  → ("Library", "Paid", "Dark Mass", "Snares")
    Factory/Deluge/DRUMS/KICK/X.WAV         → ("Library", "Factory", "Deluge", "DRUMS")"""
    parts = rel.split(SEP)
    comps = []
    for i, part in enumerate(parts):
        bits = part.split("/")
        if i < len(parts) - 1 and is_zip(bits[-1]):
            bits[-1] = bits[-1][:-4]
        comps += bits
    comps = comps[:-1]                               # folders (and zips) only
    src = comps[0] if comps else ""
    src = src.title() if src.lower() in ("factory", "paid", "free", "user") else src
    return AREA, src, comps[1] if len(comps) > 1 else "", comps[2] if len(comps) > 2 else ""
