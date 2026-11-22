"""The Deluge's SD card, reached over USB. Same interface as dsm.card.card.Card.

Speed (measured on Nigel's Deluge): listing is quick, small files take a fraction of a second, bulk data
moves at about 30 KB/s. Moves and renames happen on the Deluge itself, so they are instant. To keep
things quick, preset and song files and sample header facts are cached on the PC, keyed by the
file's size and date; a file that changes on the Deluge is simply read again.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading

from dsm.card.card import Card, VolumeFacts
from dsm.settings import app_dir
from dsm.usb.link import DelugeLink, UsbError, fat_time

CACHE_EXTS = (".xml",)
SLOW_BYTES_PER_S = 30_000


def eta(nbytes: int) -> str:
    s = nbytes / SLOW_BYTES_PER_S
    return f"about {s:.0f} s" if s < 90 else f"about {s / 60:.0f} min"


class RemoteReader(io.RawIOBase):
    """A seekable read-only file on the Deluge, fetched in small pieces as needed (for headers)."""

    def __init__(self, link: DelugeLink, path: str, size: int):
        self.link, self.path, self.size = link, path, size
        self.pos = 0
        self.fid = None
        self.blocks: dict[int, bytes] = {}

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def tell(self):
        return self.pos

    def _block(self, n: int) -> bytes:
        if n not in self.blocks:
            if self.fid is None:
                self.fid, self.size = self.link.open(self.path)
            self.blocks[n] = self.link.read_at(self.fid, n * 512, min(512, max(0, self.size - n * 512)))
        return self.blocks[n]

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        out = bytearray()
        while n > 0 and self.pos < self.size:
            b, off = divmod(self.pos, 512)
            chunk = self._block(b)[off:off + n]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def readinto(self, buf):
        data = self.read(len(buf))
        buf[:len(data)] = data
        return len(data)

    def close(self):
        if self.fid is not None:
            self.link.close(self.fid)
            self.fid = None
        super().close()


class UsbCard(Card):
    is_usb = True

    def __init__(self, link: DelugeLink, cache_dir: str | None = None):
        self.link = link
        self.root = "USB:Deluge"
        self.facts = VolumeFacts(root=self.root, label="DELUGE (USB)", fs="FAT32")
        self.files: dict[str, str] = {}
        self.dirs: set[str] = set()
        self.meta: dict[str, tuple[int, float]] = {}      # lower rel -> (size, mtime)
        self.cache_dir = cache_dir or os.path.join(app_dir(), "usbcache")
        os.makedirs(os.path.join(self.cache_dir, "files"), exist_ok=True)
        self._info_path = os.path.join(self.cache_dir, "headers.json")
        try:
            with open(self._info_path, encoding="utf-8") as f:
                self._infos = json.load(f)
        except (OSError, ValueError):
            self._infos = {}
        self._info_lock = threading.Lock()
        self._info_dirty = 0

    # ---------------------------------------------------------------- paths
    def abs(self, rel: str) -> str:
        return "/" + rel.lstrip("/")

    def rel(self, absolute: str) -> str:
        return absolute.lstrip("/")

    def local_path(self, rel):
        return None

    # ---------------------------------------------------------------- listing
    def scan(self, progress=None) -> None:
        self.files.clear()
        self.dirs.clear()
        self.meta.clear()
        todo = [""]
        n = 0
        while todo:
            d = todo.pop()
            for e in self.link.listdir("/" + d):
                name = e.get("name", "")
                if not name or name in (".", "..") or name.startswith(".") or \
                        name.upper() == "SYSTEM VOLUME INFORMATION":
                    continue
                rel = f"{d}/{name}" if d else name
                if e.get("attr", 0) & 0x10:
                    self.dirs.add(rel)
                    todo.append(rel)
                elif d:                                       # files at the card root are not samples or presets
                    self.files[rel.lower()] = rel
                    self.meta[rel.lower()] = (e.get("size", 0), fat_time(e.get("date", 0), e.get("time", 0)))
            n += 1
            if progress and n % 5 == 0:
                progress(n, n + len(todo), f"Listing the Deluge's card: {d or '/'}")
        self._save_infos(force=True)

    def _refresh_entry(self, rel: str):
        """Re-read one file's size and date from its folder listing."""
        folder, name = rel.rsplit("/", 1)
        for e in self.link.listdir("/" + folder):
            if e.get("name", "").lower() == name.lower() and not e.get("attr", 0) & 0x10:
                real = f"{folder}/{e['name']}"
                self.files[real.lower()] = real
                self.meta[real.lower()] = (e.get("size", 0), fat_time(e.get("date", 0), e.get("time", 0)))
                self._add_dirs(real)
                return
        self.files.pop(rel.lower(), None)
        self.meta.pop(rel.lower(), None)

    def _add_dirs(self, rel):
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.dirs.add("/".join(parts[:i]))

    # ---------------------------------------------------------------- reading
    def exists(self, rel: str) -> bool:
        return rel.lower() in self.files

    def stat(self, rel: str, fresh: bool = False):
        if fresh:
            self._refresh_entry(rel)
        if rel.lower() not in self.meta:
            raise FileNotFoundError(rel)
        return self.meta[rel.lower()]

    def _cache_file(self, rel: str) -> str:
        h = hashlib.sha1(rel.lower().encode()).hexdigest()[:16]
        return os.path.join(self.cache_dir, "files", h)

    def read_bytes(self, rel: str, progress=None, fresh: bool = False) -> bytes:
        """fresh=True always reads from the Deluge (used to check what was written)."""
        key = rel.lower()
        size, mtime = self.meta.get(key, (None, None))
        cacheable = key.endswith(CACHE_EXTS)
        cf = self._cache_file(rel)
        if cacheable and size is not None and not fresh:
            try:
                with open(cf + ".json", encoding="utf-8") as f:
                    m = json.load(f)
                if m.get("size") == size and m.get("mtime") == mtime:
                    with open(cf, "rb") as f:
                        data = f.read()
                    if len(data) == size:
                        return data
            except (OSError, ValueError):
                pass
        data = self.link.read(self.abs(self.files.get(key, rel)), progress=progress)
        if cacheable:
            self._store_cache(rel, data)
        return data

    def _store_cache(self, rel, data):
        size, mtime = self.meta.get(rel.lower(), (len(data), 0.0))
        cf = self._cache_file(rel)
        try:
            with open(cf, "wb") as f:
                f.write(data)
            with open(cf + ".json", "w", encoding="utf-8") as f:
                json.dump({"rel": rel, "size": size, "mtime": mtime}, f)
        except OSError:
            pass

    def open_read(self, rel: str):
        size, _ = self.stat(rel)
        return RemoteReader(self.link, self.abs(self.files.get(rel.lower(), rel)), size)

    def inspect(self, rel: str):
        from dsm.audio.inspect import AudioInfo, inspect
        key = rel.lower()
        size, mtime = self.meta.get(key, (None, None))
        with self._info_lock:
            hit = self._infos.get(key)
        if hit and hit.get("size") == size and hit.get("mtime") == mtime:
            d = dict(hit["info"])
            d["path"] = self.abs(rel)
            return AudioInfo(**d)
        with self.open_read(rel) as r:
            info = inspect(self.abs(rel), fileobj=r)
        if not info.error:
            from dataclasses import asdict
            with self._info_lock:
                self._infos[key] = {"size": size, "mtime": mtime, "info": asdict(info)}
                self._info_dirty += 1
            self._save_infos()
        return info

    def _save_infos(self, force=False):
        with self._info_lock:
            if not self._info_dirty or (not force and self._info_dirty < 25):
                return
            data = json.dumps(self._infos)
            self._info_dirty = 0
        try:
            tmp = self._info_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self._info_path)
        except OSError:
            pass

    # ---------------------------------------------------------------- writing
    def _tmp(self, rel):
        folder, name = rel.rsplit("/", 1)
        return f"{folder}/.dsmtmp-{name}"

    def write_bytes(self, rel: str, data: bytes) -> None:
        """Write to a temporary name, then swap it in. The old file is renamed aside first and only
        removed once the new one is in place."""
        real = self.files.get(rel.lower(), rel)
        tmp = self._tmp(real)
        self.link.write(self.abs(tmp), data)
        if self.exists(real):
            old = f"{real.rsplit('/', 1)[0]}/.dsmold-{real.rsplit('/', 1)[1]}"
            try:
                self.link.delete(self.abs(old))
            except UsbError:
                pass
            self.link.rename(self.abs(real), self.abs(old))
            self.link.rename(self.abs(tmp), self.abs(real))
            self.link.delete(self.abs(old))
        else:
            self.link.rename(self.abs(tmp), self.abs(real))
        self._refresh_entry(real)
        if real.lower().endswith(CACHE_EXTS):
            self._store_cache(real, data)

    def put_file(self, local: str, rel: str, progress=None) -> None:
        if self.exists(rel):
            raise RuntimeError(f"Already on the card: {rel}")
        with open(local, "rb") as f:
            data = f.read()
        folder = rel.rsplit("/", 1)[0]
        self._mkdirs(folder)
        tmp = self._tmp(rel)
        self.link.write(self.abs(tmp), data, progress=progress)
        self.link.rename(self.abs(tmp), self.abs(rel))
        self._refresh_entry(rel)

    def _mkdirs(self, folder: str):
        parts = folder.split("/")
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if d not in self.dirs and d.lower() not in {x.lower() for x in self.dirs}:
                self.link.mkdir(self.abs(d))
                self.dirs.add(d)

    def move(self, old: str, new: str) -> None:
        real = self.files.get(old.lower(), old)
        same = real.lower() == new.lower()
        if self.exists(new) and not same:
            raise RuntimeError(f"Already on the card: {new}")
        self._mkdirs(new.rsplit("/", 1)[0])
        if same:
            if real == new:
                return
            mid = self._tmp(new) + ".case"
            self.link.rename(self.abs(real), self.abs(mid))
            self.link.rename(self.abs(mid), self.abs(new))
        else:
            self.link.move(self.abs(real), self.abs(new))
        m = self.meta.pop(real.lower(), None)
        self.files.pop(real.lower(), None)
        self.files[new.lower()] = new
        if m:
            self.meta[new.lower()] = m
        self._add_dirs(new)
        with self._info_lock:
            if real.lower() in self._infos:
                self._infos[new.lower()] = self._infos.pop(real.lower())

    def remove_new(self, rel: str) -> None:
        try:
            self.link.delete(self.abs(rel))
        except UsbError:
            pass
        self.files.pop(rel.lower(), None)
        self.meta.pop(rel.lower(), None)

    def rmdir_if_empty(self, rel: str) -> None:
        try:
            if not [e for e in self.link.listdir(self.abs(rel)) if e.get("name") not in (".", "..")]:
                self.link.delete(self.abs(rel))
                self.dirs.discard(rel)
        except (UsbError, FileNotFoundError):
            pass

    def free_bytes(self):
        return None

    def read_for_playback(self, rel: str, progress=None) -> io.BytesIO:
        return io.BytesIO(self.read_bytes(rel, progress=progress))
