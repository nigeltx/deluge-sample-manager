"""Settings and app-data folders. Everything lives under %LOCALAPPDATA%\\SampleManager.

Before 0.9 the app was called Deluge Sample Manager and kept its data in %LOCALAPPDATA%\\DelugeSampleManager;
the first start moves that folder across (move_old_app_dir)."""
from __future__ import annotations

import json
import os
import sys


def app_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    d = os.path.join(base, "SampleManager")
    if not os.path.isdir(d):
        move_old_app_dir(base, d)
    os.makedirs(d, exist_ok=True)
    return d


def move_old_app_dir(base: str, new: str) -> bool:
    """Move %LOCALAPPDATA%\\DelugeSampleManager to the new name, once. Returns True if it moved."""
    old = os.path.join(base, "DelugeSampleManager")
    if not os.path.isdir(old) or os.path.exists(new):
        return False
    try:
        os.rename(old, new)
        return True
    except OSError:
        return False


def default_library_root() -> str:
    if sys.platform == "win32":
        return r"G:\My Drive\Studio\Samples"
    return os.path.join(os.path.expanduser("~"), "Samples")


DEFAULTS = {
    "library_root": None,       # filled with default_library_root() on first load
    "last_card": "",
    "audition_level": 80,
    "autoplay": True,
}


class Settings(dict):
    path = None

    @classmethod
    def load(cls) -> "Settings":
        s = cls(DEFAULTS)
        s.path = os.path.join(app_dir(), "settings.json")
        try:
            with open(s.path, encoding="utf-8") as f:
                s.update(json.load(f))
        except (OSError, ValueError):
            pass
        if not s.get("library_root"):
            s["library_root"] = default_library_root()
        return s

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(dict(self), f, indent=2)
        except OSError:
            pass
