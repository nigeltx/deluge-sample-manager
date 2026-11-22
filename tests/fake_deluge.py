"""A pretend Deluge for tests: answers the USB file commands from a folder on the PC, the way
Community Firmware 1.3 does (FatFs error codes, 20-line directory pages, 7-bit packed data)."""
from __future__ import annotations

import json
import os
import queue
import shutil
import time

from dsm.usb.link import HDR, PING, PONG, REPLY, pack7, unpack7


def fat_words(t: float):
    lt = time.localtime(t)
    date = ((lt.tm_year - 1980) << 9) | (lt.tm_mon << 5) | lt.tm_mday
    tm = (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)
    return date, tm


class FakeDeluge:
    def __init__(self, root: str, max_block: int = 1024):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.fids: dict[int, tuple[str, bool]] = {}
        self.next_fid = 1
        self.max_block = max_block
        self.sent = 0
        self.closed = False

    def _p(self, path):
        return os.path.join(self.root, *[x for x in path.split("/") if x])

    def send(self, msg: bytes):
        self.sent += 1
        msg = bytes(msg)
        if msg[5] == PING:
            self.q.put(HDR + bytes([PONG, 0x00, 0xF7]))
            return
        mid = msg[6]
        body = msg[7:-1]
        cut = body.find(b"\x00")
        text, data = (body, b"") if cut < 0 else (body[:cut], unpack7(body[cut + 1:]))
        (cmd, args), = json.loads(text).items()
        reply, out = getattr(self, "c_" + cmd)(args, data)
        msg = HDR + bytes([REPLY, mid]) + json.dumps({"^" + cmd: reply}, indent=1).encode()
        if out is not None:
            msg += b"\x00" + pack7(out)
        self.q.put(msg + b"\xF7")

    def close(self):
        self.closed = True

    # commands -------------------------------------------------------------
    def c_session(self, a, d):
        return {"sid": 1, "midMin": 9, "midMax": 15}, None

    def c_dir(self, a, d):
        p = self._p(a["path"])
        if not os.path.isdir(p):
            return {"err": 5}, None
        names = sorted(os.listdir(p))
        items = []
        for n in names[a.get("offset", 0): a.get("offset", 0) + a.get("lines", 20)]:
            fp = os.path.join(p, n)
            st = os.stat(fp)
            date, tm = fat_words(st.st_mtime)
            items.append({"name": n, "size": 0 if os.path.isdir(fp) else st.st_size, "date": date, "time": tm,
                          "attr": 0x10 if os.path.isdir(fp) else 0x20})
        return {"list": items, "err": 0}, None

    def c_open(self, a, d):
        p = self._p(a["path"])
        if a.get("write"):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").close()
        elif not os.path.isfile(p):
            return {"err": 4}, None
        fid = self.next_fid
        self.next_fid += 1
        self.fids[fid] = (p, bool(a.get("write")))
        return {"fid": fid, "size": os.path.getsize(p), "err": 0}, None

    def c_read(self, a, d):
        p, _ = self.fids[a["fid"]]
        with open(p, "rb") as f:
            f.seek(a["addr"])
            data = f.read(min(a["size"], self.max_block))
        return {"fid": a["fid"], "addr": a["addr"], "size": len(data), "err": 0}, data

    def c_write(self, a, d):
        p, w = self.fids[a["fid"]]
        with open(p, "r+b") as f:
            f.seek(a["addr"])
            f.write(d)
        return {"fid": a["fid"], "addr": a["addr"], "size": len(d), "err": 0}, None

    def c_close(self, a, d):
        self.fids.pop(a["fid"], None)
        return {"fid": a["fid"], "err": 0}, None

    def c_delete(self, a, d):
        p = self._p(a["path"])
        if os.path.isdir(p):
            if os.listdir(p):
                return {"err": 7}, None
            os.rmdir(p)
        elif os.path.isfile(p):
            os.remove(p)
        else:
            return {"err": 4}, None
        return {"err": 0}, None

    def c_mkdir(self, a, d):
        p = self._p(a["path"])
        if os.path.exists(p):
            return {"err": 8}, None
        os.mkdir(p)
        return {"err": 0}, None

    def c_rename(self, a, d):
        s, t = self._p(a["from"]), self._p(a["to"])
        if not os.path.exists(s):
            return {"err": 4}, None
        if os.path.exists(t) and os.path.normcase(s) != os.path.normcase(t):
            return {"err": 8}, None
        os.rename(s, t)
        return {"err": 0}, None

    def c_move(self, a, d):
        t = self._p(a["to"])
        os.makedirs(os.path.dirname(t), exist_ok=True)
        r = self.c_rename(a, d)
        return r
