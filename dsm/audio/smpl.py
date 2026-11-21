"""Write a WAV smpl chunk (loop points and root note), which libsndfile does not carry across."""
from __future__ import annotations

import struct


def append_smpl(path: str, rate: int, loop_start: int | None, loop_end: int | None, root_note: int | None) -> None:
    """Append a smpl chunk to an existing WAV and fix the RIFF size."""
    if loop_end is None and root_note is None:
        return
    period = int(round(1e9 / rate)) if rate else 0
    loops = b""
    n = 0
    if loop_end is not None and loop_start is not None and loop_end > loop_start:
        loops = struct.pack("<IIIIII", 0, 0, int(loop_start), int(loop_end), 0, 0)
        n = 1
    root = 60 if root_note is None else int(root_note)
    body = struct.pack("<IIIIIIIII", 0, 0, period, root, 0, 0, 0, n, 0) + loops
    with open(path, "r+b") as f:
        f.seek(0, 2)
        if f.tell() & 1:
            f.write(b"\x00")
        f.write(b"smpl" + struct.pack("<I", len(body)) + body)
        size = f.tell() - 8
        f.seek(4)
        f.write(struct.pack("<I", size))
