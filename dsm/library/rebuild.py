"""Rebuild Samples from Sources (spec 2.3).

Samples holds every sample found in Sources exactly once, as the original file (unpacked when it came in a zip),
in a flat level of type folders; loops sit in a Loops folder inside their type. plan() works out what Samples
should hold and compares it with what is there; nothing changes until apply(). A second rebuild changes nothing.

- add:     a sample with no file in Samples yet (copied from Sources, or unpacked from its zip)
- move:    a file whose type changed (Set type…) — the same file, a new folder
- remove:  a file whose sample is no longer in any source, or a second copy of a sample already in Samples
           → moved to _Sample Manager\\Removed\\<date>, never deleted
- hand:    a file you put in Samples yourself whose audio is in no source → moved to Sources\\User\\Added by hand,
           so Samples can always be rebuilt from Sources

The name comes from the copy that counts (Factory, Paid, Free, User order, db.wanted); clashes in one folder get
(2), (3) in a fixed order. A file already holding the right sample in the right folder is left alone, whatever
its name.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from dsm.library import layout
from dsm.library import sources as src
from dsm.library import types as stypes
from dsm.library.scanner import scan_samples

HAND_FOLDER = "Added by hand"
BAD_CHARS = '<>:"/\\|?*'

REBUILT = "CREATE TABLE IF NOT EXISTS rebuilt (rel TEXT PRIMARY KEY, audio TEXT)"


def safe_name(name: str) -> str:
    s = "".join("-" if (c in BAD_CHARS or ord(c) < 32) else c for c in (name or "")).strip(" .")
    return s[:120] or "Untitled"


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H%M%S")


@dataclass
class Want:
    audio: str
    source_rel: str          # the copy in Sources it comes from
    name: str                # file name, with extension
    folder: str              # Kick, or Kick/Loops


@dataclass
class Plan:
    adds: list = field(default_factory=list)        # (dst rel, Want)
    moves: list = field(default_factory=list)       # (src rel, dst rel, audio)
    removes: list = field(default_factory=list)     # (rel, why)
    hand: list = field(default_factory=list)        # (rel, dst rel)
    keep: dict = field(default_factory=dict)        # rel → audio, already right
    root: str = ""

    @property
    def empty(self):
        return not (self.adds or self.moves or self.removes or self.hand)

    def summary(self):
        bits = [(len(self.adds), "to add"), (len(self.moves), "to move to another type"),
                (len(self.removes), "to take out"), (len(self.hand), "you added by hand to keep in Sources › User")]
        return ", ".join(f"{n:,} {t}" for n, t in bits if n) or "nothing to change"


def wanted(db) -> list[Want]:
    out = []
    for audio, r in db.wanted().items():
        types, loop = db.types_for(audio, r["rel"])
        folder = layout.type_folder(types or [stypes.UNKNOWN])
        if loop:
            folder += "/" + layout.LOOPS
        out.append(Want(audio, r["rel"], src.base_name(r["rel"]), folder))
    return out


def targets(wants: list[Want]) -> dict[str, Want]:
    """rel in Samples (Samples/Kick/BD 01.wav) → Want. Two samples with one name in one folder get (2), (3) —
    always in the same order, so a rebuild is repeatable."""
    out, used = {}, set()
    for w in sorted(wants, key=lambda w: (w.folder.lower(), w.name.lower(), w.audio)):
        stem, ext = os.path.splitext(safe_name(w.name))
        n = 1
        while True:
            fname = (stem if n == 1 else f"{stem} ({n})") + ext
            key = f"{layout.SAMPLES}/{w.folder}/{fname}"
            if key.lower() not in used:
                used.add(key.lower())
                out[key] = w
                break
            n += 1
    return out


def _samples_folder(root):
    return layout.find_child(root, layout.SAMPLES) or layout.SAMPLES


def plan(db, root: str) -> Plan:
    db.ex(REBUILT)
    scan_samples(db, root)
    pl = Plan(root=root)
    sname = _samples_folder(root)
    want = {k.replace(layout.SAMPLES + "/", sname + "/", 1): v for k, v in targets(wanted(db)).items()}
    want_lower = {k.lower(): k for k in want}
    disk = {r["rel"]: r["audio"] for r in db.q("SELECT rel, audio FROM sample WHERE area = 'Samples'")}
    rebuilt = {r["rel"].lower(): r["audio"] for r in db.q("SELECT * FROM rebuilt")}
    source_audios = db.source_audios()
    wanted_audio = {w.audio for w in want.values()}
    used, right = set(), set()
    # already right: the wanted sample in the wanted folder, under its name …
    for rel, a in disk.items():
        k = want_lower.get(rel.lower())
        if k and k not in right and want[k].audio == a:
            pl.keep[rel] = a
            used.add(rel)
            right.add(k)
    # … or under another name in the right folder
    by_folder = {}
    for k, w in want.items():
        if k not in right:
            by_folder.setdefault((os.path.dirname(k).lower(), w.audio), []).append(k)
    for rel, a in disk.items():
        if rel in used:
            continue
        ks = by_folder.get((os.path.dirname(rel).lower(), a))
        if ks:
            k = ks.pop(0)
            pl.keep[rel] = a
            used.add(rel)
            right.add(k)
    # moves for samples already in Samples elsewhere, adds for the rest
    by_audio = {}
    for rel, a in disk.items():
        if rel not in used and a:
            by_audio.setdefault(a, []).append(rel)
    for k, w in want.items():
        if k in right:
            continue
        there = by_audio.get(w.audio)
        if there:
            rel = there.pop(0)
            used.add(rel)
            pl.moves.append((rel, k, w.audio))
        else:
            pl.adds.append((k, w))
    # what is left
    for rel, a in disk.items():
        if rel in used:
            continue
        if a in wanted_audio:
            pl.removes.append((rel, "a second copy of a sample already in Samples"))
        elif a in source_audios:
            pl.removes.append((rel, "its pack is set aside"))
        elif rel.lower() in rebuilt:
            pl.removes.append((rel, "its sample is no longer in any source"))
        else:
            pl.hand.append((rel, f"{layout.SOURCES}/User/{HAND_FOLDER}/{os.path.basename(rel)}"))
    return pl


def _abs(root, rel):
    return os.path.join(root, *rel.split("/"))


def _free(path):
    """path, or 'name (2).ext' … if something is already there."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def apply(db, pl: Plan, progress=None, log=lambda s: None) -> dict:
    root = pl.root
    db.ex(REBUILT)
    removed_dir = os.path.join(layout.app_dir(root), "Removed", stamp())
    done = {"added": 0, "moved": 0, "removed": 0, "hand": 0, "errors": []}
    total = len(pl.adds) + len(pl.moves) + len(pl.removes) + len(pl.hand)
    step = 0

    def tick(text):
        nonlocal step
        step += 1
        if progress and (step % 20 == 0 or step == total):
            progress(step, total, text)

    def out_of_the_way(p, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(p, _free(dst))

    for rel, dst in pl.hand:
        try:
            out_of_the_way(_abs(root, rel), _abs(root, dst))
            done["hand"] += 1
            log(f"kept in Sources › User › {HAND_FOLDER}: {os.path.basename(rel)}")
        except OSError as e:
            done["errors"].append(f"{rel}: {e.strerror or e}")
        tick("Keeping files you added by hand")
    for rel, why in pl.removes:
        try:
            out_of_the_way(_abs(root, rel), os.path.join(removed_dir, *rel.split("/")))
            db.ex("DELETE FROM rebuilt WHERE lower(rel) = ?", (rel.lower(),))
            done["removed"] += 1
            log(f"took out {rel} ({why})")
        except OSError as e:
            done["errors"].append(f"{rel}: {e.strerror or e}")
        tick("Taking out")
    tmp = []
    for i, (s, d, a) in enumerate(pl.moves):               # two steps, so moves that swap places are safe
        try:
            t = _abs(root, s) + f".{i}.smtmp"
            os.replace(_abs(root, s), t)
            tmp.append((t, s, d, a))
        except OSError as e:
            done["errors"].append(f"{s}: {e.strerror or e}")
    for t, s, d, a in tmp:
        try:
            os.makedirs(os.path.dirname(_abs(root, d)), exist_ok=True)
            os.replace(t, _abs(root, d))
            db.ex("DELETE FROM rebuilt WHERE lower(rel) = ?", (s.lower(),))
            db.ex("INSERT OR REPLACE INTO rebuilt VALUES (?, ?)", (d, a))
            done["moved"] += 1
        except OSError as e:
            done["errors"].append(f"{d}: {e.strerror or e}")
        tick("Moving to another type")
    for d, w in pl.adds:
        try:
            dst = _abs(root, d)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            data = src.read_bytes(root, w.source_rel)
            if src.identity(data) != w.audio:
                raise OSError(f"the file in Sources has changed since it was read ({w.source_rel}); scan first")
            t = dst + ".smtmp"
            with open(t, "wb") as f:
                f.write(data)
            os.replace(t, dst)
            db.ex("INSERT OR REPLACE INTO rebuilt VALUES (?, ?)", (d, w.audio))
            done["added"] += 1
        except (OSError, KeyError) as e:
            done["errors"].append(f"{d}: {getattr(e, 'strerror', None) or e}")
        tick("Adding")
    for rel, a in pl.keep.items():
        db.ex("INSERT OR REPLACE INTO rebuilt VALUES (?, ?)", (rel, a))
    _prune_empty(_abs(root, _samples_folder(root)))
    scan_samples(db, root)
    return done


def _prune_empty(sdir):
    if not os.path.isdir(sdir):
        return
    for d in sorted((x for x, _, _ in os.walk(sdir)), key=len, reverse=True):
        if d != sdir:
            try:
                left = [f for f in os.listdir(d) if f.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")]
                if not left:
                    for f in os.listdir(d):
                        os.remove(os.path.join(d, f))
                    os.rmdir(d)
            except OSError:
                pass


def on_devices(db, mapping, factory_rec, card) -> dict[str, set[str]]:
    """{device: set of audio identities on it} — from the mapping (every Send) and, for the Deluge, the factory
    record (untouched factory files). Only the open card counts for the Deluge."""
    out = {"Deluge": set(), "Syntakt": set()}
    if card is None:
        return out
    libs = [x.library for x in mapping.links if x.card.lower() in card.files]
    if factory_rec:
        libs += [e.library for e in factory_rec.entries if e.library and e.card.lower() in card.files]
    out["Deluge"] = {a for a in db.audio_of(libs).values() if a}
    return out


def type_counts_on(db, audios: set[str]) -> dict[str, int]:
    """{type folder: how many of its samples are in audios}."""
    out: dict[str, int] = {}
    for r in db.q("SELECT source, audio FROM sample WHERE area = 'Samples'"):
        if r["audio"] in audios:
            out[r["source"]] = out.get(r["source"], 0) + 1
    return out
