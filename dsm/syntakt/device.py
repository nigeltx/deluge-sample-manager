"""The Syntakt as a device (Sample Manager 0.12, spec v2.3 section 4.3).

What the app does with the Syntakt's 64 sample slots:
- **List** them with names and sizes, by type, matched to the library.
- **Read** any of them to the PC, for audition and Get from device (a cache keeps what was read).
- **Twin Shot check**: compare the slots with Factory › Syntakt in the library — complete, or what differs.
- **Send** a converted copy (48 kHz 16-bit mono, name cut to 16 characters) into an empty slot, or a slot holding a
  sample the app itself put there; **Remove** only samples the app put there. Factory samples are locked, and the
  app never touches a sample it did not put on the Syntakt (samples are global: every project using one would change).
- Every Send and Remove first saves what was in the slot to Backups › Syntakt, then reads the new sample back to
  check it, and is recorded in a journal so **Undo** can put the slot back. The Syntakt must be restarted before it
  plays samples written to it.

The record of samples the app put on the Syntakt is _Sample Manager/syntakt.json in the library (slot → library
file, name, size, when), so it syncs with the library.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from dsm.library import layout
from dsm.syntakt import proto

RECORD = "syntakt.json"


@dataclass
class Slot:
    slot: int
    name: str
    size: int                     # payload: 64 + 2 × frames
    match: str | None = None      # library rel of the sample this slot holds, when known
    how: str = ""                 # "sent by the app" / "factory (Twin Shot)" / "name" / ""

    @property
    def frames(self) -> int:
        return max(0, (self.size - 64) // 2)

    @property
    def seconds(self) -> float:
        return self.frames / proto.RATE


def expected_frames(frames: int, rate: int) -> int:
    """How many frames a sample of `frames` at `rate` has once the Syntakt holds it at 48 kHz."""
    return round(frames * proto.RATE / rate) if rate else frames


def name_key(name: str) -> str:
    stem = os.path.splitext(name.rsplit("/", 1)[-1].split("::")[-1])[0]
    return proto.clean_name(stem)


# ------------------------------------------------------------------ the record of what the app put there
def load_record(library_root: str) -> dict:
    p = os.path.join(layout.app_dir(library_root), RECORD)
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"slots": {}}


def save_record(library_root: str, rec: dict):
    d = layout.app_dir(library_root)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, RECORD + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=1)
    os.replace(tmp, os.path.join(d, RECORD))


# ------------------------------------------------------------------ matching slots to the library
def match_slots(slots: list[Slot], factory_rows, record: dict, library_rows=()):
    """Fill in Slot.match/how. factory_rows: library rows of Factory › Syntakt (rel, name, frames, rate).
    A slot the app sent is matched through the record (name and size must still agree); otherwise a factory sample
    with the same name (as the Syntakt would show it) and the same length at 48 kHz; otherwise any library sample with
    that name and length."""
    fac = {}
    for r in factory_rows:
        fac.setdefault(name_key(r["name"]), []).append(r)
    other = {}
    for r in library_rows:
        other.setdefault(name_key(r["name"]), []).append(r)
    sent = record.get("slots", {})
    for s in slots:
        e = sent.get(str(s.slot))
        if e and e.get("name") == s.name and e.get("size") == s.size:
            s.match, s.how = e.get("library"), "sent by the app"
            continue
        for pool, how in ((fac, "factory (Twin Shot)"), (other, "same name and length")):
            hit = next((r for r in pool.get(proto.clean_name(s.name), [])
                        if abs(expected_frames(r["frames"] or 0, r["rate"] or proto.RATE) - s.frames) <= 1), None)
            if hit:
                s.match, s.how = hit["rel"], how
                break
    return slots


def twin_check(slots: list[Slot], factory_rows) -> dict:
    """Compare the Syntakt with Factory › Syntakt: {'complete': bool, 'missing': [names], 'added': [slot names],
    'text': one line}. A factory sample counts as there when a slot holds it (same name, same length at 48 kHz)."""
    have = {s.match for s in slots if s.how.startswith("factory")}
    missing = sorted(os.path.splitext(r["name"])[0] for r in factory_rows if r["rel"] not in have)
    added = [f"{s.slot} {s.name}" for s in slots if not s.how.startswith("factory")]
    complete = not missing and bool(factory_rows)
    if not factory_rows:
        text = "no Factory › Syntakt samples in the library to compare with"
    elif complete and not added:
        text = f"Twin Shot pack complete ({len(factory_rows)} samples)"
    else:
        parts = []
        if missing:
            parts.append(f"{len(missing)} factory sample{'s' if len(missing) != 1 else ''} missing")
        if added:
            parts.append(f"{len(added)} other sample{'s' if len(added) != 1 else ''}")
        text = ("Twin Shot pack complete, plus " if complete else "Twin Shot pack incomplete: ") + ", ".join(parts)
    return {"complete": complete, "missing": missing, "added": added, "text": text}


def locked(s: Slot | None) -> str:
    """Why the app won't write over or remove this slot ('' when it may)."""
    if s is None:
        return ""
    if s.how == "sent by the app":
        return ""
    if s.how.startswith("factory"):
        return "a factory sample (locked)"
    return "a sample the app did not put there"


def free_slots(slots: list[Slot]) -> list[int]:
    used = {s.slot for s in slots}
    return [n for n in range(1, proto.SLOTS + 1) if n not in used]


# ------------------------------------------------------------------ the device session
class Syntakt:
    """One connection. All calls block: run them in a background job."""

    def __init__(self, el: proto.Elektron, transport=None, cache_dir: str = ""):
        self.el, self.transport, self.cache_dir = el, transport, cache_dir
        self.version = ""
        self.slots: list[Slot] = []

    @classmethod
    def connect(cls, cache_dir: str):
        from dsm.usb.link import MidiTransport
        ins, outs = ports()
        if not ins or not outs:
            raise proto.SyntaktError("No Syntakt MIDI ports: is it on and connected by USB?")
        try:
            t = MidiTransport(ins[0], outs[0])
        except Exception as e:  # noqa: BLE001
            raise proto.SyntaktError(f"Could not open the Syntakt's MIDI ports — is the Patch Manager, Overbridge "
                                     f"or Elektron Transfer using them? ({e})") from e
        dev = cls(proto.Elektron(t), t, cache_dir)
        try:
            p = dev.el.ping()
            if p["id"] != proto.SYNTAKT_ID:
                raise proto.SyntaktError(f"That device answered as '{p['name']}', not a Syntakt")
            dev.version = dev.el.version()
        except Exception:
            dev.close()
            raise
        return dev

    def close(self):
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001
                pass
            self.transport = None

    def refresh(self) -> list[Slot]:
        self.slots = [Slot(e["slot"], e["name"], e["size"]) for e in self.el.samples()]
        return self.slots

    def slot(self, n: int) -> Slot | None:
        return next((s for s in self.slots if s.slot == n), None)

    # ---- reading
    def _cache_path(self, s: Slot, ext: str) -> str:
        safe = "".join(c if c.isalnum() else "_" for c in s.name)
        return os.path.join(self.cache_dir, f"{s.slot:02d} {safe} {s.size}{ext}")

    def read(self, s: Slot, progress=None) -> bytes:
        p = self._cache_path(s, ".bin")
        if os.path.exists(p):
            with open(p, "rb") as fh:
                return fh.read()
        data = self.el.read(s.slot, progress)
        info = proto.parse_sample(data)
        if not info["ok"]:
            raise proto.SyntaktError(f"slot {s.slot} read back damaged (sizes or checksum disagree)")
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        return data

    def wav(self, s: Slot, progress=None) -> str:
        """A WAV of the slot (48 kHz 16-bit mono) in the cache, for audition."""
        p = self._cache_path(s, ".wav")
        if not os.path.exists(p):
            proto.audio_to_wav(proto.parse_sample(self.read(s, progress))["audio"], p)
        return p

    # ---- writing (spec 4.3: backup, write, read back, journal)
    def _backup(self, s: Slot | None, backup_dir: str) -> str:
        if s is None:
            return ""
        data = self.el.read(s.slot)
        os.makedirs(backup_dir, exist_ok=True)
        p = os.path.join(backup_dir, f"slot {s.slot:02d} {s.name}.bin")
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def send(self, n: int, data: bytes, backup_dir: str, progress=None) -> str:
        """Write data (proto.build_sample) to slot n; returns the backup of what was there ('' if empty)."""
        before = self.slot(n)
        why = locked(before)
        if why:
            raise proto.SyntaktError(f"Slot {n} holds {why}")
        bk = self._backup(before, backup_dir)
        self.el.write(n, data, progress)
        if self.el.read(n) != data:
            raise proto.SyntaktError(f"slot {n} reads back different from what was sent")
        return bk

    def remove(self, n: int, backup_dir: str) -> str:
        s = self.slot(n)
        why = locked(s)
        if s is None:
            raise proto.SyntaktError(f"Slot {n} is already empty")
        if why:
            raise proto.SyntaktError(f"Slot {n} holds {why}")
        bk = self._backup(s, backup_dir)
        self.el.clear(n)
        return bk

    def put_back(self, n: int, backup: str):
        """Undo: write a backed-up slot back, or empty the slot if it was empty before."""
        if backup:
            with open(backup, "rb") as fh:
                data = fh.read()
            self.el.write(n, data)
            if self.el.read(n) != data:
                raise proto.SyntaktError(f"slot {n} reads back different from its backup")
        else:
            self.el.clear(n)


def ports() -> tuple[list[str], list[str]]:
    import sys

    from dsm.usb.link import _rtmidi_ins, _rtmidi_outs
    outs = [n for n in _rtmidi_outs() if "syntakt" in n.lower()]
    if sys.platform == "win32":
        from dsm.usb import winmm_in
        ins = [n for n in winmm_in.input_names() if "syntakt" in n.lower()]
    else:
        ins = [n for n in _rtmidi_ins() if "syntakt" in n.lower()]
    return ins, outs


# ------------------------------------------------------------------ the journal (Undo)
def journal_path(app_dir: str) -> str:
    return os.path.join(app_dir, "syntakt-journal.json")


def load_journal(app_dir: str) -> list[dict]:
    try:
        with open(journal_path(app_dir), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def add_journal(app_dir: str, entry: dict):
    j = load_journal(app_dir)
    j.append(dict(entry, when=time.strftime("%Y-%m-%d %H:%M:%S")))
    os.makedirs(app_dir, exist_ok=True)
    with open(journal_path(app_dir), "w", encoding="utf-8") as fh:
        json.dump(j[-200:], fh, indent=1)


def pop_journal(app_dir: str) -> dict | None:
    j = load_journal(app_dir)
    if not j:
        return None
    e = j.pop()
    with open(journal_path(app_dir), "w", encoding="utf-8") as fh:
        json.dump(j, fh, indent=1)
    return e
