"""Index the library: Factory, Paid, Free and User, with fingerprints, and samples inside zips (spec v2.2 2.2).

Every file on disk — an audio file or a zip — has a fingerprint: its size and a SHA-1 of its bytes. A file is read
again only when its size or date changes; then:
  • same SHA-1 (Drive only touched the date): nothing to do;
  • different SHA-1: it is marked *changed* and the app keeps using what it read before until you decide
    (Library changed or missing… → Use it as it is now, db.accept_change);
  • gone: marked *missing*, again keeping what was read, except in User, where a deletion is meant.
A file that went missing while a new file with the same SHA-1 appeared was moved or renamed: the old one is
forgotten without asking. After the scan the by-type view is worked out again (db.refresh_view).

Backups, Archive and the app's folder are never read, nor anything at the root outside the four folders.
Reading a file opens it, so on a Google Drive folder set to "stream" Drive downloads each new file once; after the
first scan only new or changed files are read.
"""
from __future__ import annotations

import io
import json
import os
import zipfile

from dsm.audio.inspect import inspect
from dsm.library import layout
from dsm.library import sources as src
from dsm.library.db import LibraryDB


def row_for(rel: str, data: bytes, size: int, mtime: float, file: str) -> dict:
    info = inspect(src.base_name(rel), fileobj=io.BytesIO(data))
    area, source, l1, l2 = src.split_rel(rel)
    return {"rel": rel, "area": area, "source": source, "level1": l1, "level2": l2, "name": src.base_name(rel),
            "file": file, "audio": src.identity(data), "size": size, "mtime": mtime, "container": info.container,
            "header": info.header, "rate": info.rate, "bits": info.bits, "channels": info.channels,
            "frames": info.frames, "loop_start": info.loop_start, "loop_end": info.loop_end,
            "root_note": info.root_note, "plays": int(info.deluge_plays), "native": int(info.native),
            "problems": "\n".join(info.problems), "error": info.error}


def read_source_file(rel: str, full: str) -> tuple[str, list[dict]]:
    """(SHA-1, sample rows) for one file in Sources."""
    st = os.stat(full)
    sha = src.file_sha1(full)
    rows = []
    if src.is_zip(rel):
        try:
            with zipfile.ZipFile(full) as z:
                for mrel, data in src.zip_members(z, rel):
                    rows.append(row_for(mrel, data, len(data), st.st_mtime, rel))
        except zipfile.BadZipFile as e:
            rows.append({"rel": rel + src.SEP + "(unreadable zip)", "area": src.AREA,
                         "source": src.split_rel(rel)[1], "name": os.path.basename(rel), "file": rel,
                         "size": st.st_size, "mtime": st.st_mtime, "error": f"Not a readable zip: {e}"})
    else:
        with open(full, "rb") as f:
            data = f.read()
        rows.append(row_for(rel, data, st.st_size, st.st_mtime, rel))
    return sha, rows


def scan(db: LibraryDB, root: str, progress=None) -> dict:
    """Bring the index up to date. Returns counts: seen, read, changed, missing, moved, removed."""
    out = {"seen": 0, "read": 0, "changed": 0, "missing": 0, "moved": 0, "removed": 0}
    if not root or not os.path.isdir(root):
        out["missing_root"] = True
        return out
    files = [f for _t, d in layout.source_dirs(root) for f in src.walk_files(d, root)]
    known = db.source_files()
    seen, new_shas = set(), {}
    for k, (rel, full) in enumerate(files):
        seen.add(rel)
        try:
            st = os.stat(full)
        except OSError:
            continue
        old = known.get(rel)
        if old and old["sha1"] and old["size"] == st.st_size and abs((old["mtime"] or 0) - st.st_mtime) < 0.01:
            if old["state"] == "missing":                       # it came back
                db.set_source_file(rel, st.st_size, st.st_mtime, old["sha1"])
            continue
        if progress:
            progress(k, len(files), f"Reading Sources: {rel}")
        if old and old["sha1"]:
            sha = src.file_sha1(full)
            if sha == old["sha1"]:
                db.set_source_file(rel, st.st_size, st.st_mtime, sha)
            elif old["new_sha1"] != sha or old["state"] != "changed":
                db.set_source_file(rel, old["size"], old["mtime"], old["sha1"], "changed", sha)
                out["changed"] += 1
            continue
        try:
            sha, rows = read_source_file(rel, full)
        except OSError:
            continue
        db.remove_file(rel)
        db.upsert(rows)
        db.set_source_file(rel, st.st_size, st.st_mtime, sha)
        new_shas[sha] = rel
        out["read"] += len(rows)
    out["seen"] = len(seen)
    for rel, r in known.items():
        if rel in seen:
            continue
        if r["sha1"] and r["sha1"] in new_shas:
            db.remove_file(rel)
            out["moved"] += 1
        elif rel.lower().startswith("user/"):
            db.remove_file(rel)
            out["removed"] += 1
        elif r["state"] != "missing":
            db.set_source_file(rel, r["size"], r["mtime"], r["sha1"], "missing")
            out["missing"] += 1
    db.refresh_types(progress)
    db.adopt_old_user_types()
    out["listed"] = db.refresh_view()
    save_fingerprints(db, root)
    if progress:
        progress(len(files), len(files), "")
    return out


def save_fingerprints(db: LibraryDB, root: str):
    """_Sample Manager\\sources.json: path → [size, SHA-1], so the fingerprints outlive the PC."""
    d = layout.app_dir(root)
    if not os.path.isdir(d):
        return
    data = {p: [r["size"], r["sha1"]] for p, r in sorted(db.source_files().items()) if r["sha1"]}
    tmp = os.path.join(d, "sources.json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=0)
        os.replace(tmp, os.path.join(d, "sources.json"))
    except OSError:
        pass


def info_from_row(r, root: str):
    """An AudioInfo built from an index row, so the list never has to open the file."""
    from dsm.audio.inspect import AudioInfo
    probs = [p for p in (r["problems"] or "").split("\n") if p]
    return AudioInfo(path=os.path.join(root, *r["rel"].split("/")), container=r["container"] or "?",
                     header=r["header"] or "?", rate=r["rate"] or 0, bits=r["bits"] or 0, channels=r["channels"] or 0,
                     frames=r["frames"] or 0, loop_start=r["loop_start"], loop_end=r["loop_end"],
                     root_note=r["root_note"], error=r["error"], problems=probs)
