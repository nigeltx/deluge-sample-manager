"""Windows MIDI input with big SysEx buffers, straight from winmm.dll.

python-rtmidi on Windows receives SysEx through 1 KB buffers and, with the Sub 37, stalls after the
first 1,024 bytes of an ALL PRESETS dump. This reader gives the driver 32 buffers of 64 KB (2 MB in
all). The driver calls back on its own thread; the callback only copies the bytes out and hands the
buffer to a worker thread, which re-queues it. Every winmm call is checked and errors are reported.
"""
import ctypes
import queue
import sys
import threading

CALLBACK_FUNCTION = 0x00030000
MIM_OPEN, MIM_CLOSE, MIM_DATA, MIM_LONGDATA, MIM_ERROR, MIM_LONGERROR, MIM_MOREDATA = (
    0x3C1, 0x3C2, 0x3C3, 0x3C4, 0x3C5, 0x3C6, 0x3CC)
N_BUFFERS, BUF_SIZE = 64, 32768        # each buffer must stay under 64 KB


class MIDIHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", ctypes.c_uint32),
                ("dwBytesRecorded", ctypes.c_uint32), ("dwUser", ctypes.c_size_t),
                ("dwFlags", ctypes.c_uint32), ("lpNext", ctypes.c_void_p),
                ("reserved", ctypes.c_size_t), ("dwOffset", ctypes.c_uint32),
                ("dwReserved", ctypes.c_size_t * 8)]


class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [("wMid", ctypes.c_ushort), ("wPid", ctypes.c_ushort), ("vDriverVersion", ctypes.c_uint),
                ("szPname", ctypes.c_wchar * 32), ("dwSupport", ctypes.c_uint32)]


def available():
    return sys.platform == "win32"


def _mm():
    mm = ctypes.WinDLL("winmm")
    vp, u, sz = ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t
    mm.midiInGetNumDevs.restype = u
    mm.midiInGetDevCapsW.argtypes = [sz, vp, u]
    mm.midiInOpen.argtypes = [ctypes.POINTER(vp), u, sz, sz, ctypes.c_uint32]
    for f in ("midiInPrepareHeader", "midiInUnprepareHeader", "midiInAddBuffer"):
        getattr(mm, f).argtypes = [vp, vp, u]
    for f in ("midiInStart", "midiInStop", "midiInReset", "midiInClose"):
        getattr(mm, f).argtypes = [vp]
    mm.midiInGetErrorTextW.argtypes = [u, ctypes.c_wchar_p, u]
    return mm


def input_names():
    mm = _mm()
    names = []
    for i in range(mm.midiInGetNumDevs()):
        caps = MIDIINCAPSW()
        if mm.midiInGetDevCapsW(i, ctypes.addressof(caps), ctypes.sizeof(caps)) == 0:
            names.append(caps.szPname)
        else:
            names.append(f"MIDI input {i}")
    return names


class Reassembler:
    """Joins raw SysEx bytes (split across buffers in any way) into complete F0..F7 messages."""

    def __init__(self):
        self.pending = bytearray()

    def feed(self, chunk):
        out = []
        start = 0
        while True:
            if not self.pending:
                s = chunk.find(b"\xF0", start)
                if s < 0:
                    return out
                start = s
            e = chunk.find(b"\xF7", start)
            if e < 0:
                self.pending += chunk[start:]
                return out
            s2 = chunk.rfind(b"\xF0", start + (0 if self.pending else 1), e)
            if s2 >= 0:                     # a new message started before this one ended: drop the broken one
                self.pending = bytearray(); start = s2
                continue
            self.pending += chunk[start:e + 1]
            out.append(bytes(self.pending)); self.pending = bytearray()
            start = e + 1


class WinMMIn:
    """on_sysex(bytes) for each complete message, on_raw(n) as SysEx bytes arrive,
    on_short(bytes) for short messages, on_error(str) for anything that goes wrong."""

    def __init__(self, index, on_sysex, on_raw, on_short, on_error):
        self.mm = _mm()
        self.on_sysex, self.on_raw, self.on_short, self.on_error = on_sysex, on_raw, on_short, on_error
        self.events = queue.Queue()
        self.closing = False
        self.stats = {"long": 0, "short": 0, "errors": 0, "bytes": 0}
        self.re = Reassembler()
        proto = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t)
        self._cb = proto(self._callback)          # keep a reference or Windows calls freed memory
        self.h = ctypes.c_void_p()
        self._chk(self.mm.midiInOpen(ctypes.byref(self.h), index, ctypes.cast(self._cb, ctypes.c_void_p).value, 0,
                                     CALLBACK_FUNCTION), "midiInOpen")
        self.bufs, self.hdrs = [], {}
        for _ in range(N_BUFFERS):
            buf = ctypes.create_string_buffer(BUF_SIZE)
            hdr = MIDIHDR(); hdr.lpData = ctypes.addressof(buf); hdr.dwBufferLength = BUF_SIZE
            self._chk(self.mm.midiInPrepareHeader(self.h, ctypes.addressof(hdr), ctypes.sizeof(hdr)), "midiInPrepareHeader")
            self._chk(self.mm.midiInAddBuffer(self.h, ctypes.addressof(hdr), ctypes.sizeof(hdr)), "midiInAddBuffer")
            self.bufs.append(buf); self.hdrs[ctypes.addressof(hdr)] = hdr
        self.worker = threading.Thread(target=self._work, daemon=True); self.worker.start()
        self._chk(self.mm.midiInStart(self.h), "midiInStart")

    def _err(self, code):
        b = ctypes.create_unicode_buffer(256)
        self.mm.midiInGetErrorTextW(code, b, 256)
        return b.value or f"error {code}"

    def _chk(self, r, what):
        if r != 0:
            raise RuntimeError(f"{what} failed: {self._err(r)} (code {r})")

    def _callback(self, h, msg, inst, p1, p2):       # runs on the driver's thread: copy and hand off only
        try:
            if msg in (MIM_LONGDATA, MIM_LONGERROR):
                hdr = MIDIHDR.from_address(p1)
                n = hdr.dwBytesRecorded
                self.events.put(("long", p1, ctypes.string_at(hdr.lpData, n) if n else b"", msg == MIM_LONGERROR))
            elif msg in (MIM_DATA, MIM_MOREDATA):
                self.events.put(("short", p1))
            elif msg == MIM_ERROR:
                self.events.put(("error", p1))
        except Exception as e:                      # never let an exception escape into the driver
            self.events.put(("fault", repr(e)))

    def _work(self):
        while True:
            try:
                ev = self.events.get(timeout=0.2)
            except queue.Empty:
                if self.closing:
                    return
                continue
            try:
                kind = ev[0]
                if kind == "long":
                    _, addr, data, broken = ev
                    self.stats["long"] += 1
                    if broken:
                        self.stats["errors"] += 1; self.on_error("the driver reported a damaged SysEx buffer")
                    if data:
                        self.stats["bytes"] += len(data); self.on_raw(len(data))
                        for m in self.re.feed(data):
                            self.on_sysex(m)
                    if not self.closing and addr in self.hdrs:
                        hdr = self.hdrs[addr]; hdr.dwBytesRecorded = 0
                        r = self.mm.midiInAddBuffer(self.h, ctypes.addressof(hdr), ctypes.sizeof(hdr))
                        if r:
                            self.on_error(f"midiInAddBuffer failed: {self._err(r)}")
                elif kind == "short":
                    self.stats["short"] += 1
                    p = ev[1]
                    st = p & 0xFF
                    n = 1 if st >= 0xF8 else (2 if (st & 0xE0) == 0xC0 or st in (0xF1, 0xF3) else 3)
                    self.on_short(bytes([(p >> (8 * i)) & 0xFF for i in range(n)]))
                elif kind == "error":
                    self.stats["errors"] += 1; self.on_error(f"the driver reported a bad short message {ev[1]:#x}")
                elif kind == "fault":
                    self.stats["errors"] += 1; self.on_error("callback fault: " + ev[1])
            except Exception as e:
                self.on_error("input worker: " + repr(e))

    def close(self):
        self.closing = True
        try:
            self.mm.midiInStop(self.h)
            self.mm.midiInReset(self.h)                 # hands back every queued buffer
            for hdr in self.hdrs.values():
                self.mm.midiInUnprepareHeader(self.h, ctypes.addressof(hdr), ctypes.sizeof(hdr))
            self.mm.midiInClose(self.h)
        finally:
            self.worker.join(timeout=1)
