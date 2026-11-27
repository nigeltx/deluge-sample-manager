"""The Syntakt's +Drive samples over USB (Sample Manager 0.12; bench-tested 27 Nov 2026, Syntakt OS 1.41).

Elektron's transfer protocol, as in the open-source Elektroid and the Patch Manager's Syntakt driver:
F0 00 20 3C 10 00 <7-bit groups> F7. A message is seq(2) 00 00 command args; a reply is ?? seq(2) command|80 status data.

What the bench test found (claude/syntakt-bench-test/sample-findings-2026-11-27.md):
- `53 /samples` lists 64 slots; an item's id is its slot. Reading or writing by name is refused: use /samples/<slot>.
- `54` read open with compression 0, `55` blocks of 8 KB, `56` close.
- `57` write open, `58` blocks — the 12-byte footer must go in a block of its own — `59` close.
- `5C` clear empties a slot.
- A sample: 31-byte data header (slot at 21, payload size at 25), 64-byte slot header ("SAMP", frames at 39, name of
  16 characters at 43), 16-bit big-endian mono 48 kHz audio, 12-byte footer (CRC-32 of slot header + audio, payload
  size, AA A1 DA AA).
- The Syntakt has to be restarted before it plays samples written to it (it says so on its screen).
"""
from __future__ import annotations

import queue
import struct
import threading
import time
import zlib

HDR = bytes([0xF0, 0x00, 0x20, 0x3C, 0x10, 0x00])
BLOCK = 0x2000
SYNTAKT_ID = 30
RATE = 48000
NAME_LEN = 16
SLOTS = 64
AUDIO_AT, FOOTER = 95, 12
MAGIC, TAIL = bytes.fromhex("AC11D303"), bytes.fromhex("AAA1DAAA")


class SyntaktError(Exception):
    pass


def encode(msg: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(msg), 7):
        chunk = msg[i:i + 7]
        acc = 0
        for k in range(7):
            acc <<= 1
            if k < len(chunk) and chunk[k] & 0x80:
                acc |= 1
        out.append(acc)
        out.extend(b & 0x7F for b in chunk)
    return bytes(out)


def decode(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 8):
        hi, shift = data[i], 0x40
        for b in data[i + 1:i + 8]:
            out.append(b | (0x80 if hi & shift else 0))
            shift >>= 1
    return bytes(out)


def parse_list(r: bytes) -> list[dict]:
    pos, out = 18, []
    while pos < len(r):
        end = r.find(b"\0", pos)
        if end < 0 or end + 2 >= len(r):
            break
        name = r[pos:end].decode("cp1252", "replace")
        pos = end + 1
        has_children, typ = r[pos], r[pos + 1]
        pos += 2
        if typ == 1:
            pos += 4
            out.append({"name": name, "dir": True})
        else:
            iid, size = struct.unpack(">II", r[pos:pos + 8])
            pos += 12
            out.append({"name": name, "dir": bool(has_children), "id": iid, "size": size})
    return out


# ------------------------------------------------------------------ the sample format
def parse_sample(d: bytes) -> dict:
    """slot, name, frames, ok (its own sizes and CRC agree) and the audio (big-endian bytes) of a read sample."""
    if len(d) < AUDIO_AT + FOOTER or d[:4] != MAGIC or d[31:35] != b"SAMP":
        return {"ok": False, "slot": 0, "name": "", "frames": 0, "audio": b""}
    frames = struct.unpack(">I", d[39:43])[0]
    crc = struct.unpack(">I", d[-12:-8])[0]
    ok = (d[-4:] == TAIL and len(d) == AUDIO_AT + 2 * frames + FOOTER
          and zlib.crc32(d[31:-FOOTER], 0xFFFFFFFF) & 0xFFFFFFFF == crc)
    return {"ok": ok, "slot": struct.unpack(">I", d[21:25])[0], "frames": frames,
            "name": d[43:43 + NAME_LEN].split(b"\0")[0].decode("cp1252", "replace"),
            "audio": d[AUDIO_AT:AUDIO_AT + 2 * frames]}


def clean_name(name: str) -> str:
    """What the Syntakt can show: upper case, 16 characters, plain ASCII."""
    s = "".join(c if 32 <= ord(c) < 127 else " " for c in name.upper())
    return " ".join(s.split())[:NAME_LEN].rstrip() or "SAMPLE"


def build_sample(slot: int, name: str, audio_be: bytes) -> bytes:
    """A sample ready to write: 16-bit big-endian mono 48 kHz audio, a name, the slot it goes to."""
    frames = len(audio_be) // 2
    payload = 64 + 2 * frames
    head = (MAGIC + bytes([2]) + struct.pack(">HH", 8, 13) + b"0094" + struct.pack(">I", 8) + b"\xFF\xFF\xFF\xFF"
            + struct.pack(">iI", slot, payload) + bytes([0, FOOTER]))
    nm = clean_name(name).encode("ascii")
    slot_head = b"SAMP" + bytes(4) + struct.pack(">I", frames) + nm + bytes(NAME_LEN - len(nm)) + bytes(36)
    body = slot_head + audio_be[:2 * frames]
    crc = zlib.crc32(body, 0xFFFFFFFF) & 0xFFFFFFFF
    return head + body + struct.pack(">I", crc) + struct.pack(">I", payload) + TAIL


def to_syntakt_audio(path: str) -> tuple[bytes, list[str]]:
    """Any audio file → 48 kHz 16-bit mono big-endian, and notes on what changed (shown amber in the Send plan)."""
    import numpy as np
    import soundfile as sf
    import soxr
    data, rate = sf.read(path, dtype="float64", always_2d=True)
    notes = []
    if data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
        notes.append("stereo folded to mono")
    mono = data[:, 0]
    if rate != RATE:
        mono = soxr.resample(mono, rate, RATE, quality="VHQ")
        notes.append(f"{rate / 1000:g} kHz → 48 kHz")
    info = sf.info(path)
    if info.subtype not in ("PCM_16",):
        notes.append(f"{info.subtype.replace('PCM_', '')}-bit → 16-bit" if info.subtype.startswith("PCM_")
                     else f"{info.subtype} → 16-bit")
    pcm = np.clip(np.round(mono * 32767), -32768, 32767).astype(">i2")
    return pcm.tobytes(), notes


def audio_to_wav(audio_be: bytes, path: str):
    """Save a Syntakt sample's audio as a plain WAV (48 kHz 16-bit mono), for audition or Get from device."""
    import wave
    import numpy as np
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.frombuffer(audio_be, dtype=">i2").astype("<i2").tobytes())


# ------------------------------------------------------------------ the connection
class Elektron:
    """Talks to the Syntakt through a transport with send(bytes) and a queue q of complete SysEx messages."""

    def __init__(self, transport, timeout: float = 5.0):
        self.t = transport
        self.timeout = timeout
        self.seq = 0
        self.lock = threading.RLock()

    def cmd(self, cmd: int, args: bytes = b"", timeout: float | None = None) -> bytes:
        with self.lock:
            self.seq = (self.seq + 1) & 0xFFFF
            seq = self.seq
            while True:                       # drop anything left over from before
                try:
                    self.t.q.get_nowait()
                except queue.Empty:
                    break
            self.t.send(HDR + encode(bytes([seq >> 8, seq & 0xFF, 0, 0, cmd]) + bytes(args)) + b"\xF7")
            end = time.time() + (timeout or self.timeout)
            while time.time() < end:
                try:
                    m = self.t.q.get(timeout=max(0.01, end - time.time()))
                except queue.Empty:
                    break
                if not m.startswith(HDR) or len(m) < 12:
                    continue
                r = decode(m[6:-1])
                if len(r) >= 6 and (r[2] << 8 | r[3]) == seq and r[4] == (cmd | 0x80):
                    return r
            raise SyntaktError(f"the Syntakt did not answer (command {cmd:02X})")

    @staticmethod
    def _ok(r: bytes, what: str) -> bytes:
        if len(r) > 5 and not r[5]:
            text = r[6:].split(b"\0")[0].decode("latin-1", "replace")
            raise SyntaktError(f"{what} refused{': ' + text if text else ''}")
        return r

    def ping(self) -> dict:
        r = self.cmd(0x01)
        n = r[6] if len(r) > 6 else 0
        return {"id": r[5], "name": r[7 + n:].split(b"\0")[0].decode("latin-1", "replace")}

    def version(self) -> str:
        r = self.cmd(0x02)
        return r[10:].split(b"\0")[0].decode("latin-1", "replace")

    def samples(self) -> list[dict]:
        """[{'slot', 'name', 'size'}] for every used slot."""
        r = self._ok(self.cmd(0x53, b"/samples\0" + struct.pack(">IIB", 0, 0, 1)), "listing the samples")
        return [{"slot": e["id"], "name": e["name"], "size": e["size"]} for e in parse_list(r) if not e["dir"]]

    def read(self, slot: int, progress=None) -> bytes:
        path = f"/samples/{slot}".encode() + b"\0"
        r = self._ok(self.cmd(0x54, path + struct.pack(">IB", BLOCK, 0)), f"reading slot {slot}")
        jid, buf, seq = struct.unpack(">I", r[6:10])[0], bytearray(), 0
        try:
            while True:
                r = self._ok(self.cmd(0x55, struct.pack(">II", jid, seq), timeout=8.0), f"reading slot {slot}")
                size = struct.unpack(">I", r[23:27])[0]
                buf.extend(r[27:27 + size])
                if progress:
                    progress(len(buf))
                if r[18]:
                    break
                seq += 1
                time.sleep(0.02)
        finally:
            self.cmd(0x56, struct.pack(">I", jid))
        return bytes(buf)

    def write(self, slot: int, data: bytes, progress=None):
        """Write a whole sample (build_sample's bytes); the footer goes in a block of its own."""
        path = f"/samples/{slot}".encode() + b"\0"
        r = self._ok(self.cmd(0x57, struct.pack(">I", len(data)) + path), f"writing slot {slot}")
        jid = struct.unpack(">I", r[6:10])[0]
        time.sleep(0.05)
        body, foot = data[:-FOOTER], data[-FOOTER:]
        chunks = [body[o:o + BLOCK] for o in range(0, len(body), BLOCK)] + [foot]
        sent = 0
        for seq, chunk in enumerate(chunks):
            crc = zlib.crc32(chunk, 0xFFFFFFFF) & 0xFFFFFFFF
            self._ok(self.cmd(0x58, struct.pack(">IIII", jid, seq, crc, len(chunk)) + chunk, timeout=8.0),
                     f"writing slot {slot}")
            sent += len(chunk)
            if progress:
                progress(sent)
            time.sleep(0.05)
        r = self._ok(self.cmd(0x59, struct.pack(">II", jid, len(data))), f"finishing slot {slot}")
        got = struct.unpack(">I", r[10:14])[0] if len(r) >= 14 else len(data)
        if got != len(data):
            raise SyntaktError(f"the Syntakt stored {got} bytes, not {len(data)}")

    def clear(self, slot: int):
        self._ok(self.cmd(0x5C, f"/samples/{slot}".encode() + b"\0"), f"clearing slot {slot}")
