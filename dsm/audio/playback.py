"""Waveform peaks and audition playback.

Playback uses sounddevice (PortAudio, WASAPI on Windows). If no audio device can be
opened the app still runs; Player.available is False and the reason is shown.
"""
from __future__ import annotations

import numpy as np
import soundfile as sf


def load(path, max_seconds: float = 120.0):
    """Return (float32 frames x channels, rate). path may be a file name or a file object.
    Long files are cut at max_seconds."""
    with sf.SoundFile(path) as f:
        n = min(f.frames, int(f.samplerate * max_seconds)) if f.frames > 0 else -1
        data = f.read(frames=n, dtype="float32", always_2d=True)
        return data, f.samplerate


def peaks(data: np.ndarray, bins: int = 600) -> np.ndarray:
    """Max absolute value per bin, mixed to mono, length <= bins."""
    if data.size == 0:
        return np.zeros(1, dtype=np.float32)
    mono = np.abs(data).max(axis=1)
    if len(mono) <= bins:
        return mono
    cut = len(mono) - len(mono) % bins
    p = mono[:cut].reshape(bins, -1).max(axis=1)
    return p


class Player:
    def __init__(self):
        self.error = None
        try:
            import sounddevice as sd  # noqa: F401
            self._sd = sd
        except Exception as e:  # noqa: BLE001 — no PortAudio, no device
            self._sd = None
            self.error = f"Audio output unavailable: {e}"
        self.playing = False
        self.level = 0.8

    @property
    def available(self) -> bool:
        return self._sd is not None

    def play(self, data: np.ndarray, rate: int, loop_region: tuple[int, int] | None = None, loop: bool = False):
        if not self._sd:
            return
        self.stop()
        out = data * self.level
        if loop and loop_region and 0 <= loop_region[0] < loop_region[1] <= len(out):
            # play the head once, then repeat the loop region: build ~30 s of audio
            head, body = out[: loop_region[1]], out[loop_region[0]: loop_region[1]]
            reps = max(1, int(30 * rate / max(1, len(body))))
            out = np.concatenate([head] + [body] * reps)
            self._sd.play(out, rate)
        else:
            self._sd.play(out, rate, loop=loop)
        self.playing = True

    def stop(self):
        if self._sd and self.playing:
            try:
                self._sd.stop()
            except Exception:  # noqa: BLE001
                pass
        self.playing = False
