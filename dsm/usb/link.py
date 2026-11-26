"""Talk to the Deluge's SD card over its USB cable (Community Firmware 1.3+ SysEx file commands).

Protocol, as checked on Nigel's Deluge on 19 Nov 2026 (project note deluge-bench-test/usb-file-protocol.md):
  request  F0 00 21 7B 01 04 <msgId> {json} [00 <7-bit packed data>] F7
  reply    F0 00 21 7B 01 05 <msgId> {json} [00 <packed data>] F7
  ping     F0 00 21 7B 01 00 00 F7  ->  F0 00 21 7B 01 7F 00 F7
Commands: session, dir, open, read, write, close, delete, mkdir, rename, move. File data goes in 512-byte
blocks (larger writes have lost bytes). Every call here blocks until the reply arrives; one call at a
time (a lock keeps the app's background jobs from interleaving).
"""
from __future__ import annotations

import json
import logging
import queue
import re
import sys
import threading
import time

log = logging.getLogger("dsm.usb")

HDR = bytes([0xF0, 0x00, 0x21, 0x7B, 0x01])
REQ, REPLY, PING, PONG = 0x04, 0x05, 0x00, 0x7F
BLOCK = 512


class UsbError(RuntimeError):
    pass


def pack7(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 7):
        chunk = data[i:i + 7]
        hi = 0
        for j, b in enumerate(chunk):
            if b & 0x80:
                hi |= 1 << j
        out.append(hi)
        out.extend(b & 0x7F for b in chunk)
    return bytes(out)


def unpack7(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 8):
        hi = data[i]
        for j, b in enumerate(data[i + 1:i + 8]):
            out.append(b | (0x80 if hi & (1 << j) else 0))
    return bytes(out)


def encode(cmd: str, args: dict, mid: int, data: bytes | None = None) -> bytes:
    text = json.dumps({cmd: args}, separators=(",", ":"), ensure_ascii=True)
    msg = HDR + bytes([REQ, mid]) + text.encode("ascii")
    if data is not None:
        msg += b"\x00" + pack7(data)
    return msg + b"\xF7"


def decode(m: bytes):
    """Reply message -> (msgId, dict, data bytes)."""
    body = bytes(m[7:-1])
    cut = body.find(b"\x00")
    text_b, data = (body, b"") if cut < 0 else (body[:cut], unpack7(body[cut + 1:]))
    text = text_b.decode("latin-1")
    obj = None
    for t in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        try:
            obj = json.loads(t)
            break
        except ValueError:
            pass
    return m[6], obj or {}, data


def fat_time(date: int, tm: int) -> float:
    """FAT date and time words -> seconds since the epoch (local time)."""
    try:
        y, mo, d = 1980 + (date >> 9), (date >> 5) & 15, date & 31
        h, mi, s = tm >> 11, (tm >> 5) & 63, (tm & 31) * 2
        return time.mktime((y, max(mo, 1), max(d, 1), h, mi, s, 0, 0, -1))
    except (OverflowError, ValueError):
        return 0.0


# ------------------------------------------------------------------ transports
class MidiTransport:
    """rtmidi output; input through winmm big buffers on Windows (proven in the bench test), rtmidi elsewhere."""

    def __init__(self, in_name: str, out_name: str):
        import rtmidi
        self.q: queue.Queue = queue.Queue()
        self.inp = None
        outs = _rtmidi_outs()
        self.out = rtmidi.MidiOut()
        self.out.open_port(outs.index(out_name))
        if sys.platform == "win32":
            from dsm.usb import winmm_in
            ins = winmm_in.input_names()
            self.inp = winmm_in.WinMMIn(ins.index(in_name), on_sysex=lambda m: self.q.put(m), on_raw=lambda n: None,
                                        on_short=lambda m: None, on_error=lambda e: log.warning("MIDI in: %s", e))
        else:
            from dsm.usb.winmm_in import Reassembler
            self.inp = rtmidi.MidiIn()
            self.inp.ignore_types(sysex=False, timing=True, active_sense=True)
            self.inp.open_port(_rtmidi_ins().index(in_name))
            ra = Reassembler()

            def cb(ev, _=None):
                for m in ra.feed(bytes(ev[0])):
                    self.q.put(m)
            self.inp.set_callback(cb)
        self.name = f"in '{in_name}' · out '{out_name}'"

    def send(self, msg: bytes):
        self.out.send_message(list(msg))

    def close(self):
        try:
            if hasattr(self.inp, "closing"):
                self.inp.close()
            elif self.inp is not None:
                self.inp.close_port()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.out.close_port()
        except Exception:  # noqa: BLE001
            pass


def _rtmidi_outs():
    import rtmidi
    o = rtmidi.MidiOut()
    try:
        return o.get_ports()
    finally:
        o.delete()


def _rtmidi_ins():
    import rtmidi
    i = rtmidi.MidiIn()
    try:
        return i.get_ports()
    finally:
        i.delete()


def deluge_port_pairs():
    """Every (input, output) pair whose names mention the Deluge."""
    try:
        outs = _rtmidi_outs()
        if sys.platform == "win32":
            from dsm.usb import winmm_in
            ins = winmm_in.input_names()
        else:
            ins = _rtmidi_ins()
    except Exception as e:  # noqa: BLE001 — no MIDI system at all
        raise UsbError(f"MIDI isn't available on this PC: {e}") from e
    din = [n for n in ins if "deluge" in n.lower()]
    dout = [n for n in outs if "deluge" in n.lower()]
    return [(i, o) for o in dout for i in din]


# ------------------------------------------------------------------ the link
class DelugeLink:
    def __init__(self, transport, timeout: float = 5.0):
        self.t = transport
        self.timeout = timeout
        self.lock = threading.RLock()
        self.mid_lo, self.mid_hi, self.seq = 1, 7, 0
        self.version = ""

    # ---- low level
    def _next_mid(self):
        span = self.mid_hi - self.mid_lo + 1
        self.seq = (self.seq + 1) % span
        return self.mid_lo + self.seq

    def _drain(self):
        try:
            while True:
                self.t.q.get_nowait()
        except queue.Empty:
            pass

    def ping(self, timeout: float = 1.0) -> bool:
        with self.lock:
            self._drain()
            self.t.send(HDR + bytes([PING, 0x00, 0xF7]))
            end = time.time() + timeout
            while time.time() < end:
                try:
                    m = self.t.q.get(timeout=max(0.01, end - time.time()))
                except queue.Empty:
                    break
                if m[:6] == HDR + bytes([PONG]):
                    return True
            return False

    def request(self, cmd: str, args: dict, data: bytes | None = None, timeout: float | None = None):
        """-> (reply body dict, data bytes). Raises UsbError on timeout or a FatFs error."""
        with self.lock:
            mid = self._next_mid()
            self.t.send(encode(cmd, args, mid, data))
            end = time.time() + (timeout or self.timeout)
            while True:
                left = end - time.time()
                if left <= 0:
                    raise UsbError(f"The Deluge didn't answer '{cmd}'. Is it still connected?")
                try:
                    m = self.t.q.get(timeout=left)
                except queue.Empty:
                    continue
                if len(m) < 8 or m[:5] != HDR or m[5] not in (REQ, REPLY):
                    continue
                rid, obj, d = decode(m)
                if rid != mid:
                    continue
                body = obj.get("^" + cmd) or obj.get(cmd) or {}
                return body, d

    def _ok(self, cmd, args, data=None, what=""):
        body, d = self.request(cmd, args, data)
        err = body.get("err", 0)
        if err:
            raise UsbError(f"{what or cmd} failed on the Deluge (error {err}{_FR.get(err, '')})")
        return body, d

    # ---- session and listing
    def start_session(self):
        body, _ = self.request("session", {"tag": "SampleManager"})
        lo, hi = body.get("midMin"), body.get("midMax")
        if isinstance(lo, int) and isinstance(hi, int) and 0 < lo <= hi < 128:
            self.mid_lo, self.mid_hi = lo, hi

    def listdir(self, path: str) -> list[dict]:
        out, offset = [], 0
        while True:
            body, _ = self.request("dir", {"path": path, "offset": offset, "lines": 20})
            items = body.get("list") or []
            out.extend(items)
            if body.get("err"):
                if body.get("err") in (4, 5):
                    raise FileNotFoundError(path)
                raise UsbError(f"Listing {path} failed (error {body.get('err')})")
            if len(items) < 20:
                return out
            offset += len(items)

    # ---- files
    def open(self, path: str, write: bool = False):
        body, _ = self._ok("open", {"path": path, "write": 1 if write else 0}, what=f"Opening {path}")
        return body.get("fid"), body.get("size", 0)

    def close(self, fid):
        try:
            self.request("close", {"fid": fid})
        except UsbError:
            pass

    def read_at(self, fid, addr: int, size: int) -> bytes:
        body, d = self._ok("read", {"fid": fid, "addr": addr, "size": size}, what="Reading")
        return d[: body.get("size", len(d))] if isinstance(body.get("size"), int) else d

    def read(self, path: str, progress=None) -> bytes:
        with self.lock:
            fid, size = self.open(path)
            buf = bytearray()
            try:
                while len(buf) < size:
                    chunk = self.read_at(fid, len(buf), min(BLOCK, size - len(buf)))
                    if not chunk:
                        raise UsbError(f"Reading {path} stopped at {len(buf)} of {size} bytes")
                    buf.extend(chunk)
                    if progress:
                        progress(len(buf), size)
            finally:
                self.close(fid)
            return bytes(buf)

    def write(self, path: str, data: bytes, progress=None):
        with self.lock:
            fid, _ = self.open(path, write=True)
            pos = 0
            try:
                while pos < len(data):
                    chunk = data[pos:pos + BLOCK]
                    body, _ = self._ok("write", {"fid": fid, "addr": pos, "size": len(chunk)}, data=chunk,
                                       what=f"Writing {path}")
                    if body.get("size") != len(chunk):
                        raise UsbError(f"Writing {path} stopped at {pos} bytes")
                    pos += len(chunk)
                    if progress:
                        progress(pos, len(data))
            finally:
                self.close(fid)

    def move(self, src: str, dst: str):
        self._ok("move", {"from": src, "to": dst}, what=f"Moving {src}")

    def rename(self, src: str, dst: str):
        self._ok("rename", {"from": src, "to": dst}, what=f"Renaming {src}")

    def delete(self, path: str):
        self._ok("delete", {"path": path}, what=f"Removing {path}")

    def mkdir(self, path: str):
        body, _ = self.request("mkdir", {"path": path})
        if body.get("err") not in (0, None, 8):          # 8 = FR_EXIST
            raise UsbError(f"Making folder {path} failed (error {body.get('err')})")

    def close_link(self):
        self.t.close()


_FR = {4: ": no such file", 5: ": no such folder", 7: ": not allowed", 8: ": already exists",
       3: ": card not ready", 1: ": card read/write error"}


def connect() -> DelugeLink:
    """Find the Deluge on USB: try every Deluge-named port pair and keep the one that answers a ping."""
    pairs = deluge_port_pairs()
    if not pairs:
        raise UsbError("No Deluge MIDI ports found. Check the Deluge is plugged into the PC with its USB cable and "
                       "switched on.")
    problems = []
    for i, o in pairs:
        try:
            t = MidiTransport(i, o)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{i} / {o}: {e}")
            continue
        link = DelugeLink(t)
        if link.ping():
            link.start_session()
            log.info("Deluge found on %s", t.name)
            link.port_name = t.name
            return link
        t.close()
    hint = (" Another program may have the Deluge's ports open — close the Patch Manager or any DAW and try again."
            if problems else " The Deluge needs Community Firmware 1.3 or later.")
    raise UsbError("The Deluge didn't answer on USB." + hint + ("\n\n" + "\n".join(problems[:4]) if problems else ""))
