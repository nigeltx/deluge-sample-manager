"""The library index: one row per sample in the library (including samples inside zips).

Lives at %LOCALAPPDATA%\\SampleManager\\library.db and can always be rebuilt by rescanning. scanner.py keeps it
up to date.

Tables
  sample       one row per copy of a sample: rel (path below the library root, "::" for inside a zip),
               source/level1/level2 for the left pane, file (the file on disk that holds it), audio (identity,
               sources.identity) and the header facts. area is always "Library" (0.9.0 also had Samples).
  source_file  one row per file on disk (an audio file or a zip): size, date and SHA-1 (the fingerprint,
               spec 2.2) and its state: ok, changed (new_sha1 is what is there now) or missing.
  stype        the app's type guess for each copy (types.py).
  user_type    types set by hand (Set type…), by audio identity, so they follow the sample wherever its copies are.
  listed       the by-type view (spec 2.3): one row per sample — the copy that counts — with its type list and
               whether it is a loop. Worked out again by refresh_view() after every scan and Set type.
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
CREATE TABLE IF NOT EXISTS listed (audio TEXT PRIMARY KEY, rel TEXT, folder TEXT, loop INTEGER);
CREATE INDEX IF NOT EXISTS listed_folder ON listed(folder);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- sample IDs (ids.py): one per sound, by audio identity; sid_last keeps the highest number ever given per type code
CREATE TABLE IF NOT EXISTS sid (audio TEXT PRIMARY KEY, code TEXT, num INTEGER, name TEXT);
CREATE TABLE IF NOT EXISTS sid_last (code TEXT PRIMARY KEY, last INTEGER);
"""

SCHEMA_VERSION = "2"
SOURCE_ORDER = {"Factory": 0, "Paid": 1, "Free": 2, "User": 3}

# a Sources row's types: the user's (by audio) or the guess (by rel). A Samples row's type is its folder.
SELECT = """SELECT s.*, COALESCE(u.types, g.types) AS types, COALESCE(u.loop, g.loop) AS is_loop,
  g.why AS type_why, (u.audio IS NOT NULL) AS type_by_user, l.folder AS listed_folder,
  i.code AS sid_code, i.num AS sid_num, i.name AS sid_name,
  CASE WHEN i.code IS NULL THEN NULL ELSE i.code || substr('0000' || i.num, -4, 4) END AS sid
FROM sample s LEFT JOIN stype g ON g.rel = s.rel LEFT JOIN user_type u ON u.audio = s.audio
LEFT JOIN listed l ON l.audio = s.audio AND l.rel = s.rel LEFT JOIN sid i ON i.audio = s.audio"""

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
            for t in ("sample", "stype", "source_file", "listed"):
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
              stype=None, loop=None, audios=None, folder=None, listed=False, limit: int = 20000) -> list[sqlite3.Row]:
        """stype: one type name or a list (any matches). loop: True / False / None for both. audios: a set of
        identities to keep. listed: only the copy that counts of each sample (the by-type view); folder: one type
        list of that view (Kick … Unknown)."""
        sql, args = "SELECT * FROM (" + SELECT + ") s WHERE 1=1", []
        if listed or folder:
            sql += " AND s.listed_folder IS NOT NULL"
        if folder:
            sql += " AND s.listed_folder = ?"
            args.append(folder)
        for col, val in (("area", area), ("source", source), ("level1", level1), ("level2", level2)):
            if val is not None:
                sql += f" AND s.{col} = ?"
                args.append(val)
        for w in text.lower().split():
            sql += " AND (lower(s.rel) LIKE ? OR lower(COALESCE(s.sid, '')) = ?)"
            args += [f"%{w}%", w]
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

    def duplicate_audios(self) -> dict[str, int]:
        """{identity: number of copies} for every sound in the library more than once (0.10)."""
        return {r[0]: r[1] for r in self.q("SELECT audio, COUNT(*) FROM sample WHERE audio IS NOT NULL "
                                           "AND area != 'Samples' GROUP BY audio HAVING COUNT(*) > 1")}

    def duplicate_rows(self, limit: int = 20000) -> list[sqlite3.Row]:
        """Every copy of every sound that is in the library more than once, copies of one sound together."""
        return self.q("SELECT * FROM (" + SELECT + ") s WHERE s.area != 'Samples' AND s.audio IN "
                      "(SELECT audio FROM sample WHERE audio IS NOT NULL AND area != 'Samples' "
                      "GROUP BY audio HAVING COUNT(*) > 1) ORDER BY s.audio, lower(s.rel) LIMIT ?", (limit,))

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
        return {r[0] for r in self.q("SELECT DISTINCT audio FROM sample WHERE audio IS NOT NULL "
                                     "AND area != 'Samples'")}     # 0.9.0's Samples copies are not sources

    # ------------------------------------------------------------------ the by-type view (spec 2.3)
    def refresh_view(self) -> int:
        """Work out the listed table again: one row per sample, the copy that counts, in its type list."""
        from dsm.library import layout
        rows = []
        for a, r in self.wanted().items():
            types = stypes.unpack(r["types"]) or [stypes.UNKNOWN]
            rows.append((a, r["rel"], layout.type_folder(types), int(bool(r["is_loop"]))))
        with self.lock:
            self.con.execute("DELETE FROM listed")
            self.con.executemany("INSERT INTO listed (audio, rel, folder, loop) VALUES (?,?,?,?)", rows)
            self.con.commit()
        self.assign_ids()
        return len(rows)

    def assign_ids(self) -> int:
        """Give every sample in the listed view an ID (ids.py), and a new one when its type code changed.
        Numbers are handed out in a fixed order (type, then path) and never reused. Returns how many were new."""
        from dsm.library import ids
        with self.lock:
            have = {r["audio"]: (r["code"], r["num"], r["name"]) for r in self.con.execute("SELECT * FROM sid")}
            last = {r["code"]: r["last"] for r in self.con.execute("SELECT * FROM sid_last")}
            todo = []
            for r in self.con.execute("SELECT l.audio, l.rel, COALESCE(u.types, g.types) AS types FROM listed l "
                                      "LEFT JOIN stype g ON g.rel = l.rel LEFT JOIN user_type u ON u.audio = l.audio"):
                code = ids.code_for(stypes.unpack(r["types"]) or [stypes.UNKNOWN])
                name = ids.tidy_name(r["rel"].split("::")[-1])
                old = have.get(r["audio"])
                if old and old[0] == code:
                    if old[2] != name:
                        self.con.execute("UPDATE sid SET name = ? WHERE audio = ?", (name, r["audio"]))
                    continue
                todo.append((code, r["rel"].lower(), r["audio"], name))
            for code, _rel, audio, name in sorted(todo):
                last[code] = last.get(code, 0) + 1
                self.con.execute("INSERT OR REPLACE INTO sid (audio, code, num, name) VALUES (?,?,?,?)",
                                 (audio, code, last[code], name))
            self.con.executemany("INSERT OR REPLACE INTO sid_last (code, last) VALUES (?,?)", list(last.items()))
            self.con.commit()
        return len(todo)

    def id_labels(self) -> dict[str, str]:
        """{audio: 'HO0046 Fun Hat'} for every sample with an ID."""
        from dsm.library import ids
        return {r["audio"]: ids.label(r["code"], r["num"], r["name"]) for r in self.q("SELECT * FROM sid")}

    def labels_by_rel(self) -> dict[str, str]:
        """{library rel: ID label} for every copy of every sample with an ID."""
        from dsm.library import ids
        return {r["rel"]: ids.label(r["code"], r["num"], r["name"])
                for r in self.q("SELECT s.rel, i.code, i.num, i.name FROM sample s JOIN sid i ON i.audio = s.audio")}

    def flatten_paths(self) -> int:
        """After Reorganize moves Sources\\* up a level (0.9.0 → 0.9.1): drop the "Sources/" prefix everywhere and
        forget 0.9.0's Samples folder, so the next scan finds every file where it was and reads nothing again."""
        from dsm.library import sources as src
        n = 0
        with self.lock:
            self.con.execute("DELETE FROM stype WHERE rel IN (SELECT rel FROM sample WHERE area = 'Samples')")
            self.con.execute("DELETE FROM sample WHERE area = 'Samples'")
            for r in self.con.execute("SELECT rel, file FROM sample WHERE rel LIKE 'Sources/%'").fetchall():
                new, nf = r["rel"][8:], (r["file"] or "")[8:] if (r["file"] or "").startswith("Sources/") else r["file"]
                area, so, l1, l2 = src.split_rel(new)
                self.con.execute("UPDATE sample SET rel=?, file=?, area=?, source=?, level1=?, level2=? WHERE rel=?",
                                 (new, nf, area, so, l1, l2, r["rel"]))
                self.con.execute("UPDATE stype SET rel=? WHERE rel=?", (new, r["rel"]))
                n += 1
            self.con.execute("UPDATE source_file SET path = substr(path, 9) WHERE path LIKE 'Sources/%'")
            self.con.execute("UPDATE sample SET area = 'Library'")
            self.con.commit()
        return n

    def wanted(self) -> dict[str, sqlite3.Row]:
        """{audio: the copy in Sources that counts} — the first in Factory, Paid, Free, User order, then by path.
        Files whose state is missing still count, as read before (spec 2.2)."""
        best: dict[str, tuple] = {}
        for r in self.q(SELECT + " WHERE s.audio IS NOT NULL AND s.error IS NULL"):
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
                "WHERE (g.rel IS NULL OR g.ver IS NOT ?)", (stypes.RULES_VERSION,)).fetchall()
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
        self.refresh_view()

    def clear_user_type(self, audios):
        with self.lock:
            self.con.executemany("DELETE FROM user_type WHERE audio = ?", [(a,) for a in audios])
            self.con.commit()
        self.refresh_view()

    def user_types(self) -> dict[str, tuple[str, int]]:
        return {r["audio"]: (r["types"], r["loop"]) for r in self.q("SELECT * FROM user_type")}

    def load_user_types(self, d: dict):
        with self.lock:
            self.con.executemany("INSERT OR REPLACE INTO user_type (audio, types, loop) VALUES (?,?,?)",
                                 [(a, v[0], v[1]) for a, v in d.items()])
            self.con.commit()

    def type_counts(self, loop=None) -> dict[str, int]:
        """{type list: n samples} for the by-type view."""
        sql, args = "SELECT folder, COUNT(*) n FROM listed", []
        if loop is not None:
            sql += " WHERE loop = ?"
            args.append(int(bool(loop)))
        return {r["folder"]: r["n"] for r in self.q(sql + " GROUP BY 1", args)}

    def listed_folders(self) -> dict[str, str]:
        """{audio: type list} for every listed sample."""
        return {r["audio"]: r["folder"] for r in self.q("SELECT audio, folder FROM listed")}

    def all_rels(self) -> list[str]:
        return [r[0] for r in self.q("SELECT rel FROM sample")]
