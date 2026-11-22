"""Turn dropped or selected files into a list of planned imports. Nothing is written here."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dsm.audio.inspect import AUDIO_EXTS, AudioInfo, inspect
from dsm.card.card import Card
from dsm.importer.convert import Profile, what_happens
from dsm.importer.mapping import Mapping
from dsm.importer.naming import card_path, unique

MAX_PATH = 120


@dataclass
class ImportItem:
    src: str                      # absolute path of the original
    card_rel: str                 # destination on the card
    library_rel: str              # where the original lives (or will live) in the library
    copy_original: bool           # True when the original is outside the library and gets copied in
    info: AudioInfo
    note: str = ""                # what conversion does
    skip: str | None = None       # reason this file is left out
    warn: str | None = None


@dataclass
class ImportBatch:
    source: str
    vendor: str
    pack: str
    profile: Profile
    items: list[ImportItem] = field(default_factory=list)

    @property
    def active(self):
        return [i for i in self.items if not i.skip]

    def summary(self) -> str:
        n = len(self.active)
        from dsm.importer.naming import clean
        where = f"SAMPLES/{self.source}" + (f"/{clean(self.vendor)}/{clean(self.pack)}" if self.source != "FACTORY" else "")
        return f"Import {n} file{'s' if n != 1 else ''} → {where} ({self.profile.name})"


def expand(paths: list[str]) -> list[tuple[str, str | None]]:
    """Files and folders → [(file, the dropped folder it came from or None)]."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for dp, dns, fns in os.walk(p):
                dns[:] = sorted(d for d in dns if not d.startswith(".") and d != "_DSM")
                for fn in sorted(fns):
                    if os.path.splitext(fn)[1].lower() in AUDIO_EXTS and not fn.startswith("._"):
                        out.append((os.path.join(dp, fn), p))
        elif os.path.isfile(p):
            out.append((p, None))
    return out


def library_rel_for(src: str, drop_root: str | None, library_root: str | None, source: str, vendor: str, pack: str):
    """(relative path in the library, True if it must be copied there)."""
    if library_root:
        lr = os.path.abspath(library_root)
        a = os.path.abspath(src)
        if a.lower().startswith(lr.lower() + os.sep):
            return os.path.relpath(a, lr).replace(os.sep, "/"), False
    inner = os.path.relpath(src, drop_root).replace(os.sep, "/") if drop_root else os.path.basename(src)
    parts = [source] + ([vendor] if vendor else []) + ([pack] if pack else []) + [inner]
    return "/".join(parts), True


def build_plan(files: list[tuple[str, str | None]], source: str, vendor: str, pack: str, profile: Profile,
               card: Card, library_root: str | None, mapping: Mapping, taken_extra=(), infos=None) -> ImportBatch:
    b = ImportBatch(source=source.upper(), vendor=vendor, pack=pack, profile=profile)
    taken = set(card.files) | {t.lower() for t in taken_extra}
    for src, drop_root in files:
        info = (infos or {}).get(src) or inspect(src)
        lib_rel, copy = library_rel_for(src, drop_root, library_root, b.source, vendor, pack)
        in_pack = os.path.relpath(src, drop_root).replace(os.sep, "/") if drop_root else None
        if not drop_root and not copy:
            # a library file: its path below SOURCE/VENDOR/PACK is its place in the pack
            parts = lib_rel.split("/")
            in_pack = "/".join(parts[3:]) if len(parts) > 3 else parts[-1]
        dest = card_path(b.source, vendor, pack, src, in_pack)
        item = ImportItem(src=src, card_rel=dest, library_rel=lib_rel, copy_original=copy, info=info)
        if info.error:
            item.skip = f"can't read: {info.error}"
        else:
            for link in mapping.for_library(lib_rel):
                if link.card.lower() in card.files and (not link.card_serial or not card.facts.serial
                                                        or link.card_serial == card.facts.serial):
                    item.skip = f"already on the card as {link.card}"
                    break
        if not item.skip:
            item.card_rel = unique(dest, taken)
            taken.add(item.card_rel.lower())
            item.note = what_happens(info, profile)
            if item.card_rel != dest:
                item.note += "; renamed to avoid a clash"
            if len(item.card_rel) > MAX_PATH:
                item.warn = f"path is {len(item.card_rel)} characters (rule is under {MAX_PATH})"
        b.items.append(item)
    return b


def bytes_needed(batches) -> int:
    total = 0
    for b in batches:
        for i in b.active:
            info = i.info
            ch = 1 if b.profile.channels == "mono" else max(1, min(info.channels or 1, 2))
            frames = int(info.frames * 44100 / info.rate) if info.rate else info.frames
            total += frames * ch * (b.profile.bits // 8) + 4096
    return total
