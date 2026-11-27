"""Main window (spec v2.2 section 5.1): the left pane (samples by type, the library's folders, the Deluge, smart lists), the sample
list and the details. Every change to a device goes into the pending tray; nothing is written until Apply."""
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
from dsm.library import getdevice, layout, reorganize
from dsm.library import sources as lsrc
from dsm.library import types as stypes
from dsm.library.db import LibraryDB
from dsm.library.scanner import info_from_row, scan as scan_library
from dsm.importer.mapping import Mapping
from dsm.importer.plan import expand
from dsm.settings import Settings, app_dir
from dsm.ui.apply_dialog import ApplyDialog, HistoryDialog
from dsm.ui.duplicates_dialog import DuplicatesDialog
from dsm.ui.health_dialog import HealthDialog
from dsm.ui.import_dialog import ImportDialog
from dsm.ui.library_dialogs import AlertsDialog, GetDialog, ReorganizeDialog
from dsm.ui.syntakt_ui import SyntaktMixin
from dsm.ui.tags_ui import TagsMixin
from dsm.ui.type_dialog import TypeDialog
from dsm.ui.widgets import Job, Waveform, run_job

log = logging.getLogger("dsm.ui")
AMBER, RED, GREEN, GREY = QColor("#9a3412"), QColor("#b91c1c"), QColor("#065f46"), QColor("#6b6f77")
BROWSE = "Browse for a card or folder…"
USB = "usb"
USB_AUTOLOAD = 400_000        # over USB, samples up to this size load as soon as they are selected


def bottom_folder(rel: str) -> str:
    """The folder a sample is in, alone: 'Paid/X.zip::Eclipse/Atomspheric Drones/A.wav' → 'Atomspheric Drones'."""
    parts = rel.replace("::", "/").split("/")[:-1]
    if not parts:
        return ""
    last = parts[-1]
    return os.path.splitext(last)[0] if last.lower().endswith(".zip") else last


def human_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


class MainWindow(TagsMixin, SyntaktMixin, QMainWindow):
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
        self.libdb = LibraryDB()
        self._lib_scanning = False
        self.on_deluge: dict[str, str] = {}      # audio identity → its path on the open card
        self.origin_by_audio: dict[str, str] = {} # audio identity → Factory/Paid/Free/User of the copy that counts
        self.pack_by_audio: dict[str, str] = {}   # audio identity → its pack in the library
        self._offered_reorganize = False
        self._syn_init()
        self._tags_init()
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
        from PySide6.QtWidgets import QToolButton
        lb = QToolButton()
        lb.setText("Library ▾")
        lb.setPopupMode(QToolButton.InstantPopup)
        lm = QMenu(self)
        sc = lm.addAction("Scan", self._scan_library)
        sc.setShortcut("Ctrl+R")
        lm.addAction("Reorganize library…", self._open_reorganize)
        lm.addAction("Library changed or missing…", self._open_alerts)
        lm.addSeparator()
        lm.addAction("Read tempos from loop files…", self._read_acid_all)
        lm.addAction("Tag words…", self._vocab_dialog)
        lm.addSeparator()
        lm.addAction("Open the library folder", lambda: open_folder(self.settings["library_root"]))
        lb.setMenu(lm)
        tb.addWidget(lb)
        self.addAction(sc)
        self.get_act = QAction("Get from Deluge…", self)
        self.get_act.setToolTip("Keep samples made on the Deluge (recordings, resamples, samples put there by hand) "
                                "in the library's User folder")
        self.get_act.setEnabled(False)
        self.get_act.triggered.connect(self._open_get)
        tb.addAction(self.get_act)
        tb.addSeparator()
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
        from .. import APP_NAME, __version__
        from ..updater import UpdateManager

        def _save(k, v):
            self.settings[k] = v
            self.settings.save()
        from .. import REPO, SETUP_PREFIX
        self.updates = UpdateManager(self, APP_NAME, REPO, SETUP_PREFIX,
                                     __version__, self.settings.get, _save)
        hb = QToolButton(); hb.setText("Help ▾"); hb.setPopupMode(QToolButton.InstantPopup)
        hm = QMenu(self)
        hm.addAction("Check for updates…", lambda: self.updates.check(asked=True))
        hm.addAction("Update settings…", self.updates.settings_dialog)
        hm.addAction(f"About {APP_NAME} {__version__}", lambda: QMessageBox.information(
            self, APP_NAME, f"{APP_NAME} {__version__}\n{REPO} on GitHub"))
        hb.setMenu(hm); tb.addWidget(hb)
        tb.addSeparator()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search — e.g.  kick 808   type:bass loop tag:dark bpm:140-150 key:F#m")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(380)
        self.search.returnPressed.connect(self._do_search)
        self.search.textChanged.connect(lambda t: None if t.strip() else self._search_cleared())
        tb.addWidget(self.search)
        qb = QPushButton("?")
        qb.setFixedWidth(26)
        qb.setToolTip("The search filters")
        qb.clicked.connect(self._search_help)
        tb.addWidget(qb)
        sv = QPushButton("Save as smart list…")
        sv.setToolTip("Keep this search under SMART LISTS")
        sv.clicked.connect(self._save_smart_list)
        tb.addWidget(sv)
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
        self.table = QTableWidget(0, 13)
        self.table.setHorizontalHeaderLabels(["Name", "Pack", "Source", "Deluge", "Length", "kHz/bit", "Used", "Plays",
                                              "Type", "Syntakt", "BPM", "Key", "Tags"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 240)
        hh.setSectionResizeMode(1, QHeaderView.Interactive)
        self.table.setColumnWidth(1, 150)
        for c in range(2, 12):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(12, QHeaderView.Interactive)
        self.table.setColumnWidth(12, 150)
        hh.moveSection(8, 1)                    # Type shows next to Name
        hh.moveSection(9, 5)                    # Syntakt after Deluge
        for k, col in enumerate((10, 11, 12)):  # BPM, Key, Tags after Syntakt (0.13)
            hh.moveSection(hh.visualIndex(col), 6 + k)
        self.table.currentCellChanged.connect(lambda r, *_: self._row_select(r))
        self.table.installEventFilter(self)
        ml.addWidget(self.table, 1)
        self.list_label = QLabel()
        self.list_label.setStyleSheet("color:#4f535b;")
        lr = QHBoxLayout()
        lr.addWidget(self.list_label, 1)
        lr.addWidget(QLabel("Show"))
        self.loop_filter = QComboBox()
        self.loop_filter.addItems(["One-shots and loops", "One-shots only", "Loops only"])
        self.loop_filter.setToolTip("One-shot or loop is guessed from the names; right-click a sample → Set type… to fix it")
        self.loop_filter.currentIndexChanged.connect(self._loop_filter_changed)
        lr.addWidget(self.loop_filter)
        ml.addLayout(lr)
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
        self.f_type = QLabel()
        self.f_type.setWordWrap(True)
        form.addRow("Type", self.f_type)
        self._build_tag_rows(form)
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
        split.setSizes([270, 790, 340])
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
        QTimer.singleShot(200, lambda: self._scan_library(quiet=True))
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
        QTimer.singleShot(1500, self._syn_connect)

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
        if self.card or self.syn:
            self._syn_connect()
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
        self.get_act.setEnabled(True)
        self.factory_rec = factory.load(self.settings["library_root"], self.card)
        if self.factory_rec and self.factory_rec.undone:
            self.factory_rec = None
        self._refresh_devices()
        QTimer.singleShot(0, self._build_library_root)
        self.record_act.setVisible(self.factory_rec is not None)
        self.factory_act.setVisible(self.factory_rec is None)
        QTimer.singleShot(400, self._maybe_offer_initialize)
        self._build_card_tree()

    # ------------------------------------------------------------------ tree
    def _build_card_tree(self):
        """0.11: the card has no folder tree any more; TARGETS › DELUGE (by type) is built with the library."""
        return

    # ------------------------------------------------------------------ the library (spec 2)
    def _refresh_devices(self):
        """What is on the open Deluge card, by audio identity (spec 4.4), and where each sample's source copy is."""
        self.on_deluge = {}
        if self.card:
            lib_to_card = {}
            for e in (self.factory_rec.entries if self.factory_rec else []):
                if e.library and e.card.lower() in self.card.files:
                    lib_to_card[e.library.lower()] = e.card
            for link in self.mapping.links:
                if link.library and link.card.lower() in self.card.files:
                    lib_to_card[link.library.lower()] = link.card
            for rel, a in self.libdb.audio_of(lib_to_card).items():
                if a:
                    self.on_deluge[a] = lib_to_card[rel]
        self.card_audio = {c.lower(): a for a, c in self.on_deluge.items()}
        self.card_types = {}                     # card sample -> its type list (TARGETS › DELUGE)
        self.id_labels = self.libdb.id_labels()
        if self.card:
            folders = self.libdb.listed_folders()
            for rel in self.card.sample_files():
                if rel.upper().startswith("SAMPLES/DSM_TRASH/"):
                    continue
                a = self.card_audio.get(rel.lower())
                self.card_types[rel] = folders.get(a) or layout.type_folder(stypes.guess(rel)[0])
        w = self.libdb.wanted()
        self.origin_by_audio = {a: r["source"] for a, r in w.items()}
        self.pack_by_audio = {a: " › ".join(x for x in (r["level1"], r["level2"]) if x) for a, r in w.items()}

    def _type_branch(self, parent, counts: dict, kind: str, on: dict | None = None):
        """Drums › Kick … then Bass … Unknown under parent, with counts (and how many are on the Deluge)."""
        def item(label, n, val):
            extra = f" · {on[val]:,} on Deluge" if on and on.get(val) else ""
            it = QTreeWidgetItem([f"{label}  ({n:,}{extra})"])
            it.setData(0, Qt.UserRole, (kind, val))
            if on and on.get(val):
                it.setForeground(0, GREEN)
            return it
        n_drums = sum(counts.get(t, 0) for t in layout.DRUM_FOLDERS)
        if n_drums:
            if on:
                on = dict(on, Drums=sum(on.get(t, 0) for t in layout.DRUM_FOLDERS))
            d = item("Drums", n_drums, "Drums")
            parent.addChild(d)
            for t in layout.DRUM_FOLDERS:
                if counts.get(t):
                    d.addChild(item(stypes.label(t), counts[t], t))
            d.setExpanded(True)
        for t in layout.TYPE_FOLDERS:
            if t not in layout.DRUM_FOLDERS and counts.get(t):
                parent.addChild(item(t, counts[t], t))

    def _library_on_card(self) -> set[str]:
        return set(self.on_deluge)

    def _bold(self, it, colour=None):
        f = it.font(0)
        f.setBold(True)
        it.setFont(0, f)
        if colour is not None:
            it.setForeground(0, colour)
        return it

    def _build_library_root(self):
        """LIBRARY by type, SOURCES 🔒 with their packs, TARGETS › DELUGE by type, SMART LISTS (0.11)."""
        self.tree.blockSignals(True)
        sel = self.tree.currentItem().data(0, Qt.UserRole) if self.tree.currentItem() else None
        for i in reversed(range(self.tree.topLevelItemCount())):
            if self.tree.topLevelItem(i).data(0, Qt.UserRole)[0] in ("samplesroot", "sourcesroot", "targetsroot",
                                                                      "libsmarthdr", "cardroot"):
                self.tree.takeTopLevelItem(i)
        root_path = self.settings["library_root"]
        ok = bool(root_path and os.path.isdir(root_path))
        counts = self.libdb.counts()
        loop = self._loop_wanted()
        tcounts = self.libdb.type_counts(loop=loop)
        on = {}
        if self.card:
            for a, f in self.libdb.listed_folders().items():
                if a in self.on_deluge:
                    on[f] = on.get(f, 0) + 1
        n_samples = sum(tcounts.values())
        top = QTreeWidgetItem([f"LIBRARY  ({n_samples:,})" if ok else "LIBRARY  (library folder not found — Settings)"])
        top.setData(0, Qt.UserRole, ("samplesroot", None))
        top.setToolTip(0, "Every sample in the library once, by type — a view, nothing is copied.\n"
                          "Right-click a sample → Set type… to move it to another list.")
        self.tree.insertTopLevelItem(0, self._bold(top))
        self._type_branch(top, tcounts, "samples")
        top.setExpanded(True)
        total = sum(v for k, v in counts.items() if len(k) == 2)
        srcs = QTreeWidgetItem([f"SOURCES 🔒  ({total:,})"])
        srcs.setData(0, Qt.UserRole, ("sourcesroot", None))
        srcs.setToolTip(0, (root_path or "") + "\nYour samples as they came — never changed by the app. Packs in zips "
                        "are read where they are.")
        self.tree.insertTopLevelItem(1, self._bold(srcs))
        alerts = len(self.libdb.source_alerts())
        if alerts:
            a = QTreeWidgetItem([f"⚠ {alerts} changed or missing…"])
            a.setForeground(0, AMBER)
            a.setData(0, Qt.UserRole, ("alerts", None))
            srcs.addChild(a)
        A = lsrc.AREA
        for st in layout.SOURCE_TYPES:
            n = counts.get((A, st), 0)
            si = QTreeWidgetItem([f"{st}  ({n:,})"])
            si.setData(0, Qt.UserRole, ("srcq", {"area": A, "source": st}))
            srcs.addChild(si)
            for a in sorted({k[2] for k in counts if len(k) >= 3 and k[0] == A and k[1] == st}, key=str.lower):
                ai = QTreeWidgetItem([f"{a}  ({counts.get((A, st, a), 0):,})"])
                ai.setData(0, Qt.UserRole, ("srcq", {"area": A, "source": st, "level1": a}))
                si.addChild(ai)
                for b in sorted({k[3] for k in counts if len(k) == 4 and k[:3] == (A, st, a)}, key=str.lower):
                    bi = QTreeWidgetItem([f"{b}  ({counts.get((A, st, a, b), 0):,})"])
                    bi.setData(0, Qt.UserRole, ("srcq", {"area": A, "source": st, "level1": a, "level2": b}))
                    ai.addChild(bi)
        tg = QTreeWidgetItem(["TARGETS"])
        tg.setData(0, Qt.UserRole, ("targetsroot", None))
        tg.setToolTip(0, "The devices the samples go to, with what is on each one, by type")
        self.tree.insertTopLevelItem(2, self._bold(tg))
        if self.card:
            ctypes = {}
            for rel, f in self.card_types.items():
                ctypes[f] = ctypes.get(f, 0) + 1
            dv = QTreeWidgetItem([f"DELUGE  ({sum(ctypes.values()):,} on the card · {self.card.facts.label or self.card.root})"])
            dv.setData(0, Qt.UserRole, ("devroot", "deluge"))
            dv.setToolTip(0, "Samples on the Deluge's card, by type. Right-click for Rename samples to their IDs…")
            tg.addChild(self._bold(dv))
            self._type_branch(dv, ctypes, "devtype")
            dv.setExpanded(True)
        else:
            dv = QTreeWidgetItem(["DELUGE  (no card open)"])
            dv.setData(0, Qt.UserRole, ("devroot", None))
            dv.setForeground(0, GREY)
            tg.addChild(dv)
        self._syn_tree(tg)
        tg.setExpanded(True)
        hdr = QTreeWidgetItem(["SMART LISTS"])
        hdr.setData(0, Qt.UserRole, ("libsmarthdr", None))
        self.tree.addTopLevelItem(self._bold(hdr))
        n_on = sum(on.values())
        dups = self.libdb.duplicate_audios()
        for name, n in (("On the Deluge", n_on if self.card else 0),
                        ("Not on the Deluge", max(0, n_samples - n_on)),
                        ("Won't play on the Deluge", len(self.libdb.query(listed=True, wont_play=True))),
                        ("Duplicates in the library", len(dups))):
            it = QTreeWidgetItem([f"★ {name}  ({n:,})"])
            it.setData(0, Qt.UserRole, ("libsmart", name))
            hdr.addChild(it)
        if self.card:                            # lists of the card's own files (0.11.1: moved here from DELUGE)
            for name, tip in (("Unused on the card", "Card samples no kit, synth or song uses"),
                              ("Won't play on the card", "Card files the Deluge can't play (from the health check)"),
                              ("Duplicates on the card", "The same sound in more than one card file — tidy them")):
                it = QTreeWidgetItem([f"★ {name}"])
                it.setData(0, Qt.UserRole, ("smart", name))
                it.setToolTip(0, tip)
                hdr.addChild(it)
        self._saved_tree(hdr)
        hdr.setExpanded(True)
        self.tree.blockSignals(False)
        if sel and sel[0] in ("samples", "srcq", "libsmart", "devtype", "devroot", "syntype", "synroot", "saved"):
            it = self._find_tree_item(sel)
            if it:
                self.tree.setCurrentItem(it)

    def _set_type(self, lib_rows):
        first = self._row_types(lib_rows[0])
        dlg = TypeDialog(self, len(lib_rows), first["types"], first["is_loop"])
        if not dlg.exec() or not dlg.result_value:
            return
        audios = sorted({r["audio"] for r in lib_rows if r.get("audio")})
        if dlg.result_value[0] == "set":
            _, types, loop = dlg.result_value
            self.libdb.set_user_type(audios, types, loop)
        else:
            self.libdb.clear_user_type(audios)
        self._save_types()
        self._rows_gen += 1
        self._data_gen += 1
        self.status_lbl.setText(f"Type updated for {len(audios):,} sample{'s' if len(audios) != 1 else ''}")
        self._loop_filter_changed()

    def _save_types(self):
        """Your types, by audio identity, in _Sample Manager\\types.json so they outlive the PC."""
        import json
        root = self.settings["library_root"]
        d = layout.app_dir(root) if root else None
        if not d or not os.path.isdir(d):
            return
        try:
            with open(os.path.join(d, "types.json"), "w", encoding="utf-8") as f:
                json.dump(self.libdb.user_types(), f, indent=0)
        except OSError:
            pass

    def _load_types(self):
        import json
        root = self.settings["library_root"]
        p = os.path.join(layout.app_dir(root), "types.json") if root else ""
        try:
            with open(p, encoding="utf-8") as f:
                self.libdb.load_user_types(json.load(f))
        except (OSError, ValueError):
            pass

    def _scan_library(self, quiet=False, then=None):
        if self._lib_scanning:
            return
        root = self.settings["library_root"]
        if not root or not os.path.isdir(root):
            self._build_library_root()
            if not quiet:
                QMessageBox.information(self, APP_NAME, f"The library folder isn't there:\n{root}\n\nSet it in Settings.")
            return
        if not self._offered_reorganize and reorganize.needed(root):
            self._offered_reorganize = True          # an older layout: move it first, then scan (it scans after)
            self._build_library_root()
            QTimer.singleShot(200, self._open_reorganize)
            return
        self._lib_scanning = True
        db = self.libdb
        self._load_types()
        self._tags_load()

        def work(progress):
            return scan_library(db, root, progress=progress)

        def done(r):
            self._lib_scanning = False
            self.prog.hide()
            self._rows_gen += 1
            self._tags_reload()
            QTimer.singleShot(500, self._warm_search)
            self._refresh_devices()
            self._build_library_root()
            bits = [f"Library: {r.get('seen', 0):,} files", f"{r.get('read', 0):,} samples read",
                    f"{r.get('listed', 0):,} different samples"]
            if r.get("changed") or r.get("missing"):
                bits.append(f"⚠ {r.get('changed', 0)} changed, {r.get('missing', 0)} missing — "
                            "Library ▾ › Library changed or missing…")
            self.status_lbl.setText(" · ".join(bits))
            if then:
                then()

        def failed(msg):
            self._lib_scanning = False
            self._job_failed(msg)
        run_job(Job(work, wants_progress=True), done=done, failed=failed, progress=self._progress)

    def _list_library(self, rows_db, title, show_path=True):
        self._fill_rows(self._lib_rows(rows_db), title)

    def _lib_rows(self, rows_db) -> list[dict]:
        """Rows for the list from library db rows."""
        root = self.settings["library_root"]
        rows = []
        for r in rows_db:
            label = f"{r['sid']} {r['sid_name']}" if r["sid"] else r["name"]
            rows.append({"where": "lib", "abs": os.path.join(root, *r["rel"].split("/")), "librel": r["rel"],
                         "name": label, "file_name": r["name"], "pack": bottom_folder(r["rel"]), "info": info_from_row(r, root), "audio": r["audio"],
                         "area": r["area"], "in_zip": lsrc.SEP in r["rel"],
                         "types": stypes.unpack(r["types"]) or [stypes.UNKNOWN], "is_loop": bool(r["is_loop"]),
                         "type_why": r["type_why"] or "folder in Samples", "type_by_user": bool(r["type_by_user"]),
                         "sid": r["sid"]})
        return rows

    def _tree_expand(self, item):
        return

    def _tree_select(self, item, _prev):
        if not item or not item.data(0, Qt.UserRole):
            return
        kind, val = item.data(0, Qt.UserRole)
        if kind == "card":
            self._list_card([f for f in self._card_files_in(val)], f"{val}")
        elif kind == "samples":
            folders = layout.DRUM_FOLDERS if val == "Drums" else [val]
            rows = [r for f in folders for r in self.libdb.query(folder=f)]
            self._list_library(rows, f"Library › {stypes.label(val)}")
        elif kind == "samplesroot":
            self._list_library(self.libdb.query(listed=True), "Library")
        elif kind == "devroot" and val and self.card:
            self._list_card(sorted(self.card_types, key=str.lower), "Deluge")
        elif kind == "devtype" and self.card:
            folders = set(layout.DRUM_FOLDERS) if val == "Drums" else {val}
            self._list_card(sorted((r for r, f in self.card_types.items() if f in folders), key=str.lower),
                            f"Deluge › {stypes.label(val)}")
        elif kind == "synroot" and val:
            self._syn_list(None, "Syntakt")
        elif kind == "syntype":
            folders = set(layout.DRUM_FOLDERS) if val == "Drums" else {val}
            self._syn_list(folders, f"Syntakt › {stypes.label(val)}")
        elif kind == "srcq":
            q = dict(val)
            title = " › ".join(["Library"] + [q[k] for k in ("source", "level1", "level2") if q.get(k)])
            self._list_library(self.libdb.query(**q), title)
        elif kind == "alerts":
            self._open_alerts()
        elif kind == "saved":
            self._saved_select(val)
        elif kind == "libsmart":
            on = set(self.on_deluge)
            if val == "On the Deluge":
                self._list_library(self.libdb.query(listed=True, audios=on), "On the Deluge")
            elif val == "Not on the Deluge":
                self._list_library([r for r in self.libdb.query(listed=True) if r["audio"] not in on],
                                   "Not on the Deluge")
            elif val == "Duplicates in the library":
                rows = self.libdb.duplicate_rows()
                n = len({r["audio"] for r in rows})
                self._list_library(rows, f"Duplicates in the library · {n:,} sounds in more than one place")
            else:
                self._list_library(self.libdb.query(listed=True, wont_play=True), "Won't play on the Deluge")
        elif kind == "smart" and self.index:
            if val.startswith("Duplicates"):
                self._find_card_duplicates()
            elif val.startswith("Unused"):
                self._list_card(unused_samples(self.index), "Unused on card", show_dir=True)
            else:
                if not self.health:
                    self._run_health(then_list=True)
                else:
                    self._list_wont_play()

    # ------------------------------------------------------------------ Reorganize, alerts, Get (spec 2, 4.5)
    def _lib_ok(self) -> str | None:
        root = self.settings["library_root"]
        if not root or not os.path.isdir(root):
            QMessageBox.information(self, APP_NAME, f"The library folder isn't there:\n{root}\n\nSet it in Settings.")
            return None
        return root

    def _open_reorganize(self):
        root = self._lib_ok()
        if not root:
            return
        rec = factory.load(root, self.card)
        card = self.card if (self.card and rec) else None
        mapping, settings, db = self.mapping, self.settings, self.libdb
        known = db.source_audios()

        def look(progress):
            progress(0, 0, "Looking at the library…")
            return reorganize.plan(root, mapping, rec, card, known)

        def planned(steps):
            self.prog.hide()
            if not ReorganizeDialog(self, steps).exec():
                return
            todo = [s for s in steps if s.kind != "factory-wait"]
            run_job(Job(lambda progress: reorganize.apply(root, todo, mapping, rec, card, settings, db,
                                                          progress=progress), wants_progress=True),
                    done=done, failed=self._job_failed, progress=self._progress)

        def done(out):
            self.prog.hide()
            self.mapping = Mapping(root)
            self.factory_rec = factory.load(root, self.card) or self.factory_rec
            lines = [f"{len(out['done'])} step(s) done."]
            if out["cleanup"]:
                lines.append(f"{len(out['cleanup'])} old folder(s) are on the Studio Cleanup list; run Clean up "
                             "Studio.cmd to move them to Archive.")
            lines.append("The app now scans the library.")
            box = QMessageBox(QMessageBox.Warning if out["problems"] else QMessageBox.Information, "Reorganize library",
                              "\n".join(lines))
            if out["problems"]:
                box.setDetailedText("\n".join(out["problems"]))
            box.exec()
            self._scan_library(quiet=True)
        run_job(Job(look, wants_progress=True), done=planned, failed=self._job_failed, progress=self._progress)

    def _factory_missing(self) -> int:
        root = self.settings["library_root"]
        rec = self.factory_rec or (factory.load(root) if root else None)
        if not rec or not root or not os.path.isdir(root):
            return 0
        return len(factory.missing_from_library(rec, root))

    def _open_alerts(self):
        root = self._lib_ok()
        if not root:
            return
        dlg = AlertsDialog(self, self.libdb.source_alerts(), self._factory_missing(), bool(self.card and self.factory_rec))
        if not dlg.exec():
            return
        if dlg.get_factory:
            rec = self.factory_rec
            self._copy_factory(rec)
            return
        for what, path in dlg.chosen:
            if what == "change":
                self.libdb.accept_change(path)
            else:
                self.libdb.accept_missing(path)
        self._scan_library(quiet=True)

    def _open_get(self):
        root = self._lib_ok()
        if not root or not self.card:
            return
        card, db, mapping = self.card, self.libdb, self.mapping

        def work(progress):
            return getdevice.find_new(card, db, mapping, progress=progress)

        def found(items):
            self.prog.hide()
            dlg = GetDialog(self, items, getdevice.folder_name())
            if not dlg.exec() or not any(f.keep for f in items):
                return
            run_job(Job(lambda progress: getdevice.keep(card, root, items, progress=progress), wants_progress=True),
                    done=kept, failed=self._job_failed, progress=self._progress)

        def kept(out):
            self.prog.hide()
            text = f"{out['kept']} sample(s) kept in User › {getdevice.folder_name()}."
            if out["problems"]:
                box = QMessageBox(QMessageBox.Warning, "Get from the Deluge", text)
                box.setDetailedText("\n".join(out["problems"]))
                box.exec()
            self.status_lbl.setText(text)
            self._scan_library(quiet=True)
        run_job(Job(work, wants_progress=True), done=found, failed=self._job_failed, progress=self._progress)

    def _card_files_in(self, rel_dir: str):
        pre = rel_dir.lower() + "/"
        for k, v in self.card.files.items():
            if k.startswith(pre) and "/" not in k[len(pre):] and os.path.splitext(k)[1] in AUDIO_EXTS:
                yield v

    def select_card_path(self, rel: str):
        """Jump the tree to a card folder (or the folder of a file) and select the file."""
        target = rel if rel.lower() in {d.lower() for d in self.card.dirs} else rel.rsplit("/", 1)[0]
        self.tree.setCurrentItem(None)
        self._list_card(self._card_files_in(target), target)
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
        labels = getattr(self, "id_labels", {})
        audio = getattr(self, "card_audio", {})
        rows = [{"where": "card", "rel": r, "abs": self.card.abs(r), "file_name": os.path.basename(r),
                 "name": labels.get(audio.get(r.lower())) or os.path.basename(r), "pack": bottom_folder(r),
                 "audio": audio.get(r.lower())}
                for r in rels]
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

    def _row_types(self, row):
        """Give a row its types (guessed on the spot for card files and library folders)."""
        if "types" not in row:
            rel = row.get("rel") or self._lib_rel(row["abs"]) or row["name"]
            info = row.get("info")
            ty, lp, why = stypes.guess(rel, info.seconds if info and info.rate else None)
            row.update(types=ty, is_loop=lp, type_why=why, type_by_user=False)
        return row

    def _loop_filter_changed(self, _=None):
        cur = self.tree.currentItem()
        kind = (cur.data(0, Qt.UserRole) or (None,))[0] if cur else None
        q = self.search.text().strip()
        self._build_library_root()              # reselecting a library item lists it again
        if kind in ("samples", "samplesroot", "srcq", "libsmart", "devtype", "devroot", "syntype", "synroot", "saved"):
            if q:                                # …and the search on it again
                self.search.blockSignals(True)
                self.search.setText(q)
                self.search.blockSignals(False)
                self._do_search()
            return
        if kind is None and self.search.text().strip():
            self._do_search()
        elif cur:
            self._tree_select(cur, None)

    def _loop_wanted(self):
        return {0: None, 1: False, 2: True}[self.loop_filter.currentIndex()]

    def _fill_rows(self, rows, title):
        if not self._in_search:                  # the list the search box filters (spec 8.4)
            self.search_base = (rows, title)
            if self.search.text().strip():
                self.search.blockSignals(True)
                self.search.clear()
                self.search.setStyleSheet("")
                self.search.blockSignals(False)
        for r in rows:
            self._row_types(r)
        want = self._loop_wanted()
        if want is not None:
            rows = [r for r in rows if r["is_loop"] == want]
            title += " · " + ("loops only" if want else "one-shots only")
        self.list_gen += 1
        gen = self.list_gen
        self.current = None
        self.table.setCurrentCell(-1, -1)
        self.rows = rows
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(0)               # drop the old items first: replacing items one by one is very slow
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
            pk = QTableWidgetItem(row.get("pack", ""))
            pk.setForeground(GREY)
            pk.setToolTip(row.get("librel") or row.get("rel") or "")
            self.table.setItem(i, 1, pk)
            org = self._origin(row)
            row["origin"] = org
            o = QTableWidgetItem(("🔒 " if org == "Factory" else "") + org)
            if org == "Factory":
                o.setForeground(QColor("#1d4ed8"))
                o.setToolTip("Factory sample — locked: it can't be renamed, moved, trashed or overwritten")
            self.table.setItem(i, 2, o)
            oc = QTableWidgetItem("✓" if on_card else "")
            oc.setForeground(GREEN)
            oc.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 3, oc)
            used = len(self.index.uses(row["rel"])) if row["where"] == "card" and self.index else None
            u = QTableWidgetItem()
            if used is not None:
                u.setData(Qt.DisplayRole, used)
            self.table.setItem(i, 6, u)
            for c in (4, 5, 7):
                self.table.setItem(i, c, QTableWidgetItem("…"))
            ty = QTableWidgetItem(", ".join(stypes.label(t) for t in row["types"]) + (" · loop" if row["is_loop"] else ""))
            ty.setToolTip(("Set by you" if row["type_by_user"] else f"Guessed from the {row['type_why']}")
                          + " · right-click → Set type… to change it")
            if not row["type_by_user"]:
                ty.setForeground(GREY)
            self.table.setItem(i, 8, ty)
            sy = getattr(self, "on_syntakt", {}).get(row.get("audio")) if row["where"] != "syntakt" else row["slot"]
            si = QTableWidgetItem()
            if sy:
                si.setData(Qt.DisplayRole, sy)
                si.setToolTip(f"On the Syntakt, slot {sy}")
            si.setForeground(GREEN)
            si.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 9, si)
            self._fill_tag_cells(i, row)
        self.table.setSortingEnabled(True)
        self.table.setUpdatesEnabled(True)
        self.list_label.setText(f"{title} · {len(rows)} files · reading headers…" if rows else f"{title} · no audio files")
        if not rows:
            return

        card = self.card
        if all(r.get("info") for r in rows):            # library rows come with their facts: no file reads
            self._rows_inspected(gen, title, [r["info"] for r in rows])
            return

        def work(progress):
            out = []
            for k, r in enumerate(rows):
                out.append(r["info"] if r.get("info") else
                           card.inspect(r["rel"]) if r["where"] == "card" else inspect(r["abs"]))
                if k % 20 == 0:
                    progress(k, len(rows), "")
            return out
        run_job(Job(work, wants_progress=True), done=lambda infos: self._rows_inspected(gen, title, infos),
                failed=self._job_failed)

    def _rows_inspected(self, gen, title, infos):
        if gen != self.list_gen:
            return
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
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
            self.table.setItem(r, 4, ln)
            fmt = QTableWidgetItem(info.short_format())
            if info.rate and info.rate != 44100:
                fmt.setForeground(AMBER)
            self.table.setItem(r, 5, fmt)
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
            self.table.setItem(r, 7, s)
        self.table.setSortingEnabled(True)
        self.table.setUpdatesEnabled(True)
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
        rel = row.get("librel", "")
        if row["where"] == "syntakt" and not rel:
            return "Syntakt"
        top = rel.split("/")[0].title() if "/" in rel else ""
        return top if top in layout.SOURCE_TYPES else "Other"

    def _on_card(self, row):
        """True for a card file; for a library sample, its path on the open card (spec 4.4: by audio), or False."""
        if row["where"] == "card":
            return True
        if not self.card:
            return False
        return self.on_deluge.get(row.get("audio"), False)

    def _row_at(self, r):
        it = self.table.item(r, 0)
        if it is None:
            return None
        k = it.data(Qt.UserRole)
        return self.rows[k] if isinstance(k, int) and 0 <= k < len(self.rows) else None

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
        self.wave.set_data(None, message="Fetching from the Syntakt…" if row["where"] == "syntakt" else
                           "Loading…" if row["where"] == "lib" or not self.card.is_usb
                           else "Fetching from the Deluge over USB…")
        syn, slot = self.syn, (self.syn.slot(row["slot"]) if row["where"] == "syntakt" and self.syn else None)
        path, card = row["abs"], self.card
        rel = row.get("rel")

        root = self.settings["library_root"]
        librel = row.get("librel") if row.get("in_zip") else None

        def work():
            if row["where"] == "syntakt":   # read from the Syntakt once, then from the PC cache
                if slot is None:
                    raise RuntimeError("the Syntakt isn't connected")
                src = syn.wav(slot)
            elif librel:                    # a sample inside a zip: unpacked once to the audition cache (spec 2.2)
                src = lsrc.materialize(root, librel, os.path.join(app_dir(), "cache"))
            else:
                src = card.read_for_playback(rel) if row["where"] == "card" and card.is_usb else path
            data, rate = playback.load(src)
            acid = self._acid_for_current(row, src)
            return path, data, rate, playback.peaks(data), then_play, acid
        def failed(e):
            if self.current and self.current["abs"] == path:
                self.wave.set_data(None, message=f"Can't read this file: {e}")

        def still_loading():
            if self.current and self.current["abs"] == path and self.audio is None and self.wave.peaks is None:
                self.wave.set_data(None, message="Still loading… if this file is in Google Drive it may be downloading. "
                                                 "Pick another sample at any time.")
        QTimer.singleShot(6000, still_loading)
        run_job(Job(work), done=self._audio_loaded, failed=failed)

    def _audio_loaded(self, res):
        path, data, rate, pk, then_play, acid = res
        if acid:                                 # the file's acid chunk gave a tempo or root note (spec 8.3)
            self._tags_reload()
            self._refresh_tag_cells()
        if not self.current or self.current["abs"] != path:
            return
        if acid:
            self._show_tags(self.current)
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
        self._row_types(row)
        self._show_tags(row)
        self.f_type.setText(", ".join(stypes.label(t) for t in row["types"]) + (" · loop" if row["is_loop"] else " · one-shot")
                            + ("  (set by you)" if row["type_by_user"] else f"  (guessed from the {row['type_why']})"))
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
        elif isinstance(row.get("on_card"), str) and self.index:
            refs = self.index.uses(row["on_card"])
            self.used_lbl.setText(f"USED BY ({len(refs)}) — its copy on the card")
            for ref in refs:
                it = QListWidgetItem(ref.xml + (f" · row {ref.row}" if ref.row else ""))
                it.setData(Qt.UserRole, ref)
                self.used.addItem(it)
            if not refs:
                self.used.addItem("On the card, but nothing there uses it yet.")
        elif row["where"] == "syntakt":
            self.used_lbl.setText("ON THE SYNTAKT")
            self.used.addItem(f"Slot {row['slot']} · {row['file_name']}")
            self.used.addItem(f"Matched to the library: {row['how']}" if row.get("how") else
                              "Not in the library — right-click SYNTAKT › Get from Syntakt… to keep it")
            self.used.addItem("Samples are global on the Syntakt; the app can't see which projects use one.")
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

    def _find_card_duplicates(self):
        """0.10: read the card for files with the same audio, then show the Duplicates dialog."""
        if not self.index:
            return
        idx, card = self.index, self.card
        self.status_lbl.setText("Looking for duplicates on the card…" + (" (over USB this can take a while)"
                                                                         if card.is_usb else ""))

        def work(progress):
            from dsm.edit import duplicates as dup
            return dup.card_groups(card, idx, progress=progress)

        def done(groups):
            self.prog.hide()
            if self.card is not card:
                return
            if not groups:
                self.status_lbl.setText("No duplicates on the card.")
                QMessageBox.information(self, APP_NAME, "No sample is on the card more than once.")
                return
            self.status_lbl.setText(f"{len(groups)} sounds are on the card more than once.")
            dlg = DuplicatesDialog(groups, card.facts.label or card.root, self)
            dlg.show_path.connect(self.select_card_path)
            dlg.tidy.connect(self._add_edit)
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
        old_root = self.settings["library_root"]
        path = QLineEdit(self.settings["library_root"])
        pick = QPushButton("Browse…")
        pick.clicked.connect(lambda: path.setText(QFileDialog.getExistingDirectory(dlg, "Library folder", path.text()) or path.text()))
        row = QHBoxLayout()
        row.addWidget(path, 1)
        row.addWidget(pick)
        lay.addRow("Sample library folder", row)
        lay.addRow("", QLabel("Studio › Samples: Factory, Paid, Free and User as they came, and Backups. "
                              "The devices get converted copies."))
        bpath = QLineEdit(self._backup_root())
        bpick = QPushButton("Browse…")
        bpick.clicked.connect(lambda: bpath.setText(QFileDialog.getExistingDirectory(dlg, "Backup folder", bpath.text()) or bpath.text()))
        brow = QHBoxLayout()
        brow.addWidget(bpath, 1)
        brow.addWidget(bpick)
        lay.addRow("Backup folder", brow)
        tw = QPushButton("Tag words…")
        tw.setToolTip("The words that give guessed tags from pack and folder names")
        tw.clicked.connect(self._vocab_dialog)
        lay.addRow("Tags", tw)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addRow(bb)
        dlg.resize(620, 140)
        if dlg.exec():
            self.settings["library_root"] = path.text().strip()
            self.settings["backup_root"] = bpath.text().strip()
            self.settings.save()
            if self.settings["library_root"] != old_root:
                self.libdb.clear()
            self.mapping = Mapping(self.settings["library_root"])
            self._scan_library(quiet=True)

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
        a = row.get("audio")
        bits = []
        copies = self.libdb.copies(a) if a else []
        rels = [c["rel"] for c in copies]
        if rels:
            here = row.get("librel")
            others = [x for x in rels if x != here]
            bits.append("In the library: " + (here or rels[0]).replace(lsrc.SEP, " › "))
            if others:
                bits.append(f"Also in: " + "; ".join(x.replace(lsrc.SEP, " › ") for x in others[:3])
                            + (f" (+{len(others) - 3} more)" if len(others) > 3 else ""))
        on = self.on_deluge.get(a)
        bits.append(f"On the Deluge: {on}" if on else ("Deluge: not there" if self.card else "Deluge: no card open"))
        if self.syn:
            sl = self.on_syntakt.get(a) if a else (row.get("slot") if row["where"] == "syntakt" else None)
            bits.append(f"On the Syntakt: slot {sl}" if sl else "Syntakt: not there")
        return "\n".join(bits)

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
        all_lib = [r for r in lib_rows if r.get("audio")]
        if all_lib:
            acts[m.addAction(f"Set type… ({len(all_lib):,})" if len(all_lib) > 1 else "Set type…")] = "type"
        tag_rows = [r for r in rows if r and r.get("audio") and r["where"] in ("lib", "syntakt", "card")]
        if tag_rows:
            acts[m.addAction(f"Tags… ({len(tag_rows):,})" if len(tag_rows) > 1 else "Tags…")] = "tags"
        if all_lib or tag_rows:
            m.addSeparator()
        lib_fact = [r for r in lib_rows if r.get("origin") == "Factory"]
        lib_rows = [r for r in lib_rows if r.get("origin") != "Factory"]
        if lib_fact:
            a = m.addAction(f"🔒 {len(lib_fact)} factory sample{'s' if len(lib_fact) != 1 else ''} — locked")
            a.setEnabled(False)
        send = [r for r in lib_rows if not r.get("on_card")]
        on = [r for r in lib_rows if isinstance(r.get("on_card"), str)]
        if send:
            acts[m.addAction(f"Send {len(send)} to the card…")] = "send"
        if on and self.index:
            acts[m.addAction(f"Remove {len(on)} from the card…")] = "remove"
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
        syn_rows = [r for r in rows if r and r["where"] == "syntakt"]
        if self.syn:                             # factory library samples can be sent too (0.12.1)
            to_syn = [r for r in rows if r and r["where"] == "lib" and r.get("audio") not in self.on_syntakt]
            if to_syn:
                acts[m.addAction(f"Send {len(to_syn)} to the Syntakt…")] = "syn-send"
            if syn_rows:
                acts[m.addAction(f"Remove {len(syn_rows)} from the Syntakt…")] = "syn-remove"
        if not acts:
            return
        what = acts.get(m.exec(self.table.viewport().mapToGlobal(pos)))
        if what == "syn-send":
            self._syn_send(to_syn)
        elif what == "syn-remove":
            self._syn_remove(syn_rows)
        if what == "tags":
            self._tags_dialog(tag_rows)
        if what == "type":
            self._set_type(all_lib)
        elif what == "send":
            self._send_lib([r["librel"] for r in send])
        elif what == "remove":
            rels = [r["on_card"] for r in on]
            used = [x for x in rels if self.index.uses(x)]
            free = [x for x in rels if not self.index.uses(x)]
            if used:
                QMessageBox.information(self, APP_NAME, f"{len(used)} of these are used by kits, synths or songs, so they "
                                        "stay on the card:\n\n" + "\n".join(used[:12]))
            if free:
                self._add_edit(ed.EditBatch(f"Remove {len(free)} sample{'s' if len(free) != 1 else ''} from the card "
                                            "(to the card trash)", trash=free))
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
        if d and d[0] in ("devroot", "devtype") and self.card and self.index:
            m = QMenu(self)
            a = m.addAction("Rename samples to their IDs…")
            a.setToolTip("e.g. HO0046_FUN_HAT.WAV; every kit, synth and song is updated")
            if m.exec(self.tree.viewport().mapToGlobal(pos)) is a:
                folders = None if d[0] == "devroot" else (set(layout.DRUM_FOLDERS) if d[1] == "Drums" else {d[1]})
                self._rename_to_ids(folders)
            return
        if d and d[0] in ("synroot", "syntype"):
            self._syn_tree_menu(d, pos)
            return
        if d and d[0] in ("srcq", "sourcesroot"):
            m = QMenu(self)
            c = m.addAction("Confirm guessed tags") if d[0] == "srcq" else None
            a = m.addAction("🔒 The library's folders are never changed by the app")
            a.setEnabled(False)
            if m.exec(self.tree.viewport().mapToGlobal(pos)) is c and c is not None:
                self._confirm_pack_guesses(dict(d[1]))
            return
        if d and d[0] == "saved":
            self._saved_menu(d, pos)
            return
        if d and d[0] == "libsmarthdr":
            m = QMenu(self)
            a = m.addAction("New smart list from the search…")
            if m.exec(self.tree.viewport().mapToGlobal(pos)) is a:
                self._save_smart_list()
            return
        if not d or d[0] != "samples":
            return
        rows = self.libdb.query(folder=d[1])
        todo = [r for r in rows if r["audio"] not in self.on_deluge and self.origin_by_audio.get(r["audio"]) != "Factory"]
        m = QMenu(self)
        a = m.addAction(f"Send {len(todo)} not yet on the Deluge to the card…")
        a.setEnabled(bool(todo) and self.card is not None)
        if m.exec(self.tree.viewport().mapToGlobal(pos)) is a:
            self._send_lib([r["rel"] for r in todo])

    def _rename_to_ids(self, folders=None):
        """0.11: rename card samples to their library IDs (HO0046_FUN_HAT.WAV) through the pending tray."""
        from dsm.edit.idnames import rename_batch
        rels = [r for r, f in self.card_types.items() if folders is None or f in folders]
        batch, skipped = rename_batch(self.card, rels, self.card_audio, self.id_labels)
        if not batch:
            QMessageBox.information(self, APP_NAME, "Nothing to rename: " + (skipped or "every sample already has its ID name."))
            return
        msg = (f"Rename {len(batch.moves):,} sample{'s' if len(batch.moves) != 1 else ''} on the card to their IDs, "
               "e.g.\n\n" + "\n".join(f"{o.rsplit('/', 1)[-1]} → {n.rsplit('/', 1)[-1]}" for o, n in batch.moves[:5])
               + ("\n…" if len(batch.moves) > 5 else "") + "\n\nEvery kit, synth and song that uses them is updated. "
               "The change goes to the pending tray; nothing happens until you press Apply." + (f"\n\n{skipped}" if skipped else ""))
        if QMessageBox.question(self, APP_NAME, msg) == QMessageBox.Yes:
            self._add_edit(batch)

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

    def _backup_root(self) -> str:
        """Backups › Deluge in the library once it exists (spec 2.4), else the PC's app folder."""
        b = self.settings.get("backup_root")
        if b:
            return b
        root = self.settings["library_root"]
        if root and os.path.isdir(os.path.join(root, layout.BACKUPS)):
            return layout.backups_dir(root, "Deluge")
        return os.path.join(app_dir(), "Backups")

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
                                f"card.\n\nCopy the factory samples themselves ({size}) into Factory › "
                                "Deluge now, untouched, with a checksum for each?") == QMessageBox.Yes:
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
            self._scan_library(quiet=True)
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
        backup_root = self._backup_root()
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
            self._scan_library(quiet=True)
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

    def _send_lib(self, rels):
        """Send library samples to the card. A sample inside a zip is unpacked to the PC cache first and sent from
        there; the mapping records its place in the library (spec 4.6)."""
        root = self.settings["library_root"]
        if not self.card:
            QMessageBox.information(self, APP_NAME, "Open the card first, then send.")
            return

        def work(progress):
            files, aliases = [], {}
            for k, rel in enumerate(rels):
                path = lsrc.materialize(root, rel, os.path.join(app_dir(), "cache"))
                if lsrc.SEP in rel:
                    aliases[path] = rel
                files.append((path, None))
                if k % 10 == 0:
                    progress(k, len(rels), "Unpacking from zips…" if aliases else "")
            return files, aliases
        run_job(Job(work, wants_progress=True), done=lambda r: (self.prog.hide(), self._start_import(*r)),
                failed=self._job_failed, progress=self._progress)

    def _start_import(self, files, aliases=None):
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
            dlg = ImportDialog(files, infos, self.card, self.settings["library_root"], self.mapping, dests, self,
                               aliases=aliases, names=self.libdb.labels_by_rel())
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
        backup_root = self._backup_root()
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
            self._scan_library(quiet=True)
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
        self._syn_close()
        self.settings.save()
        super().closeEvent(e)
