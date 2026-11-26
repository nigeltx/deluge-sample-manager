"""Library dialogs (spec v2.2 section 2): Reorganize, Library changed or missing, Get from the Deluge.

Each one shows what will happen first and changes nothing until its button is pressed. The work itself is in
dsm/library (reorganize.py, getdevice.py); these only show it and ask.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from dsm.library import sources as src


def _human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


class ReorganizeDialog(QDialog):
    def __init__(self, parent, steps, card_label: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Reorganize library")
        self.resize(720, 420)
        lay = QVBoxLayout(self)
        if not steps:
            text = "The library is already in order. Nothing to do."
        else:
            text = ("This moves the library to one folder: Studio › Samples holds <b>Factory</b>, <b>Paid</b>, "
                    "<b>Free</b>, <b>User</b> and <b>Backups</b> (spec 2.6). Each step is listed below; nothing "
                    "happens until you press <b>Reorganize</b>. No sample is changed or deleted: folders are moved, "
                    "and old folders go on the Studio Cleanup list, so Clean up Studio.cmd moves them to Archive.")
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        lst = QListWidget()
        lst.setWordWrap(True)
        for k, s in enumerate(steps, 1):
            it = QListWidgetItem(f"{k}. {s.text}")
            if s.kind == "factory-wait":
                it.setForeground(Qt.darkYellow)
            lst.addItem(it)
        lay.addWidget(lst, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        if any(s.kind != "factory-wait" for s in steps):
            ok = bb.addButton("Reorganize", QDialogButtonBox.AcceptRole)
            ok.setStyleSheet("background:#2563eb;color:white;font-weight:600;padding:5px 14px;")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class AlertsDialog(QDialog):
    """Library changed or missing (spec 2.2) and factory samples missing from the library (spec 2.6).
    self.chosen: list of ("change" | "missing", path) and "factory" when Get it from the device was pressed."""

    def __init__(self, parent, alerts, factory_missing: int, card_open: bool):
        super().__init__(parent)
        self.setWindowTitle("Library changed or missing")
        self.resize(760, 460)
        self.chosen, self.get_factory = [], False
        lay = QVBoxLayout(self)
        intro = QLabel("The library is never changed by the app, so a file that changed or went missing was changed "
                       "outside it. Until you decide, the app keeps using what it read before. "
                       "<b>Use it as it is now</b> reads the file again; <b>It's gone for good</b> forgets it.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.lst = QListWidget()
        for a in alerts:
            what = "changed" if a["state"] == "changed" else "missing"
            it = QListWidgetItem(f"{what}:  {a['path'].replace(src.SEP, ' › ')}")
            it.setData(Qt.UserRole, (a["state"], a["path"]))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.lst.addItem(it)
        if not alerts:
            self.lst.addItem("No file in the library has changed or gone missing.")
        lay.addWidget(self.lst, 1)
        if factory_missing:
            row = QHBoxLayout()
            fl = QLabel(f"<b>{factory_missing:,} Deluge factory samples</b> are missing from Factory › Deluge. " + ("" if card_open else "Open the Deluge's card (or USB) to get them back."))
            fl.setWordWrap(True)
            row.addWidget(fl, 1)
            gb = QPushButton("Get it from the device")
            gb.setEnabled(card_open)
            gb.clicked.connect(self._factory)
            row.addWidget(gb)
            lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        if alerts:
            use = bb.addButton("Use it as it is now", QDialogButtonBox.ActionRole)
            gone = bb.addButton("It's gone for good", QDialogButtonBox.ActionRole)
            use.clicked.connect(lambda: self._pick("changed"))
            gone.clicked.connect(lambda: self._pick("missing"))
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _ticked(self):
        out = []
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            if it.data(Qt.UserRole) and it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out

    def _pick(self, state):
        t = [p for s, p in self._ticked() if s == state]
        if t:
            self.chosen = [("change" if state == "changed" else "missing", p) for p in t]
            self.accept()

    def _factory(self):
        self.get_factory = True
        self.accept()


class GetDialog(QDialog):
    """Get from the Deluge: samples on the card that are in no source, a tick each."""

    def __init__(self, parent, found, folder: str):
        super().__init__(parent)
        self.setWindowTitle("Get from the Deluge")
        self.resize(720, 460)
        self.found = found
        lay = QVBoxLayout(self)
        if found:
            text = (f"{len(found):,} samples on the card are nowhere in the library — recordings, resamples and samples "
                    f"put there by hand. The ticked ones are copied, as they are, into User › <b>{folder}</b>. "
                    "Factory samples and exact copies of anything already in the library are never kept twice; "
                    "CLIPS/TEMP takes are left out.")
        else:
            text = "Everything on the card is already in the library. Nothing to keep."
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        self.lst = QListWidget()
        for f in found:
            it = QListWidgetItem(f"{f.card_rel}   ·   {f.where}   ·   {_human(f.size)}")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.lst.addItem(it)
        lay.addWidget(self.lst, 1)
        row = QHBoxLayout()
        allc = QCheckBox("All")
        allc.setChecked(True)
        allc.toggled.connect(lambda v: [self.lst.item(i).setCheckState(Qt.Checked if v else Qt.Unchecked)
                                        for i in range(self.lst.count())])
        row.addWidget(allc)
        row.addStretch()
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        if found:
            ok = bb.addButton("Keep ticked in User", QDialogButtonBox.AcceptRole)
            ok.setStyleSheet("background:#2563eb;color:white;font-weight:600;padding:5px 14px;")
        bb.accepted.connect(self._done)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _done(self):
        for i, f in enumerate(self.found):
            f.keep = self.lst.item(i).checkState() == Qt.Checked
        self.accept()


def folder_of(path: str) -> str:
    return os.path.dirname(path)
