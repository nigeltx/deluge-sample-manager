"""The library index: one row per audio file in the PC library, with its header facts.

Lives at %LOCALAPPDATA%\\DelugeSampleManager\\library.db. Rebuilt incrementally by scanner.py (a file is
re-read only when its size or date changes), so the library pane opens instantly however big the library is.
"""
from __future__ import annotations

import os
import sqlite3
import threading

from dsm.library import types as stypes
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
-- the app's best guess at each sample's type (see types.py); worked out again when the rules change
CREATE TABLE IF NOT EXISTS stype (
    rel TEXT PRIMARY KEY, types TEXT, loop INTEGER, why TEXT, ver INTEGER
);
-- types the user set by hand (Set type…); these always win over the guess and survive rescans
CREATE TABLE IF NOT EXISTS user_type (
    rel TEXT PRIMARY KEY, types TEXT, loop INTEGER
);
"""

SELECT = """SELECT s.*, COALESCE(u.types, g.types) AS types, COALESCE(u.loop, g.loop) AS is_loop, g.why AS type_why,
       (u.rel IS NOT NULL) AS type_by_user
FROM sample s LEFT JOIN stype g ON g.rel = s.rel LEFT JOIN user_type u ON u.rel = s.rel"""

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
            self.con.executemany("DELETE FROM stype WHERE rel = ?", [(r["rel"],) for r in rows])
            self.con.commit()

    def remove(self, rels):
        with self.lock:
            for t in ("sample", "stype", "user_type"):
                self.con.executemany(f"DELETE FROM {t} WHERE rel = ?", [(r,) for r in rels])
            self.con.commit()

    def clear(self):
        with self.lock:
            self.con.execute("DELETE FROM sample")
            self.con.execute("DELETE FROM stype")
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
              stype=None, loop=None, limit: int = 20000) -> list[sqlite3.Row]:
        """stype: one type name or a list of them (any matches). loop: True / False / None for both."""
        sql, args = SELECT + " WHERE 1=1", []
        for col, val in (("source", source), ("level1", level1), ("level2", level2)):
            if val is not None:
                sql += f" AND s.{col} = ?"
                args.append(val)
        for w in text.lower().split():
            sql += " AND lower(s.rel) LIKE ?"
            args.append(f"%{w}%")
        if wont_play:
            sql += " AND (s.plays = 0 OR s.error IS NOT NULL)"
        if stype:
            names = [stype] if isinstance(stype, str) else list(stype)
            sql += " AND (" + " OR ".join("COALESCE(u.types, g.types) LIKE ?" for _ in names) + ")"
            args += [f"%,{n},%" for n in names]
        if loop is not None:
            sql += " AND COALESCE(u.loop, g.loop, 0) = ?"
            args.append(int(bool(loop)))
        sql += " ORDER BY lower(s.rel) LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.con.execute(sql, args).fetchall()
        if rels is not None:
            rows = [r for r in rows if r["rel"].lower() in rels]
        return rows

    # ------------------------------------------------------------------ sample types
    def refresh_types(self, progress=None) -> int:
        """Work out the type guess for every sample that has none yet (or an out-of-date one). Returns how many."""
        with self.lock:
            todo = self.con.execute(
                "SELECT s.rel, s.frames, s.rate FROM sample s LEFT JOIN stype g ON g.rel = s.rel "
                "WHERE g.rel IS NULL OR g.ver IS NOT ?", (stypes.RULES_VERSION,)).fetchall()
        out = []
        for k, r in enumerate(todo):
            secs = r["frames"] / r["rate"] if r["frames"] and r["rate"] else None
            ty, lp, why = stypes.guess(r["rel"], secs)
            out.append((r["rel"], stypes.pack(ty), int(lp), why, stypes.RULES_VERSION))
            if progress and k % 2000 == 0:
                progress(k, len(todo), "Working out sample types…")
        with self.lock:
            self.con.executemany("INSERT OR REPLACE INTO stype (rel, types, loop, why, ver) VALUES (?,?,?,?,?)", out)
            self.con.commit()
        return len(out)

    def type_counts(self, loop=None) -> dict[str, int]:
        """{type: n} using the user's types where set. A sample with two types counts in both."""
        sql = ("SELECT COALESCE(u.types, g.types) t, COUNT(*) n FROM sample s LEFT JOIN stype g ON g.rel = s.rel "
               "LEFT JOIN user_type u ON u.rel = s.rel")
        args = []
        if loop is not None:
            sql += " WHERE COALESCE(u.loop, g.loop, 0) = ?"
            args.append(int(bool(loop)))
        out: dict[str, int] = {}
        with self.lock:
            for r in self.con.execute(sql + " GROUP BY 1", args):
                for t in stypes.unpack(r["t"]) or [stypes.UNKNOWN]:
                    out[t] = out.get(t, 0) + r["n"]
        return out

    def count(self, stype=None, loop=None) -> int:
        """How many samples have any of these types (each sample counted once)."""
        sql = ("SELECT COUNT(*) FROM sample s LEFT JOIN stype g ON g.rel = s.rel LEFT JOIN user_type u ON u.rel = s.rel"
               " WHERE 1=1")
        args = []
        if stype:
            names = [stype] if isinstance(stype, str) else list(stype)
            sql += " AND (" + " OR ".join("COALESCE(u.types, g.types) LIKE ?" for _ in names) + ")"
            args += [f"%,{n},%" for n in names]
        if loop is not None:
            sql += " AND COALESCE(u.loop, g.loop, 0) = ?"
            args.append(int(bool(loop)))
        with self.lock:
            return self.con.execute(sql, args).fetchone()[0]

    def set_user_type(self, rels, types: list[str], loop: bool):
        with self.lock:
            self.con.executemany("INSERT OR REPLACE INTO user_type (rel, types, loop) VALUES (?,?,?)",
                                 [(r, stypes.pack(types), int(loop)) for r in rels])
            self.con.commit()

    def clear_user_type(self, rels):
        with self.lock:
            self.con.executemany("DELETE FROM user_type WHERE rel = ?", [(r,) for r in rels])
            self.con.commit()

    def all_rels(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.con.execute("SELECT rel FROM sample")]
