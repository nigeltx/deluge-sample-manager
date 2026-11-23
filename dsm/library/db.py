"""The library index: one row per audio file in the PC library, with its header facts.

Lives at %LOCALAPPDATA%\\DelugeSampleManager\\library.db. Rebuilt incrementally by scanner.py (a file is
re-read only when its size or date changes), so the library pane opens instantly however big the library is.
"""
from __future__ import annotations

import os
import sqlite3
import threading

from dsm.settings import app_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS sample (
    rel TEXT PRIMARY KEY,           -- path below the library root, forward slashes, as on disk
    source TEXT,                    -- Factory, Free, Paid or Other
    level1 TEXT, level2 TEXT,       -- Free/Paid: vendor, pack.  Factory: first two folders.
    name TEXT,
    size INTEGER, mtime REAL,
    container TEXT, header TEXT, rate INTEGER, bits INTEGER, channels INTEGER, frames INTEGER,
    loop_start INTEGER, loop_end INTEGER, root_note INTEGER,
    plays INTEGER, native INTEGER, problems TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS sample_src ON sample(source, level1, level2);
"""

COLS = ["rel", "source", "level1", "level2", "name", "size", "mtime", "container", "header", "rate", "bits",
        "channels", "frames", "loop_start", "loop_end", "root_note", "plays", "native", "problems", "error"]


def split_rel(rel: str):
    """(source, level1, level2) from a library path."""
    parts = rel.split("/")
    top = parts[0].upper() if len(parts) > 1 else ""
    if top in ("FACTORY", "FREE", "PAID"):
        return top.title(), (parts[1] if len(parts) > 2 else ""), (parts[2] if len(parts) > 3 else "")
    return "Other", (parts[0] if len(parts) > 1 else ""), (parts[1] if len(parts) > 2 else "")


class LibraryDB:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(app_dir(), "library.db")
        self.lock = threading.RLock()
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        with self.lock:
            self.con.executescript(SCHEMA)

    def known(self) -> dict[str, tuple[int, float]]:
        with self.lock:
            return {r["rel"]: (r["size"], r["mtime"]) for r in self.con.execute("SELECT rel, size, mtime FROM sample")}

    def upsert(self, rows: list[dict]):
        if not rows:
            return
        q = f"INSERT OR REPLACE INTO sample ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})"
        with self.lock:
            self.con.executemany(q, [[r.get(c) for c in COLS] for r in rows])
            self.con.commit()

    def remove(self, rels):
        with self.lock:
            self.con.executemany("DELETE FROM sample WHERE rel = ?", [(r,) for r in rels])
            self.con.commit()

    def clear(self):
        with self.lock:
            self.con.execute("DELETE FROM sample")
            self.con.commit()

    def counts(self):
        """{(source,): n, (source, level1): n, (source, level1, level2): n, (): total}"""
        out = {}
        with self.lock:
            for r in self.con.execute("SELECT source, level1, level2, COUNT(*) n FROM sample GROUP BY 1, 2, 3"):
                s, a, b, n = r["source"], r["level1"] or "", r["level2"] or "", r["n"]
                out[()] = out.get((), 0) + n
                out[(s,)] = out.get((s,), 0) + n
                if a:
                    out[(s, a)] = out.get((s, a), 0) + n
                    if b:
                        out[(s, a, b)] = out.get((s, a, b), 0) + n
        return out

    def query(self, source=None, level1=None, level2=None, text: str = "", rels=None, wont_play=False,
              limit: int = 20000) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM sample WHERE 1=1", []
        for col, val in (("source", source), ("level1", level1), ("level2", level2)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        for w in text.lower().split():
            sql += " AND lower(rel) LIKE ?"
            args.append(f"%{w}%")
        if wont_play:
            sql += " AND (plays = 0 OR error IS NOT NULL)"
        sql += " ORDER BY lower(rel) LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.con.execute(sql, args).fetchall()
        if rels is not None:
            rows = [r for r in rows if r["rel"].lower() in rels]
        return rows

    def all_rels(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.con.execute("SELECT rel FROM sample")]
