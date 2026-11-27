"""A fake Syntakt answering the sample commands from a dict of slots, as the real one did in the bench test
(27 Nov 2026): list /samples, read /samples/<slot>, write with the footer in a block of its own, clear."""
import queue
import struct

from dsm.syntakt import proto


class FakeSyntakt:
    def __init__(self, slots=None):
        self.slots = dict(slots or {})          # slot -> bytes as the Syntakt stores them
        self.q = queue.Queue()
        self.jobs = {}
        self.next_job = 1
        self.writes = []

    def send(self, msg: bytes):
        r = proto.decode(msg[6:-1])
        seq, cmd, a = r[0:2], r[4], r[5:]
        ok, body = True, b""
        if cmd == 0x01:                          # the device id sits where the status byte would be
            reply = b"\0\0" + seq + bytes([cmd | 0x80, proto.SYNTAKT_ID, 2, 1, 2]) + b"Syntakt\0"
            self.q.put(proto.HDR + proto.encode(reply) + b"\xF7")
            return
        elif cmd == 0x02:
            body = b"\0" * 4 + b"1.41\0"
        elif cmd == 0x53:
            out = b""
            for n in sorted(self.slots):
                d = self.slots[n]
                name = proto.parse_sample(d)["name"].encode()
                out += name + b"\0" + bytes([0, 2]) + struct.pack(">II", n, len(d) - 43) + b"\x00\x7e\x01\x00"
            body = b"\0" * 12 + out
        elif cmd == 0x54:
            n = int(a.split(b"\0")[0].decode().rsplit("/", 1)[1])
            if n not in self.slots:
                ok, body = False, b"invalid sample slot\0"
            else:
                j = self._job(("r", self.slots[n]))
                body = struct.pack(">I", j)
        elif cmd == 0x55:
            jid, s = struct.unpack(">II", a[:8])
            data = self.jobs[jid][1]
            chunk = data[s * proto.BLOCK:(s + 1) * proto.BLOCK]
            last = (s + 1) * proto.BLOCK >= len(data)
            body = struct.pack(">II", jid, s) + bytes(4) + bytes([last]) + bytes(4) + struct.pack(">I", len(chunk)) + chunk
        elif cmd == 0x57:
            size = struct.unpack(">I", a[:4])[0]
            n = int(a[4:].split(b"\0")[0].decode().rsplit("/", 1)[1])
            j = self._job(("w", n, size, []))
            body = struct.pack(">I", j)
        elif cmd == 0x58:
            jid, s, crc, ln = struct.unpack(">IIII", a[:16])
            self.jobs[jid][3].append(a[16:16 + ln])
            body = struct.pack(">II", jid, s) + struct.pack(">I", sum(len(c) for c in self.jobs[jid][3]))
        elif cmd == 0x59:
            jid, size = struct.unpack(">II", a[:8])
            _, n, _, chunks = self.jobs.pop(jid)
            if len(chunks[-1]) != proto.FOOTER:              # the real one insists on the footer on its own
                ok, body = False, b"Footer was not processed\0"
            else:
                self.slots[n] = b"".join(chunks)
                self.writes.append(n)
                body = struct.pack(">II", jid, size)
        elif cmd == 0x5C:
            n = int(a.split(b"\0")[0].decode().rsplit("/", 1)[1])
            self.slots.pop(n, None)
        elif cmd == 0x56:
            self.jobs.pop(struct.unpack(">I", a[:4])[0], None)
        reply = b"\0\0" + seq + bytes([cmd | 0x80, 1 if ok else 0]) + body
        self.q.put(proto.HDR + proto.encode(reply) + b"\xF7")

    def _job(self, v):
        j = self.next_job
        self.next_job += 1
        self.jobs[j] = v
        return j

    def close(self):
        pass
