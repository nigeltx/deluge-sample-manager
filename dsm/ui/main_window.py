"""Main window: sources tree, file list, inspector (spec section 3.1). Read-only in M1/M2."""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSlider, QSplitter, QTableWidget, QTableWidgetItem, QToolBar,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from dsm import APP_NAME, __version__
from dsm.audio import playback
from dsm.audio.inspect import AUDIO_EXTS, inspect
from dsm.card.card import DELUGE_OWN_SAMPLE_DIRS, Card, find_cards
from dsm.index.health import check, library_files
from dsm.index.index import build_index, snippet, unused_samples
from dsm.logs import log_dir, open_folder
from dsm.settings import Settings
from dsm.ui.health_dialog import HealthDialog
from dsm.ui.widgets import Job, Waveform, run_job

log = logging.getLogger("dsm.ui")
AMBER, RED, GREEN, GREY = QColor("#9a3412"), QColor("#b91c1c"), QColor("#065f46"), QColor("#6b6f77")
BROWSE = "Browse for a card or folder…"


def human_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.card: Card | None = None
        self.index = None
        self.health = None
        self.player = playback.Player()
        self.player.level = settings["audition_level"] / 100
        self.rows: list[dict] = []
        self.list_gen = 0
        self.current = None          # the row dict shown in the inspector
        self.audio = None            # (data, rate) of the current row
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1400, 860)
        self._build_toolbar()
        self._build_body()
        self._build_status()
        QTimer.singleShot(0, self._startup)

    # ------------------------------------------------------------------ layout
    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel(" Card: "))
        self.card_box = QComboBox()
        self.card_box.setMinimumWidth(260)
        self.card_box.activated.connect(self._card_chosen)
        tb.addWidget(self.card_box)
        a = QAction("Refresh", self)
        a.setToolTip("Look for cards again and re-read the open card")
        a.triggered.connect(self._refresh)
        tb.addAction(a)
        self.health_act = QAction("Health check", self)
        self.health_act.setEnabled(False)
        self.health_act.triggered.connect(self._run_health)
        tb.addAction(self.health_act)
        s = QAction("Settings", self)
        s.triggered.connect(self._settings_dialog)
        tb.addAction(s)
        lg = QAction("Log folder", self)
        lg.setToolTip("Open the folder with the app's log file")
        lg.triggered.connect(lambda: open_folder(log_dir()))
        tb.addAction(lg)
        tb.addSeparator()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search the card — name, or 'unused'")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(300)
        self.search.returnPressed.connect(self._do_search)
        tb.addWidget(self.search)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding, spacer.sizePolicy().verticalPolicy())
        tb.addWidget(spacer)
        badge = QLabel("  READ-ONLY BUILD — nothing is written to the card  ")
        badge.setStyleSheet("background:#d8f0e4;color:#065f46;font-weight:600;border-radius:4px;padding:3px;")
        tb.addWidget(badge)

    def _build_body(self):
        split = QSplitter()
        self.setCentralWidget(split)
        # sources
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._tree_select)
        self.tree.itemExpanded.connect(self._tree_expand)
        split.addWidget(self.tree)
        # file list
        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(0, 0, 0, 0)
        self.warn = QLabel()
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("background:#fbe6d6;color:#7c2d12;padding:6px;border-left:3px solid #9a3412;")
        self.warn.hide()
        ml.addWidget(self.warn)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Length", "kHz/bit", "Used", "Deluge"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 5):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.currentCellChanged.connect(lambda r, *_: self._row_select(r))
        self.table.installEventFilter(self)
        ml.addWidget(self.table, 1)
        self.list_label = QLabel()
        self.list_label.setStyleSheet("color:#4f535b;")
        ml.addWidget(self.list_label)
        split.addWidget(mid)
        # inspector
        insp = QWidget()
        il = QVBoxLayout(insp)
        self.name_lbl = QLabel("—")
        self.name_lbl.setStyleSheet("font-size:15px;font-weight:700;")
        self.name_lbl.setWordWrap(True)
        self.name_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        il.addWidget(self.name_lbl)
        self.wave = Waveform()
        il.addWidget(self.wave)
        row = QHBoxLayout()
        self.play_btn = QPushButton("▶  Play (Space)")
        self.play_btn.clicked.connect(self._toggle_play)
        self.loop_chk = QCheckBox("Loop")
        self.auto_chk = QCheckBox("Play on select")
        self.auto_chk.setChecked(self.settings["autoplay"])
        self.auto_chk.toggled.connect(lambda v: (self.settings.__setitem__("autoplay", v), self.settings.save()))
        row.addWidget(self.play_btn)
        row.addWidget(self.loop_chk)
        row.addWidget(self.auto_chk)
        row.addStretch()
        il.addLayout(row)
        lv = QHBoxLayout()
        lv.addWidget(QLabel("Audition level"))
        self.level = QSlider(Qt.Horizontal)
        self.level.setRange(0, 100)
        self.level.setValue(self.settings["audition_level"])
        self.level.valueChanged.connect(self._level)
        lv.addWidget(self.level)
        il.addLayout(lv)
        form = QFormLayout()
        self.f_format, self.f_rate, self.f_len, self.f_loop, self.f_where = (QLabel() for _ in range(5))
        for lbl in (self.f_format, self.f_rate, self.f_len, self.f_loop, self.f_where):
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Format", self.f_format)
        form.addRow("Rate", self.f_rate)
        form.addRow("Length", self.f_len)
        form.addRow("Loop / root", self.f_loop)
        form.addRow("Location", self.f_where)
        il.addLayout(form)
        self.problems = QLabel()
        self.problems.setWordWrap(True)
        il.addWidget(self.problems)
        self.used_lbl = QLabel("USED BY")
        self.used_lbl.setStyleSheet("color:#1d4ed8;font-weight:700;margin-top:8px;")
        il.addWidget(self.used_lbl)
        self.used = QListWidget()
        self.used.itemDoubleClicked.connect(self._show_ref)
        self.used.setToolTip("Double-click to see the exact line in the preset file")
        il.addWidget(self.used, 1)
        split.addWidget(insp)
        split.setSizes([300, 640, 460])
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self._stop)

    def _build_status(self):
        self.status_lbl = QLabel("Starting…")
        self.prog = QProgressBar()
        self.prog.setMaximumWidth(260)
        self.prog.hide()
        self.statusBar().addWidget(self.status_lbl, 1)
        self.statusBar().addPermanentWidget(self.prog)
        if not self.player.available:
            self.statusBar().addPermanentWidget(QLabel(f"  {self.player.error}  "))

    # ------------------------------------------------------------------ cards
    def _startup(self):
        self._fill_card_box()
        self._build_library_root()
        last = self.settings.get("last_card")
        cards = find_cards()
        target = cards[0] if cards else (last if last and os.path.isdir(last) else None)
        if target:
            self._open_card(target)
        else:
            self.status_lbl.setText("No Deluge card found. Put the card in the reader and press Refresh, "
                                    "or choose a folder from the Card list.")

    def _fill_card_box(self):
        self.card_box.clear()
        seen = []
        for c in find_cards():
            seen.append(c)
            self.card_box.addItem(f"{c}  (Deluge card)", c)
        last = self.settings.get("last_card")
        if last and last not in seen and os.path.isdir(last):
            self.card_box.addItem(f"{last}  (last used)", last)
        self.card_box.addItem(BROWSE, None)
        if self.card:
            i = self.card_box.findData(self.card.root)
            if i >= 0:
                self.card_box.setCurrentIndex(i)

    def _card_chosen(self, i):
        path = self.card_box.itemData(i)
        if path is None:
            path = QFileDialog.getExistingDirectory(self, "Choose the card (or a copy of it)")
            if not path:
                return
        self._open_card(path)

    def _refresh(self):
        self._fill_card_box()
        if self.card and os.path.isdir(self.card.root):
            self._open_card(self.card.root)
        elif self.card:
            self.status_lbl.setText("The card was removed.")
            self.card = self.index = None
            self._build_card_tree()
        else:
            self._startup()

    def _open_card(self, path: str):
        self._stop()
        self.status_lbl.setText(f"Reading {path} …")
        self.health_act.setEnabled(False)

        def work(progress):
            c = Card(path)
            c.scan()
            return c, build_index(c, progress=progress)
        run_job(Job(work, wants_progress=True), done=self._card_ready, failed=self._job_failed,
                progress=self._progress)

    def _card_ready(self, result):
        self.card, self.index = result
        self.health = None
        self.prog.hide()
        self.settings["last_card"] = self.card.root
        self.settings.save()
        self._fill_card_box()
        f = self.card.facts
        n_s, n_x = len(self.card.sample_files()), len(self.index.docs)
        free = f" · {human_size(f.free)} free of {human_size(f.total)}" if f.total else ""
        self.status_lbl.setText(f"{f.label or self.card.root} · {f.fs or '?'}{free} · {n_s} samples · {n_x} presets "
                                f"and songs · {len(self.index.missing)} missing references")
        if f.warnings:
            self.warn.setText("  ·  ".join(f.warnings))
            self.warn.show()
        else:
            self.warn.hide()
        self.health_act.setEnabled(True)
        self._build_card_tree()

    # ------------------------------------------------------------------ tree
    def _build_card_tree(self):
        self.tree.blockSignals(True)
        top = self.tree.topLevelItem(0)
        if top and top.data(0, Qt.UserRole) == ("cardroot", None):
            self.tree.takeTopLevelItem(0)
        if self.card:
            root = QTreeWidgetItem([f"CARD  {self.card.facts.label or self.card.root}"])
            root.setData(0, Qt.UserRole, ("cardroot", None))
            self.tree.insertTopLevelItem(0, root)
            items = {}
            for d in sorted(self.card.dirs, key=lambda s: s.lower()):
                parent = items.get(d.rsplit("/", 1)[0]) if "/" in d else root
                it = QTreeWidgetItem([d.rsplit("/", 1)[-1]])
                it.setData(0, Qt.UserRole, ("card", d))
                if d.split("/")[0].upper() == "SAMPLES" and d.count("/") == 1 and \
                        d.split("/")[1].upper() in DELUGE_OWN_SAMPLE_DIRS:
                    it.setForeground(0, GREY)
                (parent or root).addChild(it)
                items[d] = it
            for name in ("Unused on card", "Won't play on the Deluge"):
                s = QTreeWidgetItem([f"★ {name}"])
                s.setData(0, Qt.UserRole, ("smart", name))
                root.addChild(s)
            root.setExpanded(True)
            if "SAMPLES" in items:
                items["SAMPLES"].setExpanded(True)
        self.tree.blockSignals(False)

    def _build_library_root(self):
        for i in range(self.tree.topLevelItemCount()):
            if self.tree.topLevelItem(i).data(0, Qt.UserRole)[0] == "libroot":
                self.tree.takeTopLevelItem(i)
                break
        root_path = self.settings["library_root"]
        lib = QTreeWidgetItem([f"LIBRARY  {root_path}"])
        lib.setData(0, Qt.UserRole, ("libroot", root_path))
        if os.path.isdir(root_path):
            lib.addChild(QTreeWidgetItem(["…"]))
        else:
            lib.addChild(QTreeWidgetItem(["(folder not found — set it in Settings)"]))
        self.tree.addTopLevelItem(lib)

    def _tree_expand(self, item):
        kind, path = item.data(0, Qt.UserRole) or (None, None)
        if kind not in ("lib", "libroot") or item.childCount() != 1 or item.child(0).data(0, Qt.UserRole):
            return
        item.takeChild(0)
        try:
            subs = sorted((e for e in os.scandir(path) if e.is_dir() and not e.name.startswith(".")),
                          key=lambda e: e.name.lower())
        except OSError as e:
            item.addChild(QTreeWidgetItem([f"(can't read: {e.strerror})"]))
            return
        for e in subs:
            ch = QTreeWidgetItem([e.name])
            ch.setData(0, Qt.UserRole, ("lib", e.path))
            ch.addChild(QTreeWidgetItem(["…"]))
            item.addChild(ch)

    def _tree_select(self, item, _prev):
        if not item or not item.data(0, Qt.UserRole):
            return
        kind, val = item.data(0, Qt.UserRole)
        if kind == "card":
            self._list_card([f for f in self._card_files_in(val)], f"{val}")
        elif kind in ("lib", "libroot"):
            self._list_lib(val)
        elif kind == "smart" and self.index:
            if val.startswith("Unused"):
                self._list_card(unused_samples(self.index), "Unused on card", show_dir=True)
            else:
                if not self.health:
                    self._run_health(then_list=True)
                else:
                    self._list_wont_play()

    def _card_files_in(self, rel_dir: str):
        pre = rel_dir.lower() + "/"
        for k, v in self.card.files.items():
            if k.startswith(pre) and "/" not in k[len(pre):] and os.path.splitext(k)[1] in AUDIO_EXTS:
                yield v

    def select_card_path(self, rel: str):
        """Jump the tree to a card folder (or the folder of a file) and select the file."""
        target = rel if rel.lower() in {d.lower() for d in self.card.dirs} else rel.rsplit("/", 1)[0]
        it = self._find_tree_item(("card", target))
        if it:
            self.tree.setCurrentItem(it)
            self.tree.scrollToItem(it)
            QTimer.singleShot(300, lambda: self._select_row_by_rel(rel))

    def _find_tree_item(self, data):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            d = it.data(0, Qt.UserRole)
            if d and d[0] == data[0] and str(d[1]).lower() == str(data[1]).lower():
                return it
            stack += [it.child(i) for i in range(it.childCount())]
        return None

    def _select_row_by_rel(self, rel):
        for r in range(self.table.rowCount()):
            row = self._row_at(r)
            if row and row.get("rel", "").lower() == rel.lower():
                self.table.selectRow(r)
                return

    # ------------------------------------------------------------------ file list
    def _list_card(self, rels, title, show_dir=False):
        rows = [{"where": "card", "rel": r, "abs": self.card.abs(r),
                 "name": r[len("SAMPLES/"):] if show_dir else os.path.basename(r)} for r in rels]
        self._fill_rows(rows, title)

    def _list_lib(self, folder):
        try:
            names = sorted(e.name for e in os.scandir(folder)
                           if e.is_file() and os.path.splitext(e.name)[1].lower() in AUDIO_EXTS)
        except OSError as e:
            names = []
            self.list_label.setText(f"Can't read {folder}: {e.strerror}")
        rows = [{"where": "lib", "abs": os.path.join(folder, n), "name": n} for n in names]
        self._fill_rows(rows, folder)

    def _fill_rows(self, rows, title):
        self.list_gen += 1
        gen = self.list_gen
        self.current = None
        self.table.setCurrentCell(-1, -1)
        self.rows = rows
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            it = QTableWidgetItem(row["name"])
            it.setData(Qt.UserRole, i)          # index into self.rows (Qt would copy a dict)
            self.table.setItem(i, 0, it)
            used = len(self.index.uses(row["rel"])) if row["where"] == "card" and self.index else None
            u = QTableWidgetItem()
            if used is not None:
                u.setData(Qt.DisplayRole, used)
            self.table.setItem(i, 3, u)
            for c in (1, 2, 4):
                self.table.setItem(i, c, QTableWidgetItem("…"))
        self.table.setSortingEnabled(True)
        self.list_label.setText(f"{title} · {len(rows)} files · reading headers…" if rows else f"{title} · no audio files")
        if not rows:
            return

        def work(progress):
            out = []
            for k, r in enumerate(rows):
                out.append(inspect(r["abs"]))
                if k % 20 == 0:
                    progress(k, len(rows), "")
            return out
        run_job(Job(work, wants_progress=True), done=lambda infos: self._rows_inspected(gen, title, infos),
                failed=self._job_failed)

    def _rows_inspected(self, gen, title, infos):
        if gen != self.list_gen:
            return
        self.table.setSortingEnabled(False)
        by_abs = {i.path: i for i in infos}
        bad = convert = 0
        for r in range(self.table.rowCount()):
            row = self._row_at(r)
            info = by_abs.get(row["abs"])
            row["info"] = info
            if not info:
                continue
            ln = QTableWidgetItem(f"{info.seconds:.2f}s")
            ln.setData(Qt.UserRole + 1, info.seconds)
            self.table.setItem(r, 1, ln)
            fmt = QTableWidgetItem(info.short_format())
            if info.rate and info.rate != 44100:
                fmt.setForeground(AMBER)
            self.table.setItem(r, 2, fmt)
            if not info.deluge_plays:
                st, col = "won't play", RED
                bad += 1
            elif info.problems:
                st, col = "plays", AMBER
                convert += 1
            else:
                st, col = "native", GREEN
            s = QTableWidgetItem(st)
            s.setForeground(col)
            self.table.setItem(r, 4, s)
        self.table.setSortingEnabled(True)
        bits = [f"{title} · {len(infos)} files"]
        if bad:
            bits.append(f"{bad} won't play on the Deluge")
        if convert:
            bits.append(f"{convert} play but aren't 44.1 kHz")
        self.list_label.setText(" · ".join(bits))
        if self.table.rowCount() and self.table.currentRow() < 0:
            self.table.selectRow(0)
        elif self.table.currentRow() >= 0:
            row = self._row_at(self.table.currentRow())
            if row is not self.current:
                self._row_select(self.table.currentRow())
            else:
                self._show_info(row)

    def _row_at(self, r):
        it = self.table.item(r, 0)
        if it is None:
            return None
        k = it.data(Qt.UserRole)
        return self.rows[k] if isinstance(k, int) and 0 <= k < len(self.rows) else None

    def _do_search(self):
        q = self.search.text().strip().lower()
        if not self.card or not q:
            return
        if q == "unused":
            if self.index:
                self._list_card(unused_samples(self.index), "Search: unused", show_dir=True)
            return
        words = q.split()
        hits = [s for s in self.card.sample_files() if all(w in s.lower() for w in words)]
        self._list_card(hits[:2000], f"Search: {q}" + (" (first 2000)" if len(hits) > 2000 else ""), show_dir=True)

    # ------------------------------------------------------------------ inspector
    def _row_select(self, r):
        if r < 0 or not self.table.item(r, 0):
            return
        row = self._row_at(r)
        if row is None:
            return
        self._stop()
        self.current = row
        self.audio = None
        self._show_info(row)
        self.wave.set_data(None, message="Loading…")
        path = row["abs"]

        def work():
            data, rate = playback.load(path)
            return path, data, rate, playback.peaks(data)
        run_job(Job(work), done=self._audio_loaded, failed=lambda e: self.wave.set_data(None, message=f"Can't read: {e}"))

    def _audio_loaded(self, res):
        path, data, rate, pk = res
        if not self.current or self.current["abs"] != path:
            return
        self.audio = (data, rate)
        info = self.current.get("info")
        loop = (info.loop_start, info.loop_end) if info and info.loop_end else None
        self.wave.set_data(pk, len(data), loop)
        if self.auto_chk.isChecked() and self.player.available:
            self._play()

    def _show_info(self, row):
        info = row.get("info") or None
        self.name_lbl.setText(row["name"])
        self.f_where.setText(row.get("rel") or row["abs"])
        if info:
            self.f_format.setText(info.describe())
            self.f_rate.setText(f"{info.rate / 1000:g} kHz" + ("  ✓ native" if info.native else "") if info.rate else "?")
            self.f_len.setText(f"{info.seconds:.3f} s · {info.frames:,} frames")
            lp = f"{info.loop_start:,} – {info.loop_end:,}" if info.loop_end else "none"
            root = f" · root note {info.root_note}" if info.root_note is not None else ""
            self.f_loop.setText(lp + root)
            msgs = []
            for p in info.problems:
                colour = "#b91c1c" if p.startswith("!") else "#9a3412"
                msgs.append(f"<span style='color:{colour}'>● {p.lstrip('!')}</span>")
            if info.error:
                msgs.append(f"<span style='color:#b91c1c'>● {info.error}</span>")
            self.problems.setText("<br/>".join(msgs) if msgs else "<span style='color:#065f46'>● Plays on the Deluge</span>")
        else:
            for w in (self.f_format, self.f_rate, self.f_len, self.f_loop, self.problems):
                w.setText("…")
        self.used.clear()
        if row["where"] == "card" and self.index:
            refs = self.index.uses(row["rel"])
            self.used_lbl.setText(f"USED BY ({len(refs)})")
            for ref in refs:
                label = ref.xml + (f" · row {ref.row}" if ref.row else "")
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, ref)
                self.used.addItem(it)
            if not refs:
                self.used.addItem("Nothing on the card uses this sample.")
        else:
            self.used_lbl.setText("USED BY")
            self.used.addItem("Library file — not on the card.")

    def _show_ref(self, item):
        ref = item.data(Qt.UserRole)
        if not ref:
            return
        text, a, b = snippet(self.card, ref)
        dlg = QDialog(self)
        dlg.setWindowTitle(ref.xml)
        dlg.resize(820, 420)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"<b>{ref.xml}</b> — {'row ' + ref.row if ref.row else ''} ({ref.form})"))
        ed = QPlainTextEdit(text)
        ed.setReadOnly(True)
        ed.setStyleSheet("font-family:Consolas,monospace;")
        cur = ed.textCursor()
        cur.setPosition(a)
        cur.setPosition(b, QTextCursor.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#fde68a"))
        cur.mergeCharFormat(fmt)
        cur.setPosition(a)
        ed.setTextCursor(cur)
        lay.addWidget(ed)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    # ------------------------------------------------------------------ audition
    def eventFilter(self, obj, ev):
        if obj is self.table and ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Space:
            self._toggle_play()
            return True
        return super().eventFilter(obj, ev)

    def _toggle_play(self):
        if self.player.playing:
            self._stop()
        else:
            self._play()

    def _play(self):
        if not self.audio or not self.player.available:
            return
        info = (self.current or {}).get("info")
        region = (info.loop_start, info.loop_end) if info and info.loop_end else None
        self.player.play(self.audio[0], self.audio[1], loop_region=region, loop=self.loop_chk.isChecked())
        self.play_btn.setText("■  Stop (Space)")
        if not self.loop_chk.isChecked():
            secs = len(self.audio[0]) / self.audio[1]
            QTimer.singleShot(int(secs * 1000) + 150, self._maybe_reset_btn)

    def _maybe_reset_btn(self):
        if not self.loop_chk.isChecked():
            self.player.playing = False
            self.play_btn.setText("▶  Play (Space)")

    def _stop(self):
        self.player.stop()
        self.play_btn.setText("▶  Play (Space)")

    def _level(self, v):
        self.player.level = v / 100
        self.settings["audition_level"] = v
        self.settings.save()

    # ------------------------------------------------------------------ health
    def _run_health(self, then_list=False):
        if not self.index:
            return
        self.status_lbl.setText("Running the health check…")
        idx, lib = self.index, self.settings["library_root"]

        def work(progress):
            return check(idx, library_files(lib), progress=progress)

        def done(rep):
            self.prog.hide()
            self.health = rep
            self.status_lbl.setText("Health check finished.")
            if then_list:
                self._list_wont_play()
            else:
                dlg = HealthDialog(rep, self.card.facts.label or self.card.root, self)
                dlg.show_path.connect(lambda p: (self.select_card_path(p)))
                dlg.show()
        run_job(Job(work, wants_progress=True), done=done, failed=self._job_failed, progress=self._progress)

    def _list_wont_play(self):
        self._list_card([f.path for f in self.health.of("convert")], "Won't play on the Deluge", show_dir=True)

    # ------------------------------------------------------------------ misc
    def _progress(self, i, n, text):
        self.prog.show()
        self.prog.setRange(0, max(n, 1))
        self.prog.setValue(i)
        if text:
            self.status_lbl.setText(text)

    def _job_failed(self, msg):
        self.prog.hide()
        self.status_lbl.setText("Something went wrong — see the log folder.")
        QMessageBox.warning(self, APP_NAME, f"{msg}\n\nNothing on the card was changed. The log folder has the details.")

    def _settings_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        lay = QFormLayout(dlg)
        path = QLineEdit(self.settings["library_root"])
        pick = QPushButton("Browse…")
        pick.clicked.connect(lambda: path.setText(QFileDialog.getExistingDirectory(dlg, "Library folder", path.text()) or path.text()))
        row = QHBoxLayout()
        row.addWidget(path, 1)
        row.addWidget(pick)
        lay.addRow("Sample library folder", row)
        lay.addRow("", QLabel("Holds FREE, FACTORY and PAID, the same layout as the card."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addRow(bb)
        dlg.resize(620, 140)
        if dlg.exec():
            self.settings["library_root"] = path.text().strip()
            self.settings.save()
            self._build_library_root()

    def closeEvent(self, e):
        self._stop()
        self.settings.save()
        super().closeEvent(e)
