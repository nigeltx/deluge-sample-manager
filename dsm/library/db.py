"""The library index: one row per sample in Sources (including samples inside zips) and per file in Samples.

Lives at %LOCALAPPDATA%\\SampleManager\\library.db and can always be rebuilt by rescanning. scanner.py keeps it
up to date; rebuild.py reads it to work out what Samples should hold.

Tables
  sample       one row per sample: rel (path below the library root, "::" for inside a zip), area (Sources or
               Samples), source/level1/level2 for the left pane, file (the file on disk that holds it), audio
               (identity, sources.identity) and the header facts.
  source_file  one row per file on disk in Sources (an audio file or a zip): size, date and SHA-1 (the
               fingerprint, spec 2.2) and its state: ok, changed (new_sha1 is what is there now) or missing.
  stype        the app's type guess for each Sources sample (types.py).
  user_type    types set by hand (Set type…), by audio identity, so they follow the sample wherever its copies are.
"""
from __future__ import annotations

import os
import sqlite3
import threading

from dsm.library import types as stypes
from dsm.settings import app_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS sample (
    rel TEXT PRIMARY KEY,
    area TEXT, source TEXT, level1 TEXT, level2 TEXT,
    name TEXT, file TEXT, audio TEXT,
    size INTEGER, mtime REAL,
    container TEXT, header TEXT, rate INTEGER, bits INTEGER, channels INTEGER, frames INTEGER,
    loop_start INTEGER, loop_end INTEGER, root_note INTEGER,
    plays INTEGER, native INTEGER, problems TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS sample_src ON sample(area, source, level1, level2);
CREATE INDEX IF NOT EXISTS sample_audio ON sample(audio);
CREATE INDEX IF NOT EXISTS sample_file ON sample(file);
CREATE TABLE IF NOT EXISTS source_file (
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha1 TEXT,
    state TEXT DEFAULT 'ok', new_sha1 TEXT
);
CREATE TABLE IF NOT EXISTS stype (
    rel TEXT PRIMARY KEY, types TEXT, loop INTEGER, why TEXT, ver INTEGER
);
CREATE TABLE IF NOT EXISTS user_type (
    audio TEXT PRIMARY KEY, types TEXT, loop INTEGER
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

SCHEMA_VERSION = "2"
SOURCE_ORDER = {"Factory": 0, "Paid": 1, "Free": 2, "User": 3}

# a Sources row's types: the user's (by audio) or the guess (by rel). A Samples row's type is its folder.
SELECT = """SELECT s.*,
  CASE WHEN s.area = 'Samples' THEN ',' || s.source || ',' ELSE COALESCE(u.types, g.types) END AS types,
  CASE WHEN s.area = 'Samples' THEN (s.level1 = 'Loops') ELSE COALESCE(u.loop, g.loop) END AS is_loop,
  g.why AS type_why, (u.audio IS NOT NULL) AS type_by_user
FROM sample s LEFT JOIN stype g ON g.rel = s.rel LEFT JOIN user_type u ON u.audio = s.audio"""

COLS = ["rel", "area", "source", "level1", "level2", "name", "file", "audio", "size", "mtime", "container", "header",
        "rate", "bits", "channels", "frames", "loop_start", "loop_end", "root_note", "plays", "native", "problems",
        "error"]


class LibraryDB:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(app_dir(), "library.db")
        self.lock = threading.RLock()
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        with self.lock:
            self._migrate()
            self.con.executescript(SCHEMA)
            self.con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
            self.con.commit()

    def _migrate(self):
        """0.8 kept one row per file keyed by its path, with no audio identity: start the index again (it is only
        an index) but keep the types set by hand, to be matched to samples by path at the next scan."""
        tabs = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sample" not in tabs:
            return
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(sample)")}
        if "audio" in cols:
            return
        if "user_type" in tabs:
            self.con.execute("ALTER TABLE user_type RENAME TO user_type_by_path")
        self.con.execute("DROP TABLE sample")
        self.con.execute("DROP TABLE IF EXISTS stype")
        self.con.commit()

    def q(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def ex(self, sql, args=()):
        with self.lock:
            self.con.execute(sql, args)
            self.con.commit()

    # ------------------------------------------------------------------ writing samples
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
            for t in ("sample", "stype"):
                self.con.executemany(f"DELETE FROM {t} WHERE rel = ?", [(r,) for r in rels])
            self.con.commit()

    def remove_file(self, path: str):
        """Forget a Sources file and every sample read from it."""
        with self.lock:
            rels = [r[0] for r in self.con.execute("SELECT rel FROM sample WHERE file = ?", (path,))]
            self.con.execute("DELETE FROM source_file WHERE path = ?", (path,))
        self.remove(rels)

    def clear(self):
        with self.lock:
            for t in ("sample", "stype", "source_file"):
                self.con.execute(f"DELETE FROM {t}")
            self.con.commit()

    def known_samples(self, area: str) -> dict[str, tuple[int, float]]:
        with self.lock:
            return {r["rel"]: (r["size"], r["mtime"]) for r in
                    self.con.execute("SELECT rel, size, mtime FROM sample WHERE area = ?", (area,))}

    # ------------------------------------------------------------------ fingerprints (spec 2.2)
    def source_files(self) -> dict[str, sqlite3.Row]:
        with self.lock:
            return {r["path"]: r for r in self.con.execute("SELECT * FROM source_file")}

    def set_source_file(self, path, size, mtime, sha1, state="ok", new_sha1=None):
        self.ex("INSERT OR REPLACE INTO source_file (path, size, mtime, sha1, state, new_sha1) VALUES (?,?,?,?,?,?)",
                (path, size, mtime, sha1, state, new_sha1))

    def source_alerts(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM source_file WHERE state != 'ok' ORDER BY path")

    def accept_change(self, path: str):
        """Use the file as it is now: the next scan reads it again."""
        self.ex("UPDATE source_file SET sha1 = NULL, mtime = 0, state = 'ok', new_sha1 = NULL WHERE path = ?", (path,))

    def accept_missing(self, path: str):
        """The file is gone for good: forget it and its samples."""
        self.remove_file(path)

    # ------------------------------------------------------------------ reading
    def counts(self) -> dict:
        """{(area,): n, (area, source): n, (area, source, level1): n, (area, source, level1, level2): n}"""
        out: dict = {}
        with self.lock:
            for r in self.con.execute("SELECT area, source, level1, level2, COUNT(*) n FROM sample GROUP BY 1,2,3,4"):
                keys = [r["area"], r["source"] or "", r["level1"] or "", r["level2"] or ""]
                for k in range(1, 5):
                    if k > 1 and not keys[k - 1]:
                        break
                    t = tuple(keys[:k])
                    out[t] = out.get(t, 0) + r["n"]
        return out

    def query(self, area=None, source=None, level1=None, level2=None, text: str = "", rels=None, wont_play=False,
              stype=None, loop=None, audios=None, limit: int = 20000) -> list[sqlite3.Row]:
        """stype: one type name or a list (any matches). loop: True / False / None for both. audios: a set of
        identities to keep."""
        sql, args = "SELECT * FROM (" + SELECT + ") s WHERE 1=1", []
        for col, val in (("area", area), ("source", source), ("level1", level1), ("level2", level2)):
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
            sql += " AND (" + " OR ".join("s.types LIKE ?" for _ in names) + ")"
            args += [f"%,{n},%" for n in names]
        if loop is not None:
            sql += " AND COALESCE(s.is_loop, 0) = ?"
            args.append(int(bool(loop)))
        sql += " ORDER BY lower(s.name), lower(s.rel) LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.con.execute(sql, args).fetchall()
        if rels is not None:
            rows = [r for r in rows if r["rel"].lower() in rels]
        if audios is not None:
            rows = [r for r in rows if r["audio"] in audios]
        return rows

    def row(self, rel: str):
        r = self.q(SELECT + " WHERE s.rel = ?", (rel,))
        return r[0] if r else None

    def copies(self, audio: str) -> list[sqlite3.Row]:
        return self.q(SELECT + " WHERE s.audio = ? ORDER BY s.area DESC, s.rel", (audio,))

    def audio_of(self, rels) -> dict[str, str]:
        """{lower-case rel: audio} for the given rels."""
        want = {r.lower() for r in rels}
        out = {}
        with self.lock:
            for r in self.con.execute("SELECT rel, audio FROM sample"):
                if r["rel"].lower() in want:
                    out[r["rel"].lower()] = r["audio"]
        return out

    def source_audios(self) -> set[str]:
        return {r[0] for r in self.q("SELECT DISTINCT audio FROM sample WHERE area = 'Sources' AND audio IS NOT NULL")}

    def wanted(self) -> dict[str, sqlite3.Row]:
        """{audio: the copy in Sources that counts} — the first in Factory, Paid, Free, User order, then by path.
        Files whose state is missing still count, as read before (spec 2.2)."""
        best: dict[str, tuple] = {}
        for r in self.q(SELECT + " WHERE s.area = 'Sources' AND s.audio IS NOT NULL AND s.error IS NULL"):
            key = (SOURCE_ORDER.get(r["source"], 9), r["rel"].lower())
            if r["audio"] not in best or key < best[r["audio"]][0]:
                best[r["audio"]] = (key, r)
        return {a: v[1] for a, v in best.items()}

    # ------------------------------------------------------------------ sample types
    def refresh_types(self, progress=None) -> int:
        """Work out the type guess for every Sources sample that has none yet (or an out-of-date one)."""
        with self.lock:
            todo = self.con.execute(
                "SELECT s.rel, s.frames, s.rate FROM sample s LEFT JOIN stype g ON g.rel = s.rel "
                "WHERE s.area = 'Sources' AND (g.rel IS NULL OR g.ver IS NOT ?)", (stypes.RULES_VERSION,)).fetchall()
        out = []
        for k, r in enumerate(todo):
            secs = r["frames"] / r["rate"] if r["frames"] and r["rate"] else None
            ty, lp, why = stypes.guess(r["rel"].replace("::", "/"), secs)
            out.append((r["rel"], stypes.pack(ty), int(lp), why, stypes.RULES_VERSION))
            if progress and k % 2000 == 0:
                progress(k, len(todo), "Working out sample types…")
        with self.lock:
            self.con.executemany("INSERT OR REPLACE INTO stype (rel, types, loop, why, ver) VALUES (?,?,?,?,?)", out)
            self.con.commit()
        return len(out)

    def adopt_old_user_types(self) -> int:
        """Types set by hand in 0.8 were kept by path: give each to the sample now at that path."""
        with self.lock:
            tabs = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "user_type_by_path" not in tabs:
                return 0
            old = self.con.execute("SELECT rel, types, loop FROM user_type_by_path").fetchall()
        by_rel = self.audio_of([r["rel"] for r in old])
        n = 0
        with self.lock:
            for r in old:
                a = by_rel.get(r["rel"].lower())
                if a:
                    self.con.execute("INSERT OR IGNORE INTO user_type (audio, types, loop) VALUES (?,?,?)",
                                     (a, r["types"], r["loop"]))
                    n += 1
            self.con.execute("DROP TABLE user_type_by_path")
            self.con.commit()
        return n

    def types_for(self, audio: str, rel: str) -> tuple[list[str], bool]:
        r = self.q("SELECT types, loop FROM user_type WHERE audio = ?", (audio,))
        if not r:
            r = self.q("SELECT types, loop FROM stype WHERE rel = ?", (rel,))
        if not r:
            return [], False
        return stypes.unpack(r[0]["types"]), bool(r[0]["loop"])

    def set_user_type(self, audios, types: list[str], loop: bool):
        with self.lock:
            self.con.executemany("INSERT OR REPLACE INTO user_type (audio, types, loop) VALUES (?,?,?)",
                                 [(a, stypes.pack(types), int(loop)) for a in audios])
            self.con.commit()

    def clear_user_type(self, audios):
        with self.lock:
            self.con.executemany("DELETE FROM user_type WHERE audio = ?", [(a,) for a in audios])
            self.con.commit()

    def user_types(self) -> dict[str, tuple[str, int]]:
        return {r["audio"]: (r["types"], r["loop"]) for r in self.q("SELECT * FROM user_type")}

    def load_user_types(self, d: dict):
        with self.lock:
            self.con.executemany("INSERT OR REPLACE INTO user_type (audio, types, loop) VALUES (?,?,?)",
                                 [(a, v[0], v[1]) for a, v in d.items()])
            self.con.commit()

    def type_counts(self, loop=None) -> dict[str, int]:
        """{type folder: n} for Samples."""
        sql, args = "SELECT source, COUNT(*) n FROM sample WHERE area = 'Samples'", []
        if loop is not None:
            sql += " AND (level1 = 'Loops') = ?"
            args.append(int(bool(loop)))
        return {r["source"]: r["n"] for r in self.q(sql + " GROUP BY 1", args)}

    def all_rels(self) -> list[str]:
        return [r[0] for r in self.q("SELECT rel FROM sample")]
