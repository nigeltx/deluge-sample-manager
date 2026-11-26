"""The library's folders (spec v2.1 section 2).

    Studio\\Samples                      the library root (Settings)
        Sources\\Factory\\Deluge, Syntakt   each device's factory samples — complete (2.2)
        Sources\\Paid, Free, User         packs as they came, folders or zips; never changed by the app
        Samples\\Kick … Unknown           one copy of each sample, by type; loops in <Type>\\Loops; written only by rebuild.py
        Backups\\Deluge, Syntakt          dated dumps of a device — never a source, never scanned
        _Sample Manager                  the app's own files: mapping, factory record, fingerprints, choices

Before 0.9 the app folder was _DSM (reorganize.py renames it).
"""
from __future__ import annotations

import os

from dsm.library import types as stypes

APP_FOLDER = "_Sample Manager"
OLD_APP_FOLDER = "_DSM"
SOURCES, SAMPLES, BACKUPS = "Sources", "Samples", "Backups"
SOURCE_TYPES = ["Factory", "Paid", "Free", "User"]
DEVICES = ["Deluge", "Syntakt"]
LOOPS = "Loops"
ARCHIVE = "Archive"                   # Clean up Studio.cmd moves things here; never read
SKIP_DIRS = {APP_FOLDER.lower(), OLD_APP_FOLDER.lower(), ARCHIVE.lower(), BACKUPS.lower()}

# Folder names in Samples, in the order the left pane shows them. A guessed "Hat" (open or closed unknown)
# goes to Other drums until it is set by hand.
TYPE_FOLDERS = ["Kick", "Snare", "Clap", "Closed hat", "Open hat", "Cymbal", "Tom", "Percussion", "Other drums",
                "Bass", "Synth", "Keys", "Pad", "Vocal", "FX", "Instrument", stypes.UNKNOWN]


def type_folder(types: list[str]) -> str:
    """The Samples folder for a sample with these types (the first one counts)."""
    for t in types or []:
        if t in TYPE_FOLDERS:
            return t
        if t == "Hat":
            return "Other drums"
    return stypes.UNKNOWN


def app_dir(root: str) -> str:
    return os.path.join(root, APP_FOLDER)


def sources_dir(root: str, *more: str) -> str:
    return os.path.join(root, SOURCES, *more)


def samples_dir(root: str, *more: str) -> str:
    return os.path.join(root, SAMPLES, *more)


def backups_dir(root: str, device: str) -> str:
    return os.path.join(root, BACKUPS, device)


def factory_dir(root: str, device: str) -> str:
    return sources_dir(root, "Factory", device)


def find_child(parent: str, name: str) -> str | None:
    """The real name of a child folder, matched without regard to case, or None."""
    try:
        for e in os.scandir(parent):
            if e.name.lower() == name.lower():
                return e.name
    except OSError:
        pass
    return None


def is_new_layout(root: str) -> bool:
    return bool(root and find_child(root, SOURCES) and os.path.isdir(app_dir(root)))


def cleanup_list_path(root: str) -> str:
    """Studio\\Software\\Studio Cleanup\\Cleanup list.txt (root is Studio\\Samples)."""
    studio = os.path.dirname(os.path.normpath(root))
    return os.path.join(studio, "Software", "Studio Cleanup", "Cleanup list.txt")


def add_to_cleanup(root: str, paths: list[str]) -> bool:
    """Add folders to the Studio Cleanup list (paths relative to Studio), the same way the Patch Manager does.
    False if the cleanup tool is not there."""
    p = cleanup_list_path(root)
    if not os.path.isfile(p):
        return False
    studio = os.path.dirname(os.path.normpath(root))
    with open(p, "r", encoding="utf-8-sig") as f:
        have = {line.strip().lower() for line in f}
    rels = [os.path.relpath(x, studio).replace("/", "\\") for x in paths]
    new = [r for r in rels if r.lower() not in have]
    if new:
        with open(p, "rb") as f:
            raw = f.read()
        with open(p, "ab") as f:
            if raw and not raw.endswith(b"\n"):
                f.write(b"\r\n")
            for r in new:
                f.write(r.encode("utf-8") + b"\r\n")
    return True
