"""Main window: sources tree, file list, inspector (spec section 3.1). Read-only in M1/M2."""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QInputDialog, QMenu, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
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
from dsm.edit import edits as ed
from dsm.edit import factory
from dsm.importer import apply as ap
from dsm.importer.mapping import Mapping
from dsm.importer.plan import expand
from dsm.settings import Settings, app_dir
from dsm.ui.apply_dialog import ApplyDialog, HistoryDialog
from dsm.ui.health_dialog import HealthDialog
from dsm.ui.import_dialog import ImportDialog
from dsm.ui.widgets import Job, Waveform, run_job

log = logging.getLogger("dsm.ui")
AMBER, RED, GREEN, GREY = QColor("#9a3412"), QColor("#b91c1c"), QColor("#065f46"), QColor("#6b6f77")
BROWSE = "Browse for a card or folder…"
USB = "usb"
USB_AUTOLOAD = 400_000        # over USB, samples up to this size load as soon as they are selected


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
        self.mapping = Mapping(settings["library_root"])
        self.pending = []            # ImportBatch list — nothing written until Apply
        self.backed_up_this_session = False
        self.factory_rec = None
        self._offered_init = set()
        self.setAcceptDrops(True)
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
        self.import_act = QAction("Import…", self)
        self.import_act.setToolTip("Choose files to put on the card (or drag them onto the window)")
        self.import_act.setEnabled(False)
        self.import_act.triggered.connect(self._import_files_dialog)
        tb.addAction(self.import_act)
        h = QAction("History", self)
        h.setToolTip("Every change made to a card, with Undo")
        h.triggered.connect(lambda: HistoryDialog(self, self).exec())
        tb.addAction(h)
        self.factory_act = QAction("Initialize Factory Samples", self)
        self.factory_act.setToolTip("Move the factory samples under SAMPLES/FACTORY, record them, copy them to the "
                                    "library and lock them — the first thing to do with a card")
        self.factory_act.setVisible(False)
        self.factory_act.triggered.connect(self._initialize_factory)
        tb.addAction(self.factory_act)
        self.record_act = QAction("Factory record", self)
        self.record_act.setToolTip("The record of the factory samples, and their copy in the library")
        self.record_act.setVisible(False)
        self.record_act.triggered.connect(self._record_dialog)
        tb.addAction(self.record_act)
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
        badge = QLabel("  Nothing is written to the card until you press Apply  ")
        badge.setStyleSheet("background:#d8f0e4;color:#065f46;font-weight:600;border-radius:4px;padding:3px;")
        tb.addWidget(badge)

    def _build_body(self):
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        split = QSplitter()
        cl.addWidget(split, 1)
        self.tray = QWidget()
        tl = QHBoxLayout(self.tray)
        tl.setContentsMargins(6, 4, 6, 4)
        self.tray.setStyleSheet("background:#fff7ed;")
        self.tray_list = QListWidget()
        self.tray_list.setMaximumHeight(70)
        tl.addWidget(QLabel("<b style='color:#7c2d12'>Pending</b><br/><span style='color:#7c2d12'>not written yet</span>"))
        tl.addWidget(self.tray_list, 1)
        rm = QPushButton("Remove")
        rm.clicked.connect(self._remove_pending)
        self.apply_btn = QPushButton("Apply…")
        self.apply_btn.setStyleSheet("background:#2563eb;color:white;font-weight:600;padding:6px 14px;")
        self.apply_btn.clicked.connect(self._apply)
        tl.addWidget(rm)
        tl.addWidget(self.apply_btn)
        self.tray.hide()
        cl.addWidget(self.tray)
        self.setCentralWidget(central)
        # sources
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._tree_select)
        self.tree.itemExpanded.connect(self._tree_expand)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
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
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Name", "Origin", "On card", "Length", "kHz/bit", "Used", "Deluge"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 7):
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
        self.f_link = QLabel()
        self.f_link.setWordWrap(True)
        self.f_link.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Original / copy", self.f_link)
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
        if not cards and last == USB:
            self._open_usb()
        elif target:
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
        self.card_box.addItem("Deluge over USB", USB)
        self.card_box.addItem(BROWSE, None)
        if self.card:
            i = self.card_box.findData(USB if self.card.is_usb else self.card.root)
            if i >= 0:
                self.card_box.setCurrentIndex(i)

    def _card_chosen(self, i):
        path = self.card_box.itemData(i)
        if path == USB:
            self._open_usb()
            return
        if path is None:
            path = QFileDialog.getExistingDirectory(self, "Choose the card (or a copy of it)")
            if not path:
                return
        self._open_card(path)

    def _refresh(self):
        self._fill_card_box()
        if self.card and self.card.is_usb:
            self._open_usb(reuse=True)
        elif self.card and os.path.isdir(self.card.root):
            self._open_card(self.card.root)
        elif self.card:
            self.status_lbl.setText("The card was removed.")
            self.card = self.index = None
            self._build_card_tree()
        else:
            self._startup()

    def _close_usb(self):
        if self.card is not None and self.card.is_usb:
            try:
                self.card.link.close_link()
            except Exception:  # noqa: BLE001
                pass

    def _open_usb(self, reuse=False):
        """Connect to the Deluge over its USB cable and read its card."""
        self._stop()
        old = self.card if (reuse and self.card is not None and self.card.is_usb) else None
        if not old:
            self._close_usb()
        self.status_lbl.setText("Looking for the Deluge on USB …")
        self.health_act.setEnabled(False)

        def work(progress):
            from dsm.usb.link import connect
            from dsm.usb.usbcard import UsbCard
            c = old or UsbCard(connect())
            c.scan(progress=progress)
            progress(0, 1, "Reading the Deluge's presets and songs (first time is slow; after that they come from "
                           "the PC's copy) …")
            return c, build_index(c, progress=progress)

        def failed(msg):
            self.prog.hide()
            self.status_lbl.setText("Not connected to the Deluge.")
            QMessageBox.warning(self, APP_NAME, msg.split(": ", 1)[-1])
        run_job(Job(work, wants_progress=True), done=self._card_ready, failed=failed, progress=self._progress)

    def _open_card(self, path: str):
        self._stop()
        self._close_usb()
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
        self.settings["last_card"] = USB if self.card.is_usb else self.card.root
        self.settings.save()
        self._fill_card_box()
        f = self.card.facts
        n_s, n_x = len(self.card.sample_files()), len(self.index.docs)
        free = f" · {human_size(f.free)} free of {human_size(f.total)}" if f.total else ""
        via = f" · over USB ({getattr(self.card.link, 'port_name', '')})" if self.card.is_usb else ""
        self.status_lbl.setText(f"{f.label or self.card.root} · {f.fs or '?'}{free} · {n_s} samples · {n_x} presets "
                                f"and songs · {len(self.index.missing)} missing references{via}")
        if f.warnings:
            self.warn.setText("  ·  ".join(f.warnings))
            self.warn.show()
        else:
            self.warn.hide()
        self.health_act.setEnabled(True)
        self.import_act.setEnabled(True)
        self.factory_rec = factory.load(self.settings["library_root"], self.card)
        if self.factory_rec and self.factory_rec.undone:
            self.factory_rec = None
        self.record_act.setVisible(self.factory_rec is not None)
        self.factory_act.setVisible(self.factory_rec is None)
        QTimer.singleShot(400, self._maybe_offer_initialize)
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
                if d.split("/")[0].upper() == "DSM":          # the app's own data folder on the card
                    continue
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
            subs = sorted((e for e in os.scandir(path) if e.is_dir() and not e.name.startswith(".")
                           and e.name != "_DSM"),          # the app's own data folder
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
            on_card = self._on_card(row)
            row["on_card"] = on_card
            if on_card:
                it.setForeground(GREEN)
                f = it.font()
                f.setBold(True)
                it.setFont(f)
                it.setToolTip("On the Deluge's card" + (f": {on_card}" if isinstance(on_card, str) else ""))
            self.table.setItem(i, 0, it)
            org = self._origin(row)
            row["origin"] = org
            o = QTableWidgetItem(("🔒 " if org == "Factory" else "") + org)
            if org == "Factory":
                o.setForeground(QColor("#1d4ed8"))
                o.setToolTip("Factory sample — locked: it can't be renamed, moved, trashed or overwritten")
            self.table.setItem(i, 1, o)
            oc = QTableWidgetItem("✓" if on_card else "")
            oc.setForeground(GREEN)
            oc.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 2, oc)
            used = len(self.index.uses(row["rel"])) if row["where"] == "card" and self.index else None
            u = QTableWidgetItem()
            if used is not None:
                u.setData(Qt.DisplayRole, used)
            self.table.setItem(i, 5, u)
            for c in (3, 4, 6):
                self.table.setItem(i, c, QTableWidgetItem("…"))
        self.table.setSortingEnabled(True)
        self.list_label.setText(f"{title} · {len(rows)} files · reading headers…" if rows else f"{title} · no audio files")
        if not rows:
            return

        card = self.card

        def work(progress):
            out = []
            for k, r in enumerate(rows):
                out.append(card.inspect(r["rel"]) if r["where"] == "card" else inspect(r["abs"]))
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
            self.table.setItem(r, 3, ln)
            fmt = QTableWidgetItem(info.short_format())
            if info.rate and info.rate != 44100:
                fmt.setForeground(AMBER)
            self.table.setItem(r, 4, fmt)
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
            self.table.setItem(r, 6, s)
        self.table.setSortingEnabled(True)
        bits = [f"{title} · {len(infos)} file{'s' if len(infos) != 1 else ''}"]
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

    def _lib_rel(self, path):
        lib = self.settings["library_root"]
        try:
            rel = os.path.relpath(path, lib).replace(os.sep, "/")
        except ValueError:
            return None
        return None if rel.startswith("..") else rel

    def _origin(self, row) -> str:
        if row["where"] == "card":
            return factory.origin(row["rel"], on_card=True)
        rel = self._lib_rel(row["abs"])
        return factory.origin(rel, on_card=False) if rel else "Other"

    def _on_card(self, row):
        """True (or the card path) when this sample is on the Deluge's card."""
        if row["where"] == "card":
            return True
        if not self.card:
            return False
        rel = self._lib_rel(row["abs"])
        if not rel:
            return False
        for link in self.mapping.for_library(rel):
            if link.card.lower() in self.card.files:
                return link.card
        rec = getattr(self, "factory_rec", None)
        if rec:
            for e in rec.entries:
                if e.library and e.library.lower() == rel.lower() and e.card.lower() in self.card.files:
                    return e.card
        return False

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
        if row["where"] == "card" and self.card.is_usb:
            try:
                size = self.card.stat(row["rel"])[0]
            except FileNotFoundError:
                size = 0
            if size > USB_AUTOLOAD:
                from dsm.usb.usbcard import eta
                self.wave.set_data(None, message=f"On the Deluge over USB ({human_size(size)}) — press Play to fetch it, "
                                                 f"{eta(size)}")
                return
        self._load_audio(row)

    def _load_audio(self, row, then_play=False):
        self.wave.set_data(None, message="Loading…" if row["where"] == "lib" or not self.card.is_usb
                           else "Fetching from the Deluge over USB…")
        path, card = row["abs"], self.card
        rel = row.get("rel")

        def work():
            src = card.read_for_playback(rel) if row["where"] == "card" and card.is_usb else path
            data, rate = playback.load(src)
            return path, data, rate, playback.peaks(data), then_play
        run_job(Job(work), done=self._audio_loaded, failed=lambda e: self.wave.set_data(None, message=f"Can't read: {e}"))

    def _audio_loaded(self, res):
        path, data, rate, pk, then_play = res
        if not self.current or self.current["abs"] != path:
            return
        self.audio = (data, rate)
        info = self.current.get("info")
        loop = (info.loop_start, info.loop_end) if info and info.loop_end else None
        self.wave.set_data(pk, len(data), loop)
        if (then_play or self.auto_chk.isChecked()) and self.player.available:
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
        self.f_link.setText(self._link_text(row))
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
        if not self.audio and self.current and self.player.available:
            self._load_audio(self.current, then_play=True)      # not fetched yet (big file over USB)
            return
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
            rep = check(idx, library_files(lib), progress=progress)
            rec = factory.load(lib, idx.card)
            if rec and not rec.undone:
                from dsm.index.health import Finding
                for kind, text in factory.check(rec, idx.card):
                    rep.findings.insert(0, Finding("factory", text, "Missing from the card" if kind == "missing" else
                                                   "Changed since the factory record was made", text.split(" ")[0]))
                rep.counts["factory"] = len(rep.of("factory"))
            return rep

        def done(rep):
            self.prog.hide()
            self.health = rep
            self.status_lbl.setText("Health check finished.")
            if then_list:
                self._list_wont_play()
            else:
                dlg = HealthDialog(rep, self.card.facts.label or self.card.root, self)
                dlg.show_path.connect(lambda p: (self.select_card_path(p)))
                dlg.relink.connect(lambda old, new: self._add_edit(ed.EditBatch(
                    f"Relink {old.rsplit('/', 1)[-1]} → {new}", relinks=[(old, new)])))
                dlg.factory.connect(self._initialize_factory)
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
        lay.addRow("", QLabel("Holds FREE, FACTORY and PAID. Keeps the originals; the card gets converted copies."))
        bpath = QLineEdit(self.settings.get("backup_root") or os.path.join(app_dir(), "Backups"))
        bpick = QPushButton("Browse…")
        bpick.clicked.connect(lambda: bpath.setText(QFileDialog.getExistingDirectory(dlg, "Backup folder", bpath.text()) or bpath.text()))
        brow = QHBoxLayout()
        brow.addWidget(bpath, 1)
        brow.addWidget(bpick)
        lay.addRow("Backup folder", brow)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addRow(bb)
        dlg.resize(620, 140)
        if dlg.exec():
            self.settings["library_root"] = path.text().strip()
            self.settings["backup_root"] = bpath.text().strip()
            self.settings.save()
            self.mapping = Mapping(self.settings["library_root"])
            self._build_library_root()

    # ------------------------------------------------------------------ import (M3)
    def _link_text(self, row) -> str:
        if row["where"] == "card":
            rec = getattr(self, "factory_rec", None)
            e = rec.by_card().get(row["rel"].lower()) if rec else None
            if e:
                copy = f" · untouched copy in the library: {e.library}" if e.library else " · not yet copied to the library"
                return f"Factory sample — originally {e.original}{copy}"
            link = self.mapping.for_card(row["rel"])
            return f"Original in the library: {link.library}" if link else "—"
        lib = self.settings["library_root"]
        try:
            rel = os.path.relpath(row["abs"], lib).replace(os.sep, "/")
        except ValueError:
            return "—"
        links = self.mapping.for_library(rel)
        return ("On the card as: " + ", ".join(x.card for x in links)) if links else "Not on the card"

    def _import_files_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Files to put on the card", self.settings["library_root"],
                                                "Audio (*.wav *.wave *.aif *.aiff *.aifc *.flac *.ogg *.mp3)")
        if paths:
            self._start_import([(p, None) for p in paths])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self._start_import(expand(paths))

    def _table_menu(self, pos):
        rows = [self._row_at(i.row()) for i in self.table.selectionModel().selectedRows()]
        lib_rows = [r for r in rows if r and r["where"] == "lib"]
        card_rows = [r for r in rows if r and r["where"] == "card"]
        m = QMenu(self)
        acts = {}
        if lib_rows:
            acts[m.addAction(f"Send {len(lib_rows)} to the card…")] = "send"
        locked = [r for r in card_rows if factory.is_factory_card(r["rel"])]
        if locked:
            a = m.addAction(f"🔒 {len(locked)} factory sample{'s are' if len(locked) != 1 else ' is'} locked")
            a.setEnabled(False)
        card_rows = [r for r in card_rows if not factory.is_factory_card(r["rel"])]
        if card_rows and self.index:
            if len(card_rows) == 1:
                acts[m.addAction("Rename…")] = "rename"
            acts[m.addAction(f"Move {len(card_rows)} to another folder…")] = "move"
            unused = [r for r in card_rows if not self.index.uses(r["rel"])]
            if unused:
                acts[m.addAction(f"Move {len(unused)} unused to the card trash")] = "trash"
        if not acts:
            return
        what = acts.get(m.exec(self.table.viewport().mapToGlobal(pos)))
        if what == "send":
            self._start_import([(r["abs"], None) for r in lib_rows])
        elif what == "rename":
            self._rename_file(card_rows[0]["rel"])
        elif what == "move":
            self._move_files([r["rel"] for r in card_rows])
        elif what == "trash":
            rels = [r["rel"] for r in card_rows if not self.index.uses(r["rel"])]
            self._add_edit(ed.EditBatch(f"Move {len(rels)} unused samples to the card trash", trash=rels))

    def _tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        d = it.data(0, Qt.UserRole) if it else None
        if d and d[0] == "card" and (d[1].upper() + "/").startswith("SAMPLES/FACTORY/"):
            m = QMenu(self)
            a = m.addAction("🔒 Factory samples are locked")
            a.setEnabled(False)
            m.exec(self.tree.viewport().mapToGlobal(pos))
            return
        if d and d[0] == "card" and d[1].upper().startswith("SAMPLES/") and self.index:
            m = QMenu(self)
            ren = m.addAction("Rename folder…")
            mov = m.addAction("Move folder into…")
            got = m.exec(self.tree.viewport().mapToGlobal(pos))
            if got is ren:
                self._rename_folder(d[1])
            elif got is mov:
                self._move_folder(d[1])
            return
        if not d or d[0] not in ("lib", "libroot"):
            return
        m = QMenu(self)
        a = m.addAction("Send this whole folder to the card…")
        if m.exec(self.tree.viewport().mapToGlobal(pos)) is a:
            self._start_import([(f, None) for f, _ in expand([d[1]])])

    # ------------------------------------------------------------------ rename / move / relink (M4)
    def _add_edit(self, batch):
        if not batch.active:
            return
        edits = [b for b in self.pending if getattr(b, "kind", "") == "edit"]
        before = set(ed.plan(edits, self.index).problems) if edits else set()
        p = ed.plan(edits + [batch], self.index)
        new = [x for x in p.problems if x not in before]
        if new:
            QMessageBox.warning(self, APP_NAME, "That change can't be made:\n\n" + "\n".join(new[:12]))
            return
        self.pending.append(batch)
        self._update_tray()
        refs = ed.plan([batch], self.index).ref_count
        self.status_lbl.setText(f"Added to Pending: {batch.title}" + (f" — {refs} preset references will follow" if refs else ""))

    def _sample_dirs(self):
        return sorted((d for d in self.card.dirs if d.upper().startswith("SAMPLES")
                       and not (d.upper() + "/").startswith("SAMPLES/FACTORY/")), key=str.lower)

    def _rename_file(self, rel):
        folder, name = rel.rsplit("/", 1)
        stem, ext = os.path.splitext(name)
        new, ok = QInputDialog.getText(self, "Rename sample", f"New name for {name}\n"
                                       f"(every kit, synth and song that uses it will follow)", text=stem)
        new = new.strip()
        if not ok or not new or new == stem:
            return
        if not os.path.splitext(new)[1]:
            new += ext
        if "/" in new or "\\" in new:
            QMessageBox.warning(self, APP_NAME, "Use Move to put it in another folder.")
            return
        self._add_edit(ed.EditBatch(f"Rename {name} → {new}", moves=[(rel, f"{folder}/{new}")]))

    def _pick_folder(self, title, exclude=None):
        dirs = [d for d in self._sample_dirs() if not (exclude and (d == exclude or d.startswith(exclude + "/")))]
        target, ok = QInputDialog.getItem(self, title, "Folder on the card (pick one, or type a new path):",
                                          dirs, 0, True)
        target = target.strip().strip("/").replace("\\", "/")
        if not ok or not target:
            return None
        if not target.upper().startswith("SAMPLES"):
            target = "SAMPLES/" + target
        return target

    def _move_files(self, rels):
        target = self._pick_folder(f"Move {len(rels)} sample{'s' if len(rels) != 1 else ''}")
        if target:
            self._add_edit(ed.EditBatch(f"Move {len(rels)} sample{'s' if len(rels) != 1 else ''} → {target}",
                                        moves=[(r, f"{target}/{r.rsplit('/', 1)[1]}") for r in rels]))

    def _rename_folder(self, d):
        parent, name = d.rsplit("/", 1)
        new, ok = QInputDialog.getText(self, "Rename folder", f"New name for {name}", text=name)
        new = new.strip().strip("/")
        if ok and new and new != name and "/" not in new:
            self._add_edit(ed.folder_move(self.card, d, f"{parent}/{new}", f"Rename folder {d} → {new}"))

    def _move_folder(self, d):
        target = self._pick_folder(f"Move folder {d.rsplit('/', 1)[1]} into", exclude=d)
        if target:
            self._add_edit(ed.folder_move(self.card, d, f"{target}/{d.rsplit('/', 1)[1]}"))

    def _offer_factory_copy(self, rec):
        lib = self.settings["library_root"]
        size = human_size(rec.total_bytes)
        if not lib or not os.path.isdir(lib):
            QMessageBox.warning(self, APP_NAME, "The factory record is saved on the card, but the library folder isn't "
                                "reachable, so the factory samples can't be copied there yet. Check Settings, then use "
                                "Factory record → Copy.")
            return
        if self.card.is_usb:
            from dsm.usb.usbcard import eta
            QMessageBox.information(self, "Factory record made",
                                    f"The record of {len(rec.entries)} factory samples is saved in the library and on "
                                    f"the card.\n\nCopying the samples themselves ({size}) over USB would take "
                                    f"{eta(rec.total_bytes)}. Put the card in the reader and use Factory record → "
                                    "Copy to the library — it takes minutes there.")
            return
        if QMessageBox.question(self, "Factory record made",
                                f"The record of {len(rec.entries)} factory samples is saved in the library and on the "
                                f"card.\n\nCopy the factory samples themselves ({size}) into the library's FACTORY "
                                "folder now, untouched, with a checksum for each?") == QMessageBox.Yes:
            self._copy_factory(rec)

    def _copy_factory(self, rec):
        card, lib, mapping = self.card, self.settings["library_root"], self.mapping

        def work(progress):
            return factory.copy_to_library(rec, card, lib, mapping, progress=progress)

        def done(out):
            self.prog.hide()
            text = f"{out['copied']} factory samples copied into the library, {out['skipped']} already there."
            if out["problems"]:
                box = QMessageBox(QMessageBox.Warning, "Factory copy", text + f"\n{len(out['problems'])} problems.")
                box.setDetailedText("\n".join(out["problems"]))
                box.exec()
            else:
                QMessageBox.information(self, "Factory copy", text + "\nEvery copy has a checksum in the record.")
            self.factory_rec = rec
            self._build_library_root()
            if self.current:
                self._show_info(self.current)
        run_job(Job(work, wants_progress=True), done=done, failed=self._job_failed, progress=self._progress)

    def _record_dialog(self):
        rec = factory.load(self.settings["library_root"], self.card)
        if not rec:
            return
        problems = factory.check(rec, self.card)
        dlg = QDialog(self)
        dlg.setWindowTitle("Factory record")
        dlg.resize(820, 520)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            f"Made {rec.made} on {rec.card_label or 'the card'}. <b>{len(rec.entries)}</b> factory samples, "
            f"{human_size(rec.total_bytes)}.<br/><b>{rec.copied}</b> of them copied untouched into the library "
            f"(FACTORY folder) with a checksum.<br/>" +
            (f"<span style='color:#b91c1c'>{len(problems)} missing or changed on the card — see below.</span>"
             if problems else "<span style='color:#065f46'>Every factory sample is on the card, the size the factory "
                              "made it.</span>") +
            "<br/><br/>Saved in the library as _DSM/factory-record.json and on the card as DSM/FACTORY_RECORD.JSON."))
        box = QPlainTextEdit("\n".join([f"{k.upper():8} {t}" for k, t in problems] + ([""] if problems else []) +
                                       [f"{e.original}\n    now {e.card}" + (f"\n    copy {e.library}" if e.library else "")
                                        for e in rec.entries]))
        box.setReadOnly(True)
        box.setStyleSheet("font-family:Consolas,monospace;")
        lay.addWidget(box, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        left = len(rec.entries) - rec.copied
        if left:
            b = bb.addButton(f"Copy the {left} remaining to the library", QDialogButtonBox.ActionRole)
            if self.card.is_usb:
                from dsm.usb.usbcard import eta
                rest = sum(e.size for e in rec.entries if not (e.sha1 and e.library))
                b.setToolTip(f"Over USB this takes {eta(rest)}; with the card reader, minutes.")
            b.clicked.connect(lambda: (dlg.accept(), self._copy_factory(rec)))
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def _choose_factory_folders(self, folders, loose, existing):
        """The Initialize dialog: which folders are factory. Returns (folders, loose) or None."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Initialize Factory Samples")
        dlg.resize(680, 600)
        lay = QVBoxLayout(dlg)
        usb = self.card.is_usb
        intro = QLabel(
            "<b>Run this once, as the first thing you do with a card.</b> It does three things:<br/>"
            "1. Moves the factory samples under <b>SAMPLES/FACTORY</b>, and updates every kit, synth and song that uses "
            "them.<br/>"
            "2. Makes the <b>factory record</b>: every factory sample's original path, new path and size, saved in the "
            "library and on the card.<br/>"
            "3. Copies every factory sample, untouched and read-only, into the library's FACTORY folder, with a "
            "checksum." + (" <span style='color:#9a3412'>(Over USB this step waits for the card reader.)</span>"
                           if usb else "") +
            "<br/><br/>From then on factory samples are <b>locked</b>: they can't be renamed, moved, trashed or "
            "overwritten.<br/><br/>These are the folders in SAMPLES outside FREE and PAID. On a card that came from "
            "the factory they are all factory samples. Untick anything that is yours.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        lst = QListWidget()
        for f in folders:
            n = sum(1 for k in self.card.files if k.startswith(f"samples/{f.lower()}/"))
            it = QListWidgetItem(f"SAMPLES/{f}   ({n} file{'s' if n != 1 else ''})")
            it.setData(Qt.UserRole, f)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            lst.addItem(it)
        if loose:
            it = QListWidgetItem(f"{len(loose)} loose file{'s' if len(loose) != 1 else ''} directly in SAMPLES")
            it.setData(Qt.UserRole, None)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            lst.addItem(it)
        if existing:
            it = QListWidgetItem(f"{len(existing)} already in SAMPLES/FACTORY — recorded and copied as they are")
            it.setFlags(Qt.ItemIsEnabled)
            lst.addItem(it)
        lay.addWidget(lst, 1)
        info = QLabel()
        lay.addWidget(info)
        bb = QDialogButtonBox()
        ok_btn = bb.addButton("Continue…", QDialogButtonBox.AcceptRole)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)

        def chosen():
            fs = [lst.item(i).data(Qt.UserRole) for i in range(lst.count())
                  if lst.item(i).flags() & Qt.ItemIsUserCheckable and lst.item(i).checkState() == Qt.Checked]
            return [f for f in fs if f], (loose if None in fs else [])

        def update(*_):
            fs, ls = chosen()
            b = ed.factory_move(self.card, fs, ls)
            p = ed.plan([b], self.index)
            info.setText(f"<b>{len(p.moves)}</b> files move · <b>{len(p.xml)}</b> preset and song files change "
                         f"({p.ref_count} references) · <b>{len(p.moves) + len(existing)}</b> factory samples recorded"
                         + ("" if not p.problems else f"<br/><span style='color:#b91c1c'>{p.problems[0]}</span>"))
            ok_btn.setEnabled(bool(p.moves or existing) and not p.problems)
        lst.itemChanged.connect(update)
        update()
        return chosen() if dlg.exec() else None

    def _initialize_factory(self):
        if not self.card or not self.index:
            return
        if self.pending:
            QMessageBox.information(self, APP_NAME, "Apply or remove the pending changes first, then initialize.")
            return
        lib = self.settings["library_root"]
        rec = factory.load(lib, self.card)
        if rec and not rec.undone:
            QMessageBox.information(self, APP_NAME, "This card's factory samples are already initialized.")
            self._record_dialog()
            return
        folders, loose = ed.factory_candidates(self.card)
        existing = [v for k, v in self.card.files.items() if k.startswith("samples/factory/")]
        if not folders and not loose and not existing:
            QMessageBox.information(self, APP_NAME, "No factory samples found: SAMPLES has nothing outside FREE and PAID.")
            return
        pick = self._choose_factory_folders(folders, loose, existing)
        if pick is None:
            return
        fs, ls = pick
        batch = ed.factory_move(self.card, fs, ls)
        p = ed.plan([batch], self.index)
        backup_root = self.settings.get("backup_root") or os.path.join(app_dir(), "Backups")
        full = not self.backed_up_this_session
        if batch.moves:
            dlg = ApplyDialog([batch], self.card, backup_root, full, p, self)
            if not dlg.exec() or dlg.choice != "apply":
                if dlg.choice == "dry":
                    box = QMessageBox(QMessageBox.Information, "Dry run", "Nothing was written. This is what would move:")
                    box.setDetailedText("\n".join(p.lines(limit_per_xml=1000)))
                    box.exec()
                return
        card, mapping = self.card, self.mapping
        lib_ok = bool(lib and os.path.isdir(lib))

        def work(progress):
            moved, jid = [], ""
            if batch.moves:
                j = ed.apply(p, [batch], card, mapping, backup_root, full, progress=progress)
                moved, jid = [tuple(x) for x in j.moved], j.id
            moved_new = {n.lower() for _, n in moved}
            moved += [("SAMPLES/" + v[len("SAMPLES/FACTORY/"):], v) for v in existing if v.lower() not in moved_new]
            rec = factory.make(card, moved, jid)
            problems = factory.save(rec, lib if lib_ok else None, card)
            copy = None
            if lib_ok and not card.is_usb:
                copy = factory.copy_to_library(rec, card, lib, mapping, progress=progress)
            return rec, problems, copy

        def done(res):
            rec, problems, copy = res
            self.prog.hide()
            self.backed_up_this_session = True
            self.factory_rec = rec
            lines = [f"{len(rec.entries)} factory samples are under SAMPLES/FACTORY and recorded."]
            if copy:
                lines.append(f"{copy['copied'] + copy['skipped']} copied untouched into the library's FACTORY folder "
                             "(read-only, with checksums).")
                problems = problems + copy["problems"]
            elif self.card.is_usb:
                lines.append("The copy into the library waits for the card reader: use Factory record → Copy.")
            elif not lib_ok:
                lines.append("The library folder isn't reachable, so nothing was copied yet: check Settings, then use "
                             "Factory record → Copy.")
            lines.append("Factory samples are now locked. Undo is in History.")
            box = QMessageBox(QMessageBox.Warning if problems else QMessageBox.Information, "Factory samples initialized",
                              "\n\n".join(lines))
            if problems:
                box.setDetailedText("\n".join(problems))
            box.exec()
            self._build_library_root()
            self._refresh()

        def failed(msg):
            self.prog.hide()
            QMessageBox.warning(self, "Initialize stopped", f"{msg}\n\nThe card was put back as it was.")
            self._refresh()
        run_job(Job(work, wants_progress=True), done=done, failed=failed, progress=self._progress)

    def _maybe_offer_initialize(self):
        """First thing on a card that hasn't been initialized: offer it once per card per session."""
        key = (self.card.root, self.card.facts.serial)
        if key in self._offered_init or self.factory_rec is not None or self.pending:
            return
        folders, loose = ed.factory_candidates(self.card)
        existing = any(k.startswith("samples/factory/") for k in self.card.files)
        if not (folders or loose or existing):
            return
        self._offered_init.add(key)
        if QMessageBox.question(self, "Initialize Factory Samples",
                                "This card's factory samples haven't been initialized yet.\n\nInitialize them now? "
                                "This moves them under SAMPLES/FACTORY, records them, copies them into the library, "
                                "and locks them. You see everything before it happens.") == QMessageBox.Yes:
            self._initialize_factory()

    def _start_import(self, files):
        if not self.card:
            QMessageBox.information(self, APP_NAME, "Open the card first, then import.")
            return
        if not files:
            QMessageBox.information(self, APP_NAME, "No audio files there.")
            return
        self.status_lbl.setText(f"Reading {len(files)} files…")

        def work(progress):
            out = {}
            for k, (f, _) in enumerate(files):
                out[f] = inspect(f)
                if k % 10 == 0:
                    progress(k, len(files), os.path.basename(f))
            return out

        def done(infos):
            self.prog.hide()
            self.status_lbl.setText("")
            dests = [i.card_rel for b in self.pending if getattr(b, "kind", "import") != "edit" for i in b.active]
            dests += [n for b in self.pending if getattr(b, "kind", "") == "edit" for _, n in b.moves]
            dlg = ImportDialog(files, infos, self.card, self.settings["library_root"], self.mapping, dests, self)
            if dlg.exec() and dlg.batch and dlg.batch.active:
                self.pending.append(dlg.batch)
                self._update_tray()
        run_job(Job(work, wants_progress=True), done=done, failed=self._job_failed, progress=self._progress)

    def _update_tray(self):
        self.tray_list.clear()
        for b in self.pending:
            self.tray_list.addItem(b.summary())
        self.tray.setVisible(bool(self.pending))
        n = sum(len(b.active) for b in self.pending)
        self.apply_btn.setText(f"Apply {n} change{'s' if n != 1 else ''}…")

    def _remove_pending(self):
        r = self.tray_list.currentRow()
        if r < 0 and self.pending:
            r = len(self.pending) - 1
        if 0 <= r < len(self.pending):
            del self.pending[r]
            self._update_tray()

    def _apply(self, dry=False):
        if not self.pending or not self.card:
            return
        backup_root = self.settings.get("backup_root") or os.path.join(app_dir(), "Backups")
        full = not self.backed_up_this_session
        imports = [b for b in self.pending if getattr(b, "kind", "import") != "edit"]
        edits = [b for b in self.pending if getattr(b, "kind", "") == "edit"]
        eplan = ed.plan(edits, self.index) if edits else None
        dlg = ApplyDialog(self.pending, self.card, backup_root, full, eplan, self)
        if not dlg.exec() or not dlg.choice:
            return
        dry = dlg.choice == "dry"
        card, lib, mapping = self.card, self.settings["library_root"], self.mapping

        imports_done = []
        made_record = []

        def work(progress):
            out = []
            if imports:
                out.append(ap.apply(imports, card, lib if lib and os.path.isdir(lib) else None, mapping, backup_root,
                                    full, dry_run=dry, progress=progress))
                imports_done.append(True)
            if edits:
                # plan again against the card as it is now (imports only add files, but be exact)
                from dsm.index.index import build_index
                card.scan()
                idx = build_index(card)
                p = ed.plan(edits, idx)
                j = ed.apply(p, edits, card, mapping, backup_root, full and not imports, dry_run=dry, progress=progress)
                out.append(j)
                if not dry and any(getattr(b, "factory", False) for b in edits):
                    rec = factory.make(card, [tuple(x) for x in j.moved], j.id)
                    j.log.extend(factory.save(rec, lib if lib and os.path.isdir(lib) else None, card))
                    j.log.append(f"factory record made: {len(rec.entries)} samples")
                    made_record.append(rec)
            return out

        def done(js):
            self.prog.hide()
            if dry:
                box = QMessageBox(QMessageBox.Information, "Dry run", "Nothing was written. This is what Apply would do:")
                box.setDetailedText("\n".join(line for j in js for line in j.log))
                box.exec()
                return
            self.backed_up_this_session = True
            self.pending.clear()
            self._update_tray()
            msg = []
            for j in js:
                if j.kind == "edit":
                    msg.append(f"{len(j.moved)} files moved or renamed, {len(j.trashed)} to the trash, "
                               f"{len(j.xml_after)} preset and song files updated — all checked.")
                else:
                    msg.append(f"{len(j.created_card)} files written to the card and checked; "
                               f"{len(j.created_library)} originals copied into the library.")
            QMessageBox.information(self, "Done", "\n".join(msg) + "\n\nUndo is in History.")
            self._build_library_root()
            self._refresh()
            if made_record:
                self.factory_rec = made_record[0]
                QTimer.singleShot(300, lambda: self._offer_factory_copy(made_record[0]))

        def failed(msg):
            self.prog.hide()
            if imports_done and not dry and edits:
                # the import step finished before the edit step failed: those files are on the card now
                self.pending = [b for b in self.pending if getattr(b, "kind", "") == "edit"]
                self._update_tray()
            QMessageBox.warning(self, "Apply stopped", f"{msg}\n\nThe step that failed was undone, so the card is as it "
                                "was before it. History shows anything that did finish. The pending changes are "
                                "still there.")
            self._refresh()
        run_job(Job(work, wants_progress=True), done=done, failed=failed, progress=self._progress)

    def closeEvent(self, e):
        if self.pending and QMessageBox.question(
                self, APP_NAME, "There are pending changes that haven't been applied. Quit and discard them?") \
                != QMessageBox.Yes:
            e.ignore()
            return
        self._stop()
        self._close_usb()
        self.settings.save()
        super().closeEvent(e)
