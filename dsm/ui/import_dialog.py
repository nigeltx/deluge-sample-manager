"""Import dialog (spec 3.3): what each file is, what it will become, and the checks.
Add to pending queues the work; nothing is written here."""
from __future__ import annotations

import os
from collections import Counter
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QRadioButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from dsm.importer.convert import PROFILES
from dsm.importer.naming import SOURCES, guess_vendor_pack
from dsm.importer.plan import ImportBatch, build_plan, bytes_needed

GREY, AMBER, GREEN = QColor("#6b6f77"), QColor("#9a3412"), QColor("#065f46")


class ImportDialog(QDialog):
    def __init__(self, files, infos, card, library_root, mapping, pending_dests, parent=None, aliases=None, names=None):
        super().__init__(parent)
        self.aliases = aliases or {}
        self.names = names or {}
        self.files, self.infos, self.card = files, infos, card
        self.library_root, self.mapping, self.pending = library_root, mapping, pending_dests
        self.batch: ImportBatch | None = None
        self.setWindowTitle(f"Import {len(files)} file{'s' if len(files) != 1 else ''}")
        self.resize(1250, 700)
        lay = QVBoxLayout(self)

        # guess source / vendor / pack from the first file
        src0, drop0 = files[0]
        g_source, g_vendor, g_pack = guess_vendor_pack(src0, library_root, drop0)
        top = QHBoxLayout()
        box = QGroupBox("Where it goes")
        grid = QGridLayout(box)
        grid.addWidget(QLabel("Source"), 0, 0)
        self.src_group = QButtonGroup(self)
        srow = QHBoxLayout()
        for s in ("FREE", "PAID"):            # factory samples come only from Initialize Factory Samples
            rb = QRadioButton(s.title())
            rb.setProperty("source", s)
            self.src_group.addButton(rb)
            srow.addWidget(rb)
            if s == (g_source if g_source in ("FREE", "PAID") else "PAID"):
                rb.setChecked(True)
        self.src_hint = QLabel("" if g_source else "← choose: did it cost money?")
        self.src_hint.setStyleSheet("color:#9a3412;")
        srow.addWidget(self.src_hint)
        srow.addStretch()
        grid.addLayout(srow, 0, 1)
        grid.addWidget(QLabel("Vendor"), 1, 0)
        self.vendor = QLineEdit(g_vendor)
        grid.addWidget(self.vendor, 1, 1)
        grid.addWidget(QLabel("Pack"), 2, 0)
        self.pack = QLineEdit(g_pack)
        grid.addWidget(self.pack, 2, 1)
        top.addWidget(box, 3)

        pbox = QGroupBox("Conversion")
        pl = QVBoxLayout(pbox)
        self.profile = QComboBox()
        self.profile.addItems(list(PROFILES))
        pl.addWidget(self.profile)
        self.mono = QCheckBox("Fold to mono")
        self.trim = QCheckBox("Trim leading silence")
        self.norm = QCheckBox("Peak normalise to −1 dBFS")
        self.fade = QCheckBox("5 ms fade-out")
        for w in (self.mono, self.trim, self.norm, self.fade):
            pl.addWidget(w)
        pl.addWidget(QLabel("<span style='color:#4f535b'>Always: WAV, 44.1 kHz, 24-bit, plain header, loop points kept.<br/>"
                            "Files already in that format are copied untouched.</span>"))
        top.addWidget(pbox, 2)
        lay.addLayout(top)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Original", "Is", "Becomes on the card", "What happens"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setTextElideMode(Qt.ElideLeft)       # keep the file name visible in long paths
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 210)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(3, 300)
        lay.addWidget(self.table, 1)
        self.checks = QLabel()
        self.checks.setWordWrap(True)
        lay.addWidget(self.checks)
        bb = QDialogButtonBox()
        self.ok = bb.addButton("Add to pending", QDialogButtonBox.AcceptRole)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.profile.currentTextChanged.connect(self._profile_chosen)
        for w in (self.mono, self.trim, self.norm, self.fade):
            w.toggled.connect(self._rebuild)
        self.vendor.textChanged.connect(self._rebuild)
        self.pack.textChanged.connect(self._rebuild)
        self.src_group.buttonToggled.connect(lambda *_: self._rebuild())
        self._profile_chosen(self.profile.currentText())

    def _profile_chosen(self, name):
        p = PROFILES[name]
        for w, v in ((self.mono, p.channels == "mono"), (self.trim, p.trim_silence),
                     (self.norm, p.normalise), (self.fade, p.fade_out)):
            w.blockSignals(True)
            w.setChecked(v)
            w.blockSignals(False)
        self._rebuild()

    def _current_profile(self):
        base = PROFILES[self.profile.currentText()]
        p = replace(base, channels="mono" if self.mono.isChecked() else "keep", trim_silence=self.trim.isChecked(),
                    normalise=self.norm.isChecked(), fade_out=self.fade.isChecked())
        if p != base:
            p = replace(p, name=base.name + " (adjusted)")
        return p

    def _rebuild(self):
        source = self.src_group.checkedButton().property("source")
        self.src_hint.setText("")
        b = build_plan(self.files, source, self.vendor.text().strip(), self.pack.text().strip(),
                       self._current_profile(), self.card, self.library_root, self.mapping,
                       taken_extra=self.pending, infos=self.infos, aliases=self.aliases, names=self.names)
        self.batch = b
        self.table.setRowCount(len(b.items))
        for r, it in enumerate(b.items):
            info = it.info
            cells = [os.path.basename(it.src), f"{info.container} {info.short_format()}" + (" ext. hdr" if info.header == "EXTENSIBLE" else ""),
                     it.card_rel if not it.skip else "—", it.skip or it.note + (f"  ⚠ {it.warn}" if it.warn else "")]
            for c, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setToolTip(it.src if c == 0 else text)
                if it.skip:
                    cell.setForeground(GREY)
                elif c == 3 and (it.warn or "resampled" in text):
                    cell.setForeground(AMBER)
                elif c == 2:
                    cell.setForeground(GREEN)
                self.table.setItem(r, c, cell)
        active = b.active
        skipped = Counter(i.skip.split(":")[0].split(" as ")[0] for i in b.items if i.skip)
        copies = sum(1 for i in active if i.copy_original)
        need = bytes_needed([b])
        free = self.card.facts.free or 0
        size = f"{need / 1e6:.0f} MB" if need >= 1e6 else f"{max(1, need // 1000)} KB"
        lines = [f"<b>{len(active)}</b> to import · about <b>{size}</b> on the card"
                 + (f" · {free / 1e9:.1f} GB free" if free else "")]
        if skipped:
            lines.append("Left out: " + ", ".join(f"{n} {why}" for why, n in skipped.items()))
        if copies:
            lines.append(f"{copies} original{'s' if copies != 1 else ''} will be copied into the library under "
                         f"<b>{source}/{self.vendor.text().strip() or '…'}/{self.pack.text().strip() or '…'}</b> — "
                         "the library keeps the originals; the card gets the converted copies.")
        if not self.library_root or not os.path.isdir(self.library_root):
            lines.append("<span style='color:#9a3412'>The library folder isn't reachable, so originals won't be copied "
                         "into it. Check Settings.</span>")
        warns = sum(1 for i in active if i.warn)
        if warns:
            lines.append(f"<span style='color:#9a3412'>{warns} with a long path</span>")
        if self.card.is_usb:
            from dsm.usb.usbcard import eta
            colour = "#b91c1c" if need > 5_000_000 else "#9a3412"
            lines.append(f"<span style='color:{colour}'>Over USB the copy runs at about 30 KB/s: {eta(need)}."
                         + (" For this many, the card reader is much quicker." if need > 5_000_000 else "") + "</span>")
        if free and need > free:
            lines.append("<span style='color:#b91c1c'><b>Not enough room on the card.</b></span>")
        self.checks.setText("<br/>".join(lines))
        self.ok.setEnabled(bool(active) and not (free and need > free) and
                           (source == "FACTORY" or bool(self.vendor.text().strip() and self.pack.text().strip())))
        if source != "FACTORY" and not (self.vendor.text().strip() and self.pack.text().strip()):
            self.checks.setText(self.checks.text() + "<br/><span style='color:#9a3412'>Fill in vendor and pack.</span>")
        self.vendor.setEnabled(source != "FACTORY")
        self.pack.setEnabled(source != "FACTORY")
