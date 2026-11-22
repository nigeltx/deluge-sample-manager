"""Read audio file headers without decoding the audio.

WAV and AIFF are parsed by hand so the app can see exactly what the Deluge will see:
the format tag (plain PCM, float or the extensible header the Deluge refuses), the
smpl chunk's loop points and root note, and the true bit depth. Other formats fall
back to libsndfile for basic facts.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

AUDIO_EXTS = {".wav", ".wave", ".aif", ".aiff", ".aifc", ".flac", ".ogg", ".mp3"}
DELUGE_PLAYABLE_EXTS = {".wav", ".wave", ".aif", ".aiff"}

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

NATIVE_RATE = 44100


@dataclass
class AudioInfo:
    path: str
    container: str = "?"          # WAV, AIFF, FLAC, OGG, MP3
    header: str = "?"             # PCM, FLOAT, EXTENSIBLE, AIFF, AIFC, compressed
    rate: int = 0
    bits: int = 0
    channels: int = 0
    frames: int = 0
    loop_start: int | None = None
    loop_end: int | None = None
    root_note: int | None = None
    error: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return self.frames / self.rate if self.rate else 0.0

    @property
    def deluge_plays(self) -> bool:
        """True when the Deluge should load this file as it is."""
        return not self.error and not any(p.startswith("!") for p in self.problems)

    @property
    def native(self) -> bool:
        return self.deluge_plays and self.rate == NATIVE_RATE

    def short_format(self) -> str:
        if self.error:
            return "unreadable"
        r = f"{self.rate / 1000:g}" if self.rate else "?"
        return f"{r}/{self.bits or '?'}"

    def describe(self) -> str:
        if self.error:
            return f"Unreadable: {self.error}"
        ch = {1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels} ch")
        return f"{self.container} {self.header}, {self.bits}-bit, {ch}"


def _check(info: AudioInfo) -> AudioInfo:
    """Fill info.problems. A leading '!' marks something the Deluge will not play."""
    p = info.problems
    if info.error:
        return info
    if info.container in ("FLAC", "OGG", "MP3"):
        p.append(f"!{info.container} — the Deluge only plays WAV and AIFF; convert it")
    if info.header == "EXTENSIBLE":
        p.append("!Extensible WAV header — the Deluge refuses it; the audio is fine, the header needs fixing")
    if info.header == "FLOAT":
        p.append("32-bit float — not confirmed to play on the Deluge; test it or convert to 24-bit")
    if info.channels > 2:
        p.append(f"!{info.channels} channels — the Deluge plays mono or stereo only")
    if info.rate and info.rate != NATIVE_RATE:
        p.append(f"{info.rate / 1000:g} kHz — plays, but 44.1 kHz is the Deluge's native rate")
    if info.frames == 0:
        p.append("!No audio data")
    return info


def inspect(path: str, fileobj=None) -> AudioInfo:
    """Header facts for a file. fileobj, when given, is a seekable binary file to read instead of path
    (used for files on the Deluge over USB)."""
    ext = os.path.splitext(path)[1].lower()
    info = AudioInfo(path=path)
    try:
        import contextlib
        with (contextlib.nullcontext(fileobj) if fileobj is not None else open(path, "rb")) as f:
            head = f.read(12)
            if head[:4] in (b"RIFF", b"RF64") and head[8:12] == b"WAVE":
                f.seek(0)
                _parse_wav(f, info)
            elif head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
                f.seek(0)
                _parse_aiff(f, info)
            else:
                f.seek(0)
                _parse_other(f if fileobj is not None else path, ext, info)
    except Exception as e:  # noqa: BLE001 — any unreadable file is reported, never fatal
        info.error = str(e) or e.__class__.__name__
    return _check(info)


def _parse_wav(f, info: AudioInfo) -> None:
    info.container = "WAV"
    riff = f.read(12)
    end = 8 + struct.unpack("<I", riff[4:8])[0]
    block_align = 0
    data_size = None
    while True:
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
        start = f.tell()
        if cid == b"fmt ":
            body = f.read(min(size, 64))
            tag, ch, rate, _bps, block_align, bits = struct.unpack("<HHIIHH", body[:16])
            info.channels, info.rate, info.bits = ch, rate, bits
            if tag == WAVE_FORMAT_EXTENSIBLE:
                info.header = "EXTENSIBLE"
                if len(body) >= 24:
                    valid = struct.unpack("<H", body[18:20])[0]
                    if valid:
                        info.bits = valid
            elif tag == WAVE_FORMAT_PCM:
                info.header = "PCM"
            elif tag == WAVE_FORMAT_FLOAT:
                info.header = "FLOAT"
            else:
                info.header = f"tag 0x{tag:04X}"
                info.problems.append(f"!Compressed WAV (format 0x{tag:04X}) — the Deluge needs plain PCM")
        elif cid == b"data":
            data_size = size
        elif cid == b"smpl" and size >= 36:
            body = f.read(size)
            unity = struct.unpack("<I", body[12:16])[0]
            info.root_note = unity if unity < 128 else None
            n_loops = struct.unpack("<I", body[28:32])[0]
            if n_loops and len(body) >= 36 + 24:
                _cue, _typ, s, e, _frac, _cnt = struct.unpack("<IIIIII", body[36:60])
                info.loop_start, info.loop_end = s, e
        f.seek(start + size + (size & 1))
        if f.tell() >= end:
            break
    if data_size is not None and block_align:
        info.frames = data_size // block_align


def _ext80(b: bytes) -> float:
    """IEEE 754 80-bit extended float, as AIFF stores the sample rate."""
    exp = ((b[0] & 0x7F) << 8) | b[1]
    mant = int.from_bytes(b[2:10], "big")
    if exp == 0 and mant == 0:
        return 0.0
    sign = -1 if b[0] & 0x80 else 1
    return sign * mant * 2.0 ** (exp - 16383 - 63)


def _parse_aiff(f, info: AudioInfo) -> None:
    form = f.read(12)
    info.container = "AIFF"
    info.header = form[8:12].decode()
    end = 8 + struct.unpack(">I", form[4:8])[0]
    while f.tell() < end:
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        cid, size = hdr[:4], struct.unpack(">I", hdr[4:])[0]
        start = f.tell()
        if cid == b"COMM":
            body = f.read(size)
            ch, frames, bits = struct.unpack(">hIh", body[:8])
            info.channels, info.frames, info.bits = ch, frames, bits
            info.rate = int(round(_ext80(body[8:18])))
            if info.header == "AIFC" and len(body) >= 22:
                comp = body[18:22]
                if comp not in (b"NONE", b"sowt"):
                    info.problems.append(f"!Compressed AIFF ({comp.decode(errors='replace')}) — convert it")
        f.seek(start + size + (size & 1))


def _parse_other(path, ext: str, info: AudioInfo) -> None:
    import soundfile as sf
    i = sf.info(path)
    info.container = (i.format or ext.strip(".")).upper()
    if info.container == "MPEG":
        info.container = "MP3"
    info.header = "compressed"
    info.rate, info.channels, info.frames = i.samplerate, i.channels, i.frames
    sub = (i.subtype or "")
    info.bits = 24 if "24" in sub else 16 if "16" in sub else 0
