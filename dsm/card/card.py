"""The SD card: finding Deluge cards, volume facts, and a case-insensitive file map.

Everything above this module talks to a Card. Version 1 has one backend, the file
system; a SysEx backend could implement the same calls later (spec section 8.4).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

DELUGE_TOP = ("SAMPLES", "KITS", "SYNTHS", "SONGS")
DELUGE_OWN_SAMPLE_DIRS = ("CLIPS", "RECORD", "RESAMPLE", "EXPORTS")
SOURCE_DIRS = ("FREE", "FACTORY", "PAID")
TRASH_DIR = "DSM_TRASH"


@dataclass
class VolumeFacts:
    root: str
    label: str = ""
    serial: str = ""
    fs: str = ""
    total: int = 0
    free: int = 0
    warnings: list[str] = field(default_factory=list)


def volume_facts(root: str) -> VolumeFacts:
    v = VolumeFacts(root=root)
    try:
        import shutil
        du = shutil.disk_usage(root)
        v.total, v.free = du.total, du.free
    except OSError:
        pass
    if sys.platform == "win32":
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(root))[0] + "\\"
        name = ctypes.create_unicode_buffer(261)
        fsname = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive), name, 261, ctypes.byref(serial), None, None, fsname, 261)
        if ok:
            v.label, v.fs, v.serial = name.value, fsname.value, f"{serial.value:08X}"
    else:
        try:
            import psutil
            best = None
            for p in psutil.disk_partitions(all=True):
                if os.path.abspath(root).startswith(p.mountpoint) and (best is None or len(p.mountpoint) > len(best.mountpoint)):
                    best = p
            if best:
                v.fs = best.fstype.upper()
        except Exception:  # noqa: BLE001
            pass
    is_drive_root = os.path.splitdrive(os.path.abspath(root))[1] in ("\\", "/", "")
    if is_drive_root and v.fs and v.fs not in ("FAT32", "VFAT"):
        v.warnings.append(f"This card is formatted {v.fs}. The Deluge needs FAT32.")
    missing = [d for d in DELUGE_TOP if not os.path.isdir(os.path.join(root, d))]
    if missing:
        v.warnings.append("Missing Deluge folders: " + ", ".join(missing))
    return v


def find_cards() -> list[str]:
    """Drive roots that look like Deluge cards (have a SAMPLES and a KITS folder)."""
    roots = []
    try:
        import psutil
        for p in psutil.disk_partitions(all=False):
            r = p.mountpoint
            if os.path.isdir(os.path.join(r, "SAMPLES")) and os.path.isdir(os.path.join(r, "KITS")):
                roots.append(r)
    except Exception:  # noqa: BLE001
        pass
    return roots


def long_name(path: str) -> str:
    """Resolve an 8.3 short name (CB1-BD~1.WAV) to the real long path on Windows."""
    if sys.platform != "win32":
        return path
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetLongPathNameW(path, buf, 32768)
    return buf.value if 0 < n < 32768 else path


class Card:
    """A folder tree treated as a Deluge card. Paths given to and returned by the Card
    are card-relative with forward slashes, e.g. 'SAMPLES/PAID/X/KICK.WAV'."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.facts = volume_facts(self.root)
        self.files: dict[str, str] = {}   # lower-case rel path -> rel path as on disk
        self.dirs: set[str] = set()

    def abs(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    def rel(self, absolute: str) -> str:
        return os.path.relpath(absolute, self.root).replace(os.sep, "/")

    def scan(self) -> None:
        self.files.clear()
        self.dirs.clear()
        for top in os.listdir(self.root):
            full = os.path.join(self.root, top)
            if not os.path.isdir(full) or top.upper() == "SYSTEM VOLUME INFORMATION" or top.startswith("."):
                continue
            for dp, dns, fns in os.walk(full):
                dns[:] = [d for d in dns if not d.startswith(".")]
                r = self.rel(dp)
                self.dirs.add(r)
                for fn in fns:
                    if fn.startswith("._"):     # macOS resource-fork litter
                        continue
                    rp = f"{r}/{fn}"
                    self.files[rp.lower()] = rp

    def resolve(self, path_as_written: str) -> str | None:
        """Card-relative path on disk for a path written in a preset, or None if missing.
        Case-insensitive, and 8.3 short names are resolved on Windows."""
        p = path_as_written.replace("\\", "/").lstrip("/")
        hit = self.files.get(p.lower())
        if hit:
            return hit
        if "~" in p:
            full = self.abs(p)
            if os.path.exists(full):
                return self.rel(long_name(full))
        return None

    def sample_files(self) -> list[str]:
        from dsm.audio.inspect import AUDIO_EXTS
        return sorted(v for k, v in self.files.items()
                      if k.startswith("samples/") and os.path.splitext(k)[1] in AUDIO_EXTS)

    def xml_files(self) -> list[str]:
        return sorted(v for k, v in self.files.items()
                      if k.split("/")[0] in ("kits", "synths", "songs") and k.endswith(".xml"))
