"""Index the PC library: walk it, read the header of new or changed files, forget files that have gone.

Reading a header opens the file, so on a Google Drive folder set to "stream" Drive downloads each new file
once. After the first scan only new or changed files are read.
"""
from __future__ import annotations

import os

from dsm.audio.inspect import AUDIO_EXTS, inspect
from dsm.library.db import LibraryDB, split_rel


def walk(root: str):
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if not d.startswith(".") and d != "_DSM")
        for fn in fns:
            if os.path.splitext(fn)[1].lower() in AUDIO_EXTS and not fn.startswith("._"):
                full = os.path.join(dp, fn)
                yield os.path.relpath(full, root).replace(os.sep, "/"), full


def row_for(rel: str, full: str, size: int, mtime: float) -> dict:
    info = inspect(full)
    src, l1, l2 = split_rel(rel)
    return {"rel": rel, "source": src, "level1": l1, "level2": l2, "name": os.path.basename(rel),
            "size": size, "mtime": mtime, "container": info.container, "header": info.header, "rate": info.rate,
            "bits": info.bits, "channels": info.channels, "frames": info.frames, "loop_start": info.loop_start,
            "loop_end": info.loop_end, "root_note": info.root_note, "plays": int(info.deluge_plays),
            "native": int(info.native), "problems": "\n".join(info.problems), "error": info.error}


def scan(db: LibraryDB, root: str, progress=None) -> dict:
    """Bring the index up to date. Returns counts: seen, read, removed."""
    if not root or not os.path.isdir(root):
        return {"seen": 0, "read": 0, "removed": 0, "missing_root": True}
    known = db.known()
    seen, batch, read = set(), [], 0
    files = list(walk(root))
    for k, (rel, full) in enumerate(files):
        seen.add(rel)
        try:
            st = os.stat(full)
        except OSError:
            continue
        old = known.get(rel)
        if old and old[0] == st.st_size and abs((old[1] or 0) - st.st_mtime) < 0.01:
            continue
        batch.append(row_for(rel, full, st.st_size, st.st_mtime))
        read += 1
        if len(batch) >= 100:
            db.upsert(batch)
            batch = []
        if progress and read % 10 == 0:
            progress(k, len(files), f"Indexing the library: {rel}")
    db.upsert(batch)
    gone = [r for r in known if r not in seen]
    db.remove(gone)
    db.refresh_types(progress)
    if progress:
        progress(len(files), len(files), "")
    return {"seen": len(seen), "read": read, "removed": len(gone)}


def info_from_row(r, root: str):
    """An AudioInfo built from an index row, so the list never has to open the file."""
    from dsm.audio.inspect import AudioInfo
    probs = [p for p in (r["problems"] or "").split("\n") if p]
    return AudioInfo(path=os.path.join(root, *r["rel"].split("/")), container=r["container"] or "?",
                     header=r["header"] or "?", rate=r["rate"] or 0, bits=r["bits"] or 0, channels=r["channels"] or 0,
                     frames=r["frames"] or 0, loop_start=r["loop_start"], loop_end=r["loop_end"],
                     root_note=r["root_note"], error=r["error"], problems=probs)
