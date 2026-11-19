"""The health check report."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from dsm.index.health import HealthReport

SECTIONS = [
    ("missing", "Missing samples", "Presets point at these paths, but no file is there. On the Deluge the preset "
                                   "shows FILE and the row is silent. Suggestions are files with the same name; "
                                   "relinking arrives in milestone M4."),
    ("convert", "Won't play on the Deluge", "Wrong header or format. Conversion arrives in milestone M3."),
    ("xml", "Damaged preset files", "These XML files are not well-formed. The Deluge may refuse to load them."),
    ("layout", "Folder layout", "Where the card differs from the FREE / FACTORY / PAID layout."),
    ("unused", "Unused samples", "No kit, synth or song uses these. Nothing is removed automatically."),
    ("name", "Names outside the rules", "Fine for the Deluge, but outside the app's naming rules (spec Table 2.3)."),
    ("temp", "Leftover clip takes", "Unsaved audio-clip recordings in CLIPS/TEMP."),
]


class HealthDialog(QDialog):
    show_path = Signal(str)

    def __init__(self, report: HealthReport, card_label: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Health check — {card_label}")
        self.resize(1000, 640)
        self.report = report
        lay = QVBoxLayout(self)
        c = report.counts
        summary = (f"<b>{c.get('missing', 0)}</b> missing · <b>{c.get('convert', 0)}</b> won't play · "
                   f"<b>{c.get('unused_files', 0)}</b> unused · <b>{c.get('xml', 0)}</b> damaged presets · "
                   f"<b>{c.get('name', 0)}</b> naming · <b>{c.get('temp', 0)}</b> leftover takes. "
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
            top.setExpanded(kind in ("missing", "convert", "xml", "layout") and len(items) <= 50)
        self.tree.currentItemChanged.connect(self._select)
        btns = QHBoxLayout()
        self.go = QPushButton("Show in browser")
        self.go.setEnabled(False)
        self.go.clicked.connect(self._go)
        copy = QPushButton("Copy report")
        copy.clicked.connect(self._copy)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(self.go)
        btns.addStretch()
        btns.addWidget(copy)
        btns.addWidget(close)
        lay.addLayout(btns)

    def _select(self, item, _prev):
        if not item:
            return
        kind, val = item.data(0, Qt.UserRole)
        if kind == "help":
            self.detail.setPlainText(val)
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
