"""Library dialogs (spec v2.1 section 2): Rebuild, Reorganize, Sources changed or missing, Get from the Deluge.

Each one shows what will happen first and changes nothing until its button is pressed. The work itself is in
dsm/library (rebuild.py, reorganize.py, getdevice.py); these only show it and ask.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from dsm.library import rebuild as rb
from dsm.library import sources as src


def _human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


class RebuildDialog(QDialog):
    """The Rebuild plan: what Samples will gain, move, lose — grouped, with every file listed underneath."""

    def __init__(self, parent, plan: rb.Plan):
        super().__init__(parent)
        self.setWindowTitle("Rebuild Samples from Sources")
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        if plan.empty:
            msg = (f"Samples already holds every sample in Sources once, in the folder for its type "
                   f"({len(plan.keep):,} files). Nothing to do.")
        else:
            msg = ("Samples will hold every sample in Sources once, in the folder for its type. "
                   f"<b>{plan.summary()}</b>. {len(plan.keep):,} files are already right. "
                   "Nothing changes until you press <b>Rebuild</b>; files taken out go to "
                   "_Sample Manager › Removed, never straight to deletion.")
        lbl = QLabel(msg)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Change", "Why"])
        tree.setColumnWidth(0, 470)
        groups = [("Add", [(d, "from " + w.source_rel.replace(src.SEP, " › ")) for d, w in plan.adds]),
                  ("Move to another type", [(f"{a} → {b.rsplit('/', 2)[-2]}", "type changed") for a, b, _ in plan.moves]),
                  ("Take out", plan.removes),
                  ("Keep in Sources › User (you added these by hand)", [(a, "→ " + b) for a, b in plan.hand])]
        for title, items in groups:
            if not items:
                continue
            g = QTreeWidgetItem([f"{title} ({len(items):,})", ""])
            f = g.font(0)
            f.setBold(True)
            g.setFont(0, f)
            tree.addTopLevelItem(g)
            g.setExpanded(len(items) <= 200)
            for a, b in items[:3000]:
                g.addChild(QTreeWidgetItem([a, b]))
            if len(items) > 3000:
                g.addChild(QTreeWidgetItem([f"… and {len(items) - 3000:,} more", ""]))
        lay.addWidget(tree, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        if not plan.empty:
            ok = bb.addButton("Rebuild", QDialogButtonBox.AcceptRole)
            ok.setStyleSheet("background:#2563eb;color:white;font-weight:600;padding:5px 14px;")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class ReorganizeDialog(QDialog):
    def __init__(self, parent, steps, card_label: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Reorganize library")
        self.resize(720, 420)
        lay = QVBoxLayout(self)
        if not steps:
            text = "The library is already in order: Sources, Samples and Backups are in place. Nothing to do."
        else:
            text = ("This finishes moving the library to <b>Sources</b>, <b>Samples</b> and <b>Backups</b> (spec 2.6). "
                    "Each step is listed below; nothing happens until you press <b>Reorganize</b>. "
                    "Nothing in Sources is changed or removed — the only files added there are factory samples "
                    "copied from the card. Old folders left empty go on the Studio Cleanup list.")
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
    """Sources changed or missing (spec 2.2) and factory samples missing from Sources (spec 2.6).
    self.chosen: list of ("change" | "missing", path) and "factory" when Get it from the device was pressed."""

    def __init__(self, parent, alerts, factory_missing: int, card_open: bool):
        super().__init__(parent)
        self.setWindowTitle("Sources changed or missing")
        self.resize(760, 460)
        self.chosen, self.get_factory = [], False
        lay = QVBoxLayout(self)
        intro = QLabel("Sources are never changed by the app, so a file that changed or went missing was changed "
                       "outside it. Until you decide, the app keeps using what it read before. "
                       "<b>Use it as it is now</b> reads the file again; <b>It's gone for good</b> forgets it "
                       "(its samples leave Samples at the next Rebuild).")
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
            self.lst.addItem("No file in Sources has changed or gone missing.")
        lay.addWidget(self.lst, 1)
        if factory_missing:
            row = QHBoxLayout()
            fl = QLabel(f"<b>{factory_missing:,} Deluge factory samples</b> are missing from Sources › Factory › "
                        "Deluge. " + ("" if card_open else "Open the Deluge's card (or USB) to get them back."))
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
            text = (f"{len(found):,} samples on the card are in no source — recordings, resamples and samples put "
                    f"there by hand. The ticked ones are copied, as they are, into Sources › User › <b>{folder}</b> "
                    "and join Samples at the next Rebuild. Factory samples and exact copies of anything in Sources "
                    "are never kept twice; CLIPS/TEMP takes are left out.")
        else:
            text = "Everything on the card is already in Sources. Nothing to keep."
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
            ok = bb.addButton("Keep ticked in Sources › User", QDialogButtonBox.AcceptRole)
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
