"""Turn any readable audio file into a WAV the Deluge plays (spec 4.2).

Default profile 'Deluge native': 44.1 kHz, 24-bit, plain PCM header, channels kept,
loop points and root note kept (scaled if resampled). A file that is already a plain
44.1 kHz WAV and needs no processing is copied byte for byte.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass

import numpy as np
import soundfile as sf

from dsm.audio.inspect import AudioInfo, inspect
from dsm.audio.smpl import append_smpl

TARGET_RATE = 44100


@dataclass
class Profile:
    name: str = "Deluge native"
    bits: int = 24                 # 16 or 24
    channels: str = "keep"         # keep, mono
    trim_silence: bool = False
    normalise: bool = False        # peak to -1 dBFS
    fade_out: bool = False         # 5 ms
    copy_if_native: bool = True    # plain 44.1 kHz WAV with no processing is copied untouched

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def processes(self) -> bool:
        return self.trim_silence or self.normalise or self.fade_out or self.channels == "mono"


PROFILES = {
    "Deluge native": Profile(),
    "Drums mono": Profile(name="Drums mono", channels="mono", trim_silence=True, fade_out=True),
}


def needs_conversion(info: AudioInfo, prof: Profile) -> bool:
    if info.error or not info.deluge_plays or info.container != "WAV" or info.header != "PCM":
        return True
    if info.rate != TARGET_RATE:
        return True
    return not (prof.copy_if_native and not prof.processes())


def what_happens(info: AudioInfo, prof: Profile) -> str:
    """One line for the import dialog."""
    if info.error:
        return "can't read this file"
    if not needs_conversion(info, prof):
        return "copied as is"
    bits = []
    if info.container != "WAV":
        bits.append(f"{info.container} → WAV")
    elif info.header == "EXTENSIBLE":
        bits.append("header fixed")
    if info.rate != TARGET_RATE:
        bits.append(f"resampled {info.rate / 1000:g} → 44.1 kHz")
    if prof.channels == "mono" and info.channels > 1:
        bits.append("to mono")
    if info.bits != prof.bits:
        bits.append(f"{info.bits or '?'} → {prof.bits}-bit")
    for flag, word in ((prof.trim_silence, "trimmed"), (prof.normalise, "normalised"), (prof.fade_out, "fade")):
        if flag:
            bits.append(word)
    if info.loop_end and info.rate != TARGET_RATE:
        bits.append("loop points scaled")
    return ", ".join(bits) or "rewritten"


def convert(src: str, dst: str, prof: Profile) -> AudioInfo:
    """Write dst from src. dst should be a temporary name; the caller renames it into place.
    Returns the inspected result. Raises on any failure."""
    info = inspect(src)
    if info.error:
        raise ValueError(f"Can't read {os.path.basename(src)}: {info.error}")
    if not needs_conversion(info, prof):
        shutil.copyfile(src, dst)
        return inspect(dst)
    data, rate = sf.read(src, dtype="float32", always_2d=True)
    offset = 0
    if prof.channels == "mono" and data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
    if data.shape[1] > 2:
        data = data[:, :2]
    if prof.trim_silence and len(data):
        thresh = 10 ** (-60 / 20)
        loud = np.nonzero(np.abs(data).max(axis=1) > thresh)[0]
        if len(loud):
            offset = max(0, int(loud[0]) - int(rate * 0.001))
            data = data[offset:]
    ratio = 1.0
    if rate != TARGET_RATE:
        import soxr
        ratio = TARGET_RATE / rate
        data = soxr.resample(data, rate, TARGET_RATE, quality="VHQ").astype(np.float32)
        if data.ndim == 1:
            data = data[:, None]
    if prof.normalise and data.size:
        peak = float(np.abs(data).max())
        if peak > 0:
            data = data * (10 ** (-1 / 20) / peak)
    if prof.fade_out and len(data):
        n = min(len(data), int(TARGET_RATE * 0.005))
        data[-n:] *= np.linspace(1, 0, n, dtype=np.float32)[:, None]
    data = np.clip(data, -1.0, 1.0)
    sf.write(dst, data, TARGET_RATE, subtype=f"PCM_{prof.bits}", format="WAV")
    ls = le = None
    if info.loop_end is not None and info.loop_start is not None:
        ls = int(round((info.loop_start - offset) * ratio))
        le = int(round((info.loop_end - offset) * ratio))
        if le <= 0 or ls < 0 or le > len(data):
            ls = le = None
    append_smpl(dst, TARGET_RATE, ls, le, info.root_note)
    return inspect(dst)
