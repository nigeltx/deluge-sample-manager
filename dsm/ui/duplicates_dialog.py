"""Duplicates on the card: groups of files with the same audio, which copy to keep, and Tidy the card."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from dsm.edit import duplicates as dup
from dsm.edit.factory import is_factory_card

GREEN, GREY, BLUE = QColor("#065f46"), QColor("#6b6f77"), QColor("#1d4ed8")


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n} GB"


class DuplicatesDialog(QDialog):
    show_path = Signal(str)              # jump the main window to a card file
    tidy = Signal(object)                # an EditBatch for the pending tray

    def __init__(self, groups: list[dup.CardGroup], card_label: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Duplicates on the card — {card_label}")
        self.resize(980, 620)
        self.groups = groups
        lay = QVBoxLayout(self)
        files = sum(len(g.files) for g in groups)
        intro = QLabel(
            f"<b>{len(groups)}</b> sound{'s are' if len(groups) != 1 else ' is'} on the card more than once "
            f"(<b>{files}</b> files). Same audio means the same sound data, whatever the file is called.<br>"
            "For each sound, the copy marked <b>keep</b> stays; <b>Tidy the card</b> points every kit, synth and song "
            "at it and moves the other copies to the card trash (recoverable, and undoable in History). "
            "🔒 Factory copies and the Deluge's own recordings and clips are never moved (marked <b>stays</b>). Right-click a copy to keep that one instead; untick a sound to leave "
            "it as it is. Nothing changes until you press Apply in the pending tray.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Sound / copies", "Used by", "Size"])
        self.tree.setColumnWidth(0, 700)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemDoubleClicked.connect(self._open)
        self.tree.itemChanged.connect(lambda *_: self._update())
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.total = QLabel()
        row.addWidget(self.total, 1)
        self.tidy_btn = QPushButton("Tidy the card")
        self.tidy_btn.setStyleSheet("background:#2563eb;color:white;font-weight:600;padding:6px 14px;")
        self.tidy_btn.clicked.connect(self._tidy)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(self.tidy_btn)
        row.addWidget(close)
        lay.addLayout(row)
        self._fill()

    def _fill(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        for k, g in enumerate(self.groups):
            name = g.keep.rsplit("/", 1)[-1]
            top = QTreeWidgetItem([f"{name}  —  {len(g.files)} copies", "", human_size(g.size)])
            top.setData(0, Qt.UserRole, ("group", k))
            if g.extras:
                top.setFlags(top.flags() | Qt.ItemIsUserCheckable)
                top.setCheckState(0, Qt.Checked)
            else:
                top.setToolTip(0, "The other copies are factory samples or the Deluge's own recordings, so they stay")
                top.setForeground(0, GREY)
            for rel in g.files:
                keep = rel == g.keep
                stays = not keep and dup.locked(rel)
                label = ("keep   " if keep else ("stays  " if stays else "            ")) + \
                    ("🔒 " if is_factory_card(rel) else "") + rel
                n = g.uses.get(rel, 0)
                ch = QTreeWidgetItem([label, f"{n} preset{'s' if n != 1 else ''}" if n else "—", ""])
                ch.setData(0, Qt.UserRole, ("file", k, rel))
                ch.setForeground(0, GREEN if keep else (BLUE if is_factory_card(rel) else GREY))
                if keep:
                    f = ch.font(0)
                    f.setBold(True)
                    ch.setFont(0, f)
                top.addChild(ch)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        self.tree.blockSignals(False)
        self._update()

    def _chosen(self) -> list[dup.CardGroup]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            k = it.data(0, Qt.UserRole)[1]
            if self.groups[k].extras and it.checkState(0) == Qt.Checked:
                out.append(self.groups[k])
        return out

    def _update(self):
        chosen = self._chosen()
        n = sum(len(g.extras) for g in chosen)
        freed = sum(g.size * len(g.extras) for g in chosen)
        self.total.setText(f"Tidy {len(chosen)} sound{'s' if len(chosen) != 1 else ''}: {n} duplicate "
                           f"file{'s' if n != 1 else ''} to the card trash, {human_size(freed)} freed once the "
                           "trash is emptied")
        self.tidy_btn.setEnabled(n > 0)

    def _menu(self, pos):
        it = self.tree.itemAt(pos)
        d = it.data(0, Qt.UserRole) if it else None
        if not d or d[0] != "file":
            return
        _, k, rel = d
        m = QMenu(self)
        keep = m.addAction("Keep this copy")
        keep.setEnabled(rel != self.groups[k].keep)
        show = m.addAction("Show in the main window")
        a = m.exec(self.tree.viewport().mapToGlobal(pos))
        if a is keep:
            g = self.groups[k]
            g.keep = rel
            g.files = [rel] + sorted((f for f in g.files if f != rel), key=str.lower)
            self._fill()
        elif a is show:
            self.show_path.emit(rel)

    def _open(self, it, _col):
        d = it.data(0, Qt.UserRole)
        if d and d[0] == "file":
            self.show_path.emit(d[2])

    def _tidy(self):
        batch = dup.tidy(self._chosen())
        if batch:
            self.tidy.emit(batch)
            self.accept()
