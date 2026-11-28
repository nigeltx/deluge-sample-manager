"""The health check report."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from dsm.index.health import HealthReport

SECTIONS = [
    ("factory", "Factory samples", "Checked against the factory record made at the factory move: samples that are "
                                   "missing from the card or no longer the size the factory made them."),
    ("missing", "Missing samples", "Presets point at these paths, but no file is there. On the Deluge the preset "
                                   "shows FILE and the row is silent. Suggestions are files with the same name: "
                                   "Relink to this match, or Relink to a file…, fixes every preset that uses it."),
    ("convert", "Won't play on the Deluge", "Wrong header or format for the Deluge. Send the library's copy "
                                            "again and the app converts it, or convert it in an audio editor."),
    ("xml", "Damaged preset files", "These XML files are not well-formed: a tag is missing or out of place. The "
                                    "Deluge's own reader is forgiving and often loads them anyway; the app still "
                                    "rewrites their sample paths byte for byte, never touching the rest."),
    ("layout", "Folder layout", "Where the card differs from the type folders (SAMPLES/DRUMS/KICK, SAMPLES/BASS …)."),
    ("unused", "Unused samples", "No kit, synth or song uses these. Nothing is removed automatically."),
    ("name", "Names outside the rules", "Fine for the Deluge, but outside the app's naming rules (spec Table 3.3)."),
    ("temp", "Leftover clip takes", "Unsaved audio-clip recordings in CLIPS/TEMP."),
    ("junk", "Files left by other programs", "Waveform caches and folder files other programs leave beside samples "
                                             "(REAPER .reapeaks, Ableton .asd, Thumbs.db …). The Deluge never reads "
                                             "them. Move them to the card trash adds the move to Pending."),
]


class HealthDialog(QDialog):
    show_path = Signal(str)
    relink = Signal(str, str)          # missing path as written, card file to point at
    factory = Signal()
    junk = Signal(list)                # files to move to the card trash

    def __init__(self, report: HealthReport, card_label: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Health check — {card_label}")
        self.resize(1000, 640)
        self.report = report
        lay = QVBoxLayout(self)
        c = report.counts
        summary = ((f"<b style='color:#b91c1c'>{c['factory']}</b> factory samples missing or changed · "
                    if c.get("factory") else "") + f"<b>{c.get('missing', 0)}</b> missing · <b>{c.get('convert', 0)}</b> won't play · "
                   f"<b>{c.get('unused_files', 0)}</b> unused · <b>{c.get('xml', 0)}</b> damaged presets · "
                   f"<b>{c.get('name', 0)}</b> naming · <b>{c.get('temp', 0)}</b> leftover takes · "
                   f"<b>{len(report.of('junk')[0].suggestions) if report.of('junk') else 0}</b> files left by other "
                   "programs. "
                   "This check only reads the card — nothing was changed.")
        lay.addWidget(QLabel(summary))
        split = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Finding"])
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        split.addWidget(self.tree)
        split.addWidget(self.detail)
        split.setSizes([520, 480])
        lay.addWidget(split, 1)
        for kind, title, help_ in SECTIONS:
            items = report.of(kind)
            n = report.counts.get("unused_files", 0) if kind == "unused" else len(items)
            top = QTreeWidgetItem([f"{title} ({n})"])
            top.setData(0, Qt.UserRole, ("help", help_))
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            self.tree.addTopLevelItem(top)
            for it in items:
                ch = QTreeWidgetItem([it.title])
                ch.setData(0, Qt.UserRole, ("finding", it))
                top.addChild(ch)
            top.setExpanded(kind in ("missing", "convert", "xml", "layout", "junk") and len(items) <= 50)
        self.tree.currentItemChanged.connect(self._select)
        btns = QHBoxLayout()
        self.go = QPushButton("Show in browser")
        self.go.setEnabled(False)
        self.go.clicked.connect(self._go)
        self.fix = QPushButton("Relink to this match")
        self.fix.setToolTip("Point every preset that uses the missing path at the matching file on the card")
        self.fix.hide()
        self.fix.clicked.connect(self._relink)
        self.pick = QPushButton("Relink to a file…")
        self.pick.hide()
        self.pick.clicked.connect(self._relink_pick)
        self.fact = QPushButton("Move the factory library under FACTORY…")
        self.fact.hide()
        self.fact.clicked.connect(lambda: (self.factory.emit(), self.accept()))
        self.trash = QPushButton("Move them to the card trash")
        self.trash.setToolTip("Adds the move to Pending; nothing changes until Apply, and History can undo it")
        self.trash.hide()
        self.trash.clicked.connect(self._junk)
        copy = QPushButton("Copy report")
        copy.clicked.connect(self._copy)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(self.go)
        btns.addWidget(self.fix)
        btns.addWidget(self.pick)
        btns.addWidget(self.fact)
        btns.addWidget(self.trash)
        btns.addStretch()
        btns.addWidget(copy)
        btns.addWidget(close)
        lay.addLayout(btns)

    def _select(self, item, _prev):
        if not item:
            return
        kind, val = item.data(0, Qt.UserRole)
        self.fix.hide()
        self.pick.hide()
        self.fact.hide()
        self.trash.hide()
        if kind == "help":
            self.detail.setPlainText(val)
            self.go.setEnabled(False)
            return
        if val.kind == "missing":
            self.pick.show()
            on_card = [x for x in val.suggestions if x.startswith("On the card: ")]
            if len(on_card) == 1:
                self.fix.show()
        if val.kind == "layout" and "outside FREE" in val.title:
            self.fact.show()
        if val.kind == "junk":
            self.trash.show()
            self.detail.setPlainText(val.title + "\n\n" + val.detail)
            self.go.setEnabled(False)
            return
        text = val.title + "\n\n" + val.detail
        if val.suggestions:
            text += "\n\nPossible matches:\n  " + "\n  ".join(val.suggestions)
        self.detail.setPlainText(text)
        self.go.setEnabled(bool(val.path) and val.kind != "missing")

    def _go(self):
        item = self.tree.currentItem()
        if item:
            kind, val = item.data(0, Qt.UserRole)
            if kind == "finding":
                self.show_path.emit(val.path)

    def _current_finding(self):
        item = self.tree.currentItem()
        if item:
            kind, val = item.data(0, Qt.UserRole)
            if kind == "finding":
                return val
        return None

    def _junk(self):
        f = self._current_finding()
        if f:
            self.junk.emit(list(f.suggestions))
            self.trash.setEnabled(False)
            self.trash.setText("Added to Pending")

    def _relink(self):
        f = self._current_finding()
        if f:
            target = [x for x in f.suggestions if x.startswith("On the card: ")][0][len("On the card: "):]
            self.relink.emit(f.path, target)
            self.fix.setEnabled(False)
            self.fix.setText("Added to Pending")

    def _relink_pick(self):
        f = self._current_finding()
        if not f:
            return
        from PySide6.QtWidgets import QFileDialog
        root = self.parent().card.root
        path, _ = QFileDialog.getOpenFileName(self, f"Which file should {f.path.rsplit('/', 1)[-1]} point at?",
                                              os.path.join(root, "SAMPLES"), "Audio (*.wav *.aif *.aiff)")
        if path:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if rel.startswith(".."):
                return
            self.relink.emit(f.path, rel)

    def _copy(self):
        lines = []
        for kind, title, _ in SECTIONS:
            items = self.report.of(kind)
            lines.append(f"{title} ({len(items)})")
            for it in items:
                lines.append(f"  {it.title}")
                if it.detail and kind != "unused":
                    lines.append(f"      {it.detail}")
                for s in it.suggestions:
                    lines.append(f"      match: {s}")
        QGuiApplication.clipboard().setText("\n".join(lines))
