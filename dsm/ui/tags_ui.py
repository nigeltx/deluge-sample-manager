"""Tags, BPM and key, the search filters and saved smart lists in the main window (0.13, spec v2.4 section 8).
The rules are in dsm/library (tags.py, tempokey.py, search.py); this is only the window's side.

Your tags and typed BPM/key are in the library db (tables tag, tempo_key) and in _Sample Manager\\tags.json, which
is the copy that outlives the PC: it is read into the db at start and written after every change. Saved smart lists
are in _Sample Manager\\smart-lists.json."""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QTableWidgetItem,
                               QTreeWidgetItem, QWidget)

from dsm import APP_NAME
from dsm.library import layout
from dsm.library import search as lsearch
from dsm.library import tags as ltags
from dsm.library import tempokey
from dsm.library import types as stypes
from dsm.ui.tag_widgets import SearchHelp, TagBox, TagsDialog, VocabDialog
from dsm.ui.widgets import Job, run_job

log = logging.getLogger("dsm.tags")
GREY = QColor("#6b6f77")
COL_BPM, COL_KEY, COL_TAGS = 10, 11, 12
SEARCH_LIMIT = 5000


class TagsMixin:
    # ---------------------------------------------------------------- state and files
    def _tags_init(self):
        self.tag_map: dict = {}
        self.tempo_map: dict = {}
        self.vocab = {k: list(v) for k, v in ltags.VOCAB.items()}
        self.saved_lists: list[dict] = []
        self.search_base = None          # (rows, title) of the list the search box filters
        self._in_search = False
        self._data_gen = 0
        self._all_cache = None           # (key, rows, ctxs) — every listed library sample, for saved lists
        self._rows_gen = 0               # goes up when the library's rows change (scan, Set type)
        self._saved_counts: dict = {}
        self._meta_ver: dict = {}        # audio → n: goes up when that sample's tags or tempo change
        self._ctx_dirty: set = set()     # samples whose cached search context must be worked out again

    def _tags_file(self, name: str) -> str | None:
        root = self.settings["library_root"]
        d = layout.app_dir(root) if root else None
        return os.path.join(d, name) if d and os.path.isdir(d) else None

    def _tags_load(self):
        """tags.json → the db (it is the copy that counts), and the saved smart lists."""
        p = self._tags_file("tags.json")
        d = ltags.load(p) if p else None
        if d:
            self.libdb.import_user_tags(d)
            if isinstance(d.get("vocab"), dict):
                self.vocab = {g: list(d["vocab"].get(g, ltags.VOCAB[g])) for g in ltags.VOCAB}
        p = self._tags_file("smart-lists.json")
        self.saved_lists = lsearch.load_lists(p) if p else []
        self._tags_reload()

    def _tags_save(self):
        p = self._tags_file("tags.json")
        if not p:
            return
        data = self.libdb.export_user_tags()
        data["vocab"] = self.vocab
        data["version"] = 1
        if not ltags.save(p, data):
            self.status_lbl.setText("⚠ Couldn't save tags.json in the library's _Sample Manager folder")

    def _lists_save(self):
        p = self._tags_file("smart-lists.json")
        if p and not lsearch.save_lists(p, self.saved_lists):
            self.status_lbl.setText("⚠ Couldn't save smart-lists.json in the library's _Sample Manager folder")

    def _tags_reload(self):
        self.tag_map = self.libdb.tag_map()
        self.tempo_map = self.libdb.tempo_map()
        self._data_gen += 1
        self._saved_counts = {}
        if hasattr(self, "tagbox"):
            self.tagbox.set_words(ltags.vocab_words(self.vocab) + self.libdb.tags_in_use())

    # ---------------------------------------------------------------- what a sample has
    def _path_of(self, row) -> str:
        return row.get("librel") or row.get("rel") or row.get("name") or ""

    def _meta(self, row) -> dict:
        """Tags (yours, guessed), BPM and key of a row, worked out once per data generation."""
        a = row.get("audio")
        m = row.get("_meta")
        if m and m["gen"] == self._data_gen and m["v"] == self._meta_ver.get(a, 0):
            return m
        t = self.tag_map.get(a, {"yes": set(), "no": set()}) if a else {"yes": set(), "no": set()}
        path = self._path_of(row)
        guess = [g for g in ltags.guesses(path, self.vocab) if g not in t["yes"] and g not in t["no"]] \
            if row.get("where") == "lib" else []
        self._row_types(row)
        tonal = not any(x in stypes.DRUMS for x in row["types"])
        tk = tempokey.resolve(path, bool(row.get("is_loop")), self.tempo_map.get(a) if a else None, tonal)
        m = {"gen": self._data_gen, "v": self._meta_ver.get(a, 0), "yes": sorted(t["yes"]), "guess": guess, **tk}
        row["_meta"] = m
        return m

    def _ctx(self, row) -> dict:
        m = self._meta(row)
        info = row.get("info")
        a = row.get("audio")
        on = set()
        if a and a in self.on_deluge:
            on.add("deluge")
        if a and a in getattr(self, "on_syntakt", {}):
            on.add("syntakt")
        if row.get("where") == "syntakt":
            on.add("syntakt")
        if row.get("where") == "card":
            on.add("deluge")
        path = self._path_of(row)
        return {"id": row.get("sid") or "", "text": " ".join([row.get("name", ""), path, " ".join(m["yes"])]).lower(),
                "path": path, "tags": set(m["yes"]), "types": row["types"], "loop": bool(row.get("is_loop")),
                "bpm": m["bpm"], "key": m["key"], "seconds": info.seconds if info and info.rate else None,
                "on": on, "source": row.get("origin") or self._origin(row),
                "dupe": a in self._dupes() if a else False,
                "plays": (info.deluge_plays if info else None)}

    def _dupes(self):
        if getattr(self, "_dupe_gen", None) != self._data_gen:
            self._dupe_set = set(self.libdb.duplicate_audios())
            self._dupe_gen = self._data_gen
        return self._dupe_set

    # ---------------------------------------------------------------- list columns
    def _cell(self, i, col):
        """The item at (i, col), made if missing. Items are changed in place: replacing items in a big table one
        by one is very slow."""
        it = self.table.item(i, col)
        if it is None:
            it = QTableWidgetItem()
            self.table.setItem(i, col, it)
        return it

    def _fill_tag_cells(self, i, row):
        m = self._meta(row)
        b = self._cell(i, COL_BPM)
        b.setData(Qt.DisplayRole, float(m["bpm"]) if m["bpm"] else None)
        b.setToolTip(f"{tempokey.fmt_bpm(m['bpm'])} BPM ({m['bpm_how']})" if m["bpm"] else "")
        b.setData(Qt.ForegroundRole, None if m["bpm_how"] == "set by you" else GREY)
        k = self._cell(i, COL_KEY)
        k.setText(m["key"] or "")
        k.setToolTip(f"{m['key']} ({m['key_how']})" if m["key"] else "")
        k.setData(Qt.ForegroundRole, None if m["key_how"] == "set by you" else GREY)
        t = self._cell(i, COL_TAGS)
        t.setText(", ".join(m["yes"] + [g + "?" for g in m["guess"]]))
        t.setToolTip("A ? marks a tag guessed from the pack name: confirm it in the details pane" if m["guess"] else "")
        t.setData(Qt.ForegroundRole, GREY if not m["yes"] and m["guess"] else None)

    def _refresh_tag_cells(self, audios=None):
        """Fill the BPM, Key and Tags cells again — of every row, or only the rows of the samples given
        (a big list takes long to refill, so a change to one sample touches only its rows)."""
        whole = audios is None
        if whole:                           # turning sorting back on sorts the whole list again: only for all rows
            self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        try:
            for row in self.rows:
                if audios is not None and row.get("audio") not in audios:
                    continue
                it = row.get("_item")
                r = self.table.row(it) if it is not None else -1
                if r >= 0:
                    self._fill_tag_cells(r, row)
        except RuntimeError:                # the list was refilled meanwhile: its old items are gone
            pass
        finally:
            self.table.setUpdatesEnabled(True)
            if whole:
                self.table.setSortingEnabled(True)

    # ---------------------------------------------------------------- the details pane
    def _build_tag_rows(self, form):
        self.tagbox = TagBox()
        self.tagbox.added.connect(lambda t: self._tag_change(add=[t]))
        self.tagbox.taken_off.connect(lambda t: self._tag_change(remove=[t]))
        self.tagbox.confirmed.connect(lambda t: self._tag_change(add=[t]))
        form.addRow("Tags", self.tagbox)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        self.bpm_edit = QLineEdit()
        self.bpm_edit.setFixedWidth(60)
        self.bpm_edit.setToolTip("Type a tempo and press Enter; clear it to go back to the guess")
        self.bpm_edit.editingFinished.connect(self._bpm_typed)
        self.bpm_how = QLabel()
        self.bpm_how.setStyleSheet("color:#8a8f98;")
        self.key_edit = QLineEdit()
        self.key_edit.setFixedWidth(60)
        self.key_edit.setToolTip("Type a key such as F#m and press Enter; clear it to go back to the guess")
        self.key_edit.editingFinished.connect(self._key_typed)
        self.key_how = QLabel()
        self.key_how.setStyleSheet("color:#8a8f98;")
        h.addWidget(self.bpm_edit)
        h.addWidget(self.bpm_how)
        h.addSpacing(12)
        h.addWidget(QLabel("Key"))
        h.addWidget(self.key_edit)
        h.addWidget(self.key_how)
        h.addStretch()
        form.addRow("BPM", w)

    def _show_tags(self, row):
        ok = bool(row.get("audio"))
        m = self._meta(row)
        self.tagbox.set_tags(m["yes"], m["guess"], ok)
        for e, v in ((self.bpm_edit, tempokey.fmt_bpm(m["bpm"])), (self.key_edit, m["key"] or "")):
            e.blockSignals(True)
            e.setText(v)
            e.setEnabled(ok)
            e.blockSignals(False)
        self.bpm_how.setText(m["bpm_how"] or ("" if ok else "—"))
        self.key_how.setText(m["key_how"] or "")
        if not ok:
            self.tagbox.setToolTip("This file isn't in the library, so it can't be tagged")

    def _tag_targets(self):
        """The samples a change in the details pane applies to: the selected rows that are library samples."""
        rows = [self._row_at(i.row()) for i in self.table.selectionModel().selectedRows()]
        rows = [r for r in rows if r and r.get("audio")]
        if self.current and self.current.get("audio") and self.current not in rows:
            rows = [self.current]
        return rows

    def _tag_change(self, add=(), remove=(), rows=None):
        rows = rows or ([self.current] if self.current and self.current.get("audio") else [])
        audios = sorted({r["audio"] for r in rows})
        if not audios:
            return
        self.libdb.set_tags(audios, add=list(add), remove=list(remove))
        self._after_tag_change(audios)

    def _touch(self, audios):
        """Some samples' tags or tempo changed: reread the small tables, and redo only those samples' rows."""
        audios = set(audios)
        for a in audios:
            self._meta_ver[a] = self._meta_ver.get(a, 0) + 1
        self._ctx_dirty |= audios
        for a in audios:
            self.tag_map.pop(a, None)
            self.tempo_map.pop(a, None)
        for r in self.libdb.q(f"SELECT audio, tag, state FROM tag WHERE audio IN ({','.join('?' * len(audios))})",
                              list(audios)):
            self.tag_map.setdefault(r["audio"], {"yes": set(), "no": set()})[r["state"]].add(r["tag"])
        for r in self.libdb.q(f"SELECT * FROM tempo_key WHERE audio IN ({','.join('?' * len(audios))})", list(audios)):
            self.tempo_map[r["audio"]] = r
        self._refresh_tag_cells(audios)
        if self.current and self.current.get("audio") in audios:
            self._show_tags(self.current)

    def _after_tag_change(self, audios):
        self._tags_save()
        self._touch(audios)
        self.tagbox.set_words(ltags.vocab_words(self.vocab) + self.libdb.tags_in_use())
        QTimer.singleShot(0, self._update_saved_counts)

    def _bpm_typed(self):
        if not self.current or not self.current.get("audio"):
            return
        txt = self.bpm_edit.text().strip()
        m = self._meta(self.current)
        if txt == tempokey.fmt_bpm(m["bpm"]) and txt:
            return
        v = tempokey.parse_bpm(txt) if txt else None
        if txt and v is None:
            self.bpm_how.setText("a number, such as 145")
            return
        self.libdb.set_tempo([self.current["audio"]], bpm=v, set_bpm=True)
        self._after_tag_change([self.current["audio"]])

    def _key_typed(self):
        if not self.current or not self.current.get("audio"):
            return
        txt = self.key_edit.text().strip()
        m = self._meta(self.current)
        if txt and tempokey.norm_key(txt) == m["key"]:
            return
        k = tempokey.norm_key(txt) if txt else None
        if txt and k is None:
            self.key_how.setText("a key, such as F#m")
            return
        self.libdb.set_tempo([self.current["audio"]], key=k, set_key=True)
        self._after_tag_change([self.current["audio"]])

    # ---------------------------------------------------------------- Tags… for many
    def _tags_dialog(self, rows):
        rows = [r for r in rows if r.get("audio")]
        by_audio = {r["audio"]: r for r in rows}
        counts: dict[str, int] = {}
        for r in by_audio.values():
            for t in self._meta(r)["yes"]:
                counts[t] = counts.get(t, 0) + 1
        dlg = TagsDialog(self, len(by_audio), counts, ltags.vocab_words(self.vocab) + self.libdb.tags_in_use())
        if not dlg.exec() or not dlg.result_value:
            return
        v = dlg.result_value
        audios = sorted(by_audio)
        self.libdb.set_tags(audios, add=v["add"], remove=v["remove"])
        if v["bpm"][0] or v["key"][0]:
            self.libdb.set_tempo(audios, bpm=v["bpm"][1], key=v["key"][1], set_bpm=v["bpm"][0], set_key=v["key"][0])
        self._after_tag_change(audios)
        self.status_lbl.setText(f"Tags updated for {len(audios):,} sample{'s' if len(audios) != 1 else ''}")

    def _confirm_pack_guesses(self, q: dict):
        rows = self.libdb.query(**q)
        pairs = []
        for r in rows:
            if not r["audio"]:
                continue
            t = self.tag_map.get(r["audio"], {"yes": set(), "no": set()})
            for g in ltags.guesses(r["rel"], self.vocab):
                if g not in t["yes"] and g not in t["no"]:
                    pairs.append((r["audio"], g))
        if not pairs:
            QMessageBox.information(self, APP_NAME, "No guessed tags to confirm here: no pack or folder name in it "
                                    "holds one of the tag words (Settings › Tag words…).")
            return
        words = sorted({g for _a, g in pairs})
        if QMessageBox.question(self, APP_NAME, f"Confirm {', '.join(words)} on {len({a for a, _ in pairs}):,} "
                                "samples?\n\nGuesses you took off stay off.") != QMessageBox.Yes:
            return
        n = self.libdb.confirm_tags(pairs)
        self._after_tag_change({a for a, _ in pairs})
        self.status_lbl.setText(f"Confirmed {n:,} guessed tags")

    def _vocab_dialog(self):
        dlg = VocabDialog(self, self.vocab)
        if dlg.exec() and dlg.result_value:
            self.vocab = dlg.result_value
            self._tags_save()
            self._tags_reload()
            self._refresh_tag_cells()
            if self.current:
                self._show_tags(self.current)

    # ---------------------------------------------------------------- the acid chunk
    def _read_acid_all(self):
        todo = self.libdb.acid_unread()
        if not todo:
            QMessageBox.information(self, APP_NAME, "Every loop's file has been looked at already.")
            return
        if QMessageBox.question(self, APP_NAME, f"Read the tempo and root note stored in {len(todo):,} loop files "
                                "(their acid chunk)?\n\nOnly the start of each file is read, but files in Google Drive "
                                "that aren't on this PC yet are downloaded, and samples in zips are unpacked to the "
                                "cache. It can take a while; you can keep working.") != QMessageBox.Yes:
            return
        root = self.settings["library_root"]
        db = self.libdb
        cache = os.path.join(__import__("dsm.settings", fromlist=["app_dir"]).app_dir(), "cache")
        from dsm.library import sources as lsrc

        def work(progress):
            out, n = [], 0
            for k, r in enumerate(todo):
                try:
                    src = lsrc.materialize(root, r["rel"], cache) if lsrc.SEP in r["rel"] else \
                        os.path.join(root, *r["rel"].split("/"))
                    t, rt = tempokey.acid_from_file(src)
                except Exception:          # noqa: BLE001 — one unreadable file never stops the rest
                    t, rt = None, None
                out.append((r["audio"], t, rt))
                n += t is not None
                if len(out) >= 200:
                    db.set_acid(out)
                    out = []
                if k % 20 == 0:
                    progress(k, len(todo), f"Reading tempos: {r['rel'].split('/')[-1]}")
            db.set_acid(out)
            return n

        def done(n):
            self.prog.hide()
            self._tags_reload()
            self._refresh_tag_cells()
            QMessageBox.information(self, APP_NAME, f"Done: {n:,} of {len(todo):,} loops have a tempo in their file.")
        run_job(Job(work, wants_progress=True), done=done, failed=self._job_failed, progress=self._progress)

    def _acid_for_current(self, row, src):
        """While a sample loads for audition: read its acid chunk once (runs in the background job)."""
        a = row.get("audio")
        if not a or row.get("where") != "lib" or not str(src).lower().endswith(".wav"):
            return
        tk = self.tempo_map.get(a)
        if tk is not None and tk["acid_read"]:
            return
        try:
            t, r = tempokey.acid_from_file(src)
        except Exception:                  # noqa: BLE001
            return
        self.libdb.set_acid([(a, t, r)])
        return a if (t is not None or r is not None) else None

    # ---------------------------------------------------------------- search (8.4)
    def _search_rows(self, terms, rows):
        return [r for r in rows if lsearch.matches(terms, self._ctx(r))]

    def _do_search(self):
        q = lsearch.tidy(self.search.text())
        if not q:
            self._search_cleared()
            return
        base = self.search_base
        if q.lower() == "unused" and self.index and not (base and base[0] and base[0][0]["where"] == "card"):
            from dsm.index.index import unused_samples
            self._in_search = True
            try:
                self._list_card(unused_samples(self.index), "Unused on the card", show_dir=True)
            finally:
                self._in_search = False
            return
        terms, unknown = lsearch.parse(q)
        self.search.setStyleSheet("background:#fff3e0;" if unknown else "")
        self.search.setToolTip(("Not a filter, searched as text: " + " ".join(unknown) + "\nThe ? button lists the "
                                "filters.") if unknown else "")
        cur = self.tree.currentItem()
        kind = (cur.data(0, Qt.UserRole) or (None,))[0] if cur else None
        if base and base[0] and all(r["where"] == "card" for r in base[0]):
            if q.lower() == "unused" and self.index:
                from dsm.index.index import unused_samples
                self._run_search_fill([r for r in base[0] if r["rel"] in set(unused_samples(self.index))],
                                      f"{base[1]} › unused")
                return
            words = q.lower().split()
            self._run_search_fill([r for r in base[0] if all(w in r["rel"].lower() for w in words)],
                                  f"{base[1]} › search: {q}")
            return
        if base and kind not in (None, "samplesroot") and base[0]:
            rows, title = base[0], base[1]
            hits = self._search_rows(terms, rows)
        else:
            rows, ctxs = self._all_library()
            title = "Library"
            hits = [r for r, c in zip(rows, ctxs) if lsearch.matches(terms, c)]
        if not hits and lsearch.is_plain(terms) and title == "Library":    # every copy, not only the one that counts
            hits = self._lib_rows(self.libdb.query(text=q.lower(), limit=SEARCH_LIMIT))
        more = len(hits) > SEARCH_LIMIT
        self._run_search_fill(hits[:SEARCH_LIMIT], f"{title} › search: {q}"
                              + (f" (first {SEARCH_LIMIT:,} of {len(hits):,})" if more else ""))

    def _run_search_fill(self, rows, title):
        self._in_search = True
        try:
            self._fill_rows(rows, title)
        finally:
            self._in_search = False

    def _search_cleared(self):
        self.search.setStyleSheet("")
        self.search.setToolTip("")
        if self.search_base:
            self._run_search_fill(*self.search_base)

    def _search_help(self):
        SearchHelp(self).exec()

    # ---------------------------------------------------------------- saved smart lists (8.5)
    def _save_smart_list(self):
        q = lsearch.tidy(self.search.text())
        if not q:
            QMessageBox.information(self, APP_NAME, "Type a search first — for example  type:kick tag:dark  — then "
                                    "save it as a smart list.")
            return
        name, ok = QInputDialog.getText(self, APP_NAME, f"Name for the smart list\n(search: {q})")
        name = (name or "").strip()
        if not ok or not name:
            return
        if any(x["name"].lower() == name.lower() for x in self.saved_lists):
            if QMessageBox.question(self, APP_NAME, f"There is a smart list called {name} already. Replace "
                                    "its search?") != QMessageBox.Yes:
                return
            self.saved_lists = [x for x in self.saved_lists if x["name"].lower() != name.lower()]
        self.saved_lists.append({"name": name, "query": q})
        self._lists_save()
        self._build_library_root()
        it = self._find_tree_item(("saved", name))
        if it:
            self.tree.setCurrentItem(it)

    def _saved_tree(self, hdr):
        for x in self.saved_lists:
            n = self._saved_counts.get(x["query"])
            it = QTreeWidgetItem([f"★ {x['name']}" + (f"  ({n:,})" if n is not None else "")])
            it.setData(0, Qt.UserRole, ("saved", x["name"]))
            it.setToolTip(0, f"Your search: {x['query']}\nRight-click to rename, change the search or delete it.")
            hdr.addChild(it)
        if self.saved_lists and not self._saved_counts:
            QTimer.singleShot(50, self._update_saved_counts)

    def _all_library(self):
        """Every listed library sample with its search context. The rows are kept until the library changes; the
        contexts until the devices change, and a changed sample's context is worked out again on its own."""
        rkey = self._rows_gen
        ckey = (self._data_gen, len(self.on_deluge), len(getattr(self, "on_syntakt", {})))
        if not self._all_cache or self._all_cache[0] != rkey:
            rows = self._lib_rows(self.libdb.query(listed=True, limit=10 ** 7))
            for r in rows:
                self._row_types(r)
                r["origin"] = self._origin(r)
            where: dict = {}
            for i, r in enumerate(rows):
                where.setdefault(r.get("audio"), []).append(i)
            self._all_cache = (rkey, rows, None, None, where)
        rows, where = self._all_cache[1], self._all_cache[4]
        if self._all_cache[2] != ckey:
            self._ctx_dirty = set()
            self._all_cache = (rkey, rows, ckey, [self._ctx(r) for r in rows], where)
        elif self._ctx_dirty:
            dirty, self._ctx_dirty = self._ctx_dirty, set()
            ctxs = self._all_cache[3]
            for a in dirty:
                for i in where.get(a, ()):
                    ctxs[i] = self._ctx(rows[i])
        return rows, self._all_cache[3]

    def _warm_search(self):
        """Work out every sample's search context in the background, so the first search is quick."""
        run_job(Job(self._all_library), done=lambda _r: self._update_saved_counts(), failed=lambda _m: None)

    def _update_saved_counts(self):
        if not self.saved_lists:
            return
        rows, ctxs = self._all_library()
        want = self._loop_wanted()
        for x in self.saved_lists:
            terms, _ = lsearch.parse(x["query"])
            self._saved_counts[x["query"]] = sum(1 for r, c in zip(rows, ctxs)
                                                 if (want is None or c["loop"] == want) and lsearch.matches(terms, c))
        hdr = self._find_tree_item(("libsmarthdr", None))
        if hdr is None:
            return
        for i in range(hdr.childCount()):
            it = hdr.child(i)
            d = it.data(0, Qt.UserRole)
            if d and d[0] == "saved":
                x = next((s for s in self.saved_lists if s["name"] == d[1]), None)
                if x:
                    it.setText(0, f"★ {x['name']}  ({self._saved_counts.get(x['query'], 0):,})")

    def _saved_select(self, name):
        x = next((s for s in self.saved_lists if s["name"] == name), None)
        if not x:
            return
        rows, ctxs = self._all_library()
        terms, _ = lsearch.parse(x["query"])
        hits = [r for r, c in zip(rows, ctxs) if lsearch.matches(terms, c)]
        self._fill_rows(hits, f"★ {name} · {x['query']}")

    def _saved_menu(self, d, pos):
        x = next((s for s in self.saved_lists if s["name"] == d[1]), None)
        if not x:
            return
        m = QMenu(self)
        ren = m.addAction("Rename…")
        edit = m.addAction("Change the search…")
        dele = m.addAction("Delete this smart list")
        got = m.exec(self.tree.viewport().mapToGlobal(pos))
        if got is ren:
            name, ok = QInputDialog.getText(self, APP_NAME, "New name", text=x["name"])
            if ok and name.strip():
                x["name"] = name.strip()
        elif got is edit:
            q, ok = QInputDialog.getText(self, APP_NAME, f"Search for {x['name']}", text=x["query"])
            if ok and lsearch.tidy(q):
                x["query"] = lsearch.tidy(q)
        elif got is dele:
            if QMessageBox.question(self, APP_NAME, f"Delete the smart list {x['name']}?\n\nNo sample is touched.") \
                    != QMessageBox.Yes:
                return
            self.saved_lists.remove(x)
        else:
            return
        self._lists_save()
        self._saved_counts = {}
        self._build_library_root()
