"""The first-run guide (1.0): on a PC where the library folder isn't found, a short guide sets up the app —
the library (an existing one, or a new empty one with the folders the app expects), where backups go, and how the
Deluge and the Syntakt connect. Help ▾ › First-run guide… opens it again at any time.

No file logic beyond making the empty library folders (layout.make_library); the window does the rest.
"""
from __future__ import annotations

import os

from PySide6.QtWidgets import (QButtonGroup, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
                               QVBoxLayout, QWizard, QWizardPage)

from dsm import APP_NAME
from dsm.library import layout


def _text(t: str) -> QLabel:
    lab = QLabel(t)
    lab.setWordWrap(True)
    return lab


def describe(root: str) -> tuple[str, bool]:
    """(what the guide says about a folder, whether it is a library already)."""
    if not root:
        return "Choose a folder.", False
    if not os.path.isdir(root):
        return "This folder doesn't exist yet. The app will make it, with the folders below.", False
    have = [s for s in layout.SOURCE_TYPES if layout.find_child(root, s)]
    if have:
        n = 0
        for s in have:
            d = layout.find_child(root, s)
            try:
                n += len(os.listdir(d))
            except OSError:
                pass
        return (f"A sample library: {', '.join(have)} ({n:,} packs and folders). The app reads it and never "
                "changes it."), True
    try:
        empty = not os.listdir(root)
    except OSError:
        empty = False
    if empty:
        return "An empty folder. The app will make the library folders in it.", False
    return ("This folder has other things in it but no Factory, Paid, Free or User folder. The app will add those "
            "four beside what is there; put your packs into them."), False


class FirstRunGuide(QWizard):
    """result: dict(library_root, backup_root, deluge='card'|'usb'|'later', make=bool) after exec()."""

    def __init__(self, parent, library_root: str, backup_root: str):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — first-run guide")
        self.setWizardStyle(QWizard.ClassicStyle)
        self.resize(640, 440)
        self.result_value = None

        p1 = QWizardPage()
        p1.setTitle("Welcome")
        l1 = QVBoxLayout(p1)
        t = QLabel(
            f"<p>{APP_NAME} keeps one sample library for your devices — the Synthstrom Deluge and the Elektron "
            "Syntakt — and puts converted copies on each of them.</p>"
            "<ul><li><b>The library</b> is a folder of the packs as they came: <b>Factory</b> (each device's own "
            "samples), <b>Paid</b>, <b>Free</b> and <b>User</b>. The app reads it and never changes it.</li>"
            "<li>The left pane lists the samples <b>by type</b> (Kick, Snare … Pad, FX), each with an ID such as "
            "<i>KK0212 BD Deep 01</i>.</li>"
            "<li>On the Deluge's card, samples go into folders by type, and every kit, synth and song follows when "
            "they move. Every change waits in <b>Pending</b> until you press Apply, is backed up first, and can be "
            "undone in History.</li></ul>"
            "<p>The next pages set up the library, the backups and the devices. It takes a minute.</p>")
        t.setWordWrap(True)
        l1.addWidget(t)
        self.addPage(p1)

        p2 = QWizardPage()
        p2.setTitle("Your sample library")
        l2 = QVBoxLayout(p2)
        l2.addWidget(_text("Where your samples live (or should). On Google Drive it is kept on every PC."))
        row = QHBoxLayout()
        self.lib = QLineEdit(library_root or "")
        pick = QPushButton("Browse…")
        pick.clicked.connect(lambda: self.lib.setText(
            QFileDialog.getExistingDirectory(self, "Sample library folder", self.lib.text()) or self.lib.text()))
        row.addWidget(self.lib, 1)
        row.addWidget(pick)
        l2.addLayout(row)
        self.lib_state = QLabel()
        self.lib_state.setWordWrap(True)
        l2.addWidget(self.lib_state)
        l2.addWidget(_text("A new library gets: Factory › Deluge, Factory › Syntakt, Paid, Free, User, "
                            "Backups and _Sample Manager (the app's own notes)."))
        l2.addStretch()
        self.lib.textChanged.connect(self._lib_changed)
        self.addPage(p2)

        p3 = QWizardPage()
        p3.setTitle("Backups")
        l3 = QVBoxLayout(p3)
        l3.addWidget(_text("Before any change to the card, the presets and songs it touches are copied here, "
                            "with the date. Undo uses these copies."))
        row = QHBoxLayout()
        self.bk = QLineEdit(backup_root or "")
        self.bk.setPlaceholderText("the library's Backups › Deluge")
        bpick = QPushButton("Browse…")
        bpick.clicked.connect(lambda: self.bk.setText(
            QFileDialog.getExistingDirectory(self, "Backup folder", self.bk.text()) or self.bk.text()))
        row.addWidget(self.bk, 1)
        row.addWidget(bpick)
        l3.addLayout(row)
        l3.addStretch()
        self.addPage(p3)

        p4 = QWizardPage()
        p4.setTitle("Your devices")
        l4 = QVBoxLayout(p4)
        l4.addWidget(_text("<b>Deluge</b> — how will the app reach its SD card?"))
        self.grp = QButtonGroup(self)
        self.r_card = QRadioButton("A card reader: put the Deluge's card in the PC (fastest; found by itself)")
        self.r_usb = QRadioButton("USB: the Deluge stays connected (needs Community Firmware 1.3 or later)")
        self.r_later = QRadioButton("Later — I'll choose in the Card list at the top")
        for i, r in enumerate((self.r_card, self.r_usb, self.r_later)):
            self.grp.addButton(r, i)
            l4.addWidget(r)
        self.r_card.setChecked(True)
        syn = QLabel("<p><b>Syntakt</b> — connect it by USB and it shows under TARGETS by itself. Close Overbridge and "
                     "Elektron Transfer first: only one program can talk to it at a time. After the app changes its "
                     "samples, restart the Syntakt.</p>")
        syn.setWordWrap(True)
        l4.addSpacing(10)
        l4.addWidget(syn)
        l4.addStretch()
        self.addPage(p4)

        self._lib_changed(self.lib.text())

    def _lib_changed(self, text):
        msg, _is_lib = describe(text.strip())
        self.lib_state.setText(msg)
        if not self.bk.text().strip() or self.bk.text().strip().startswith(getattr(self, "_auto_bk_base", "\0")):
            self._auto_bk_base = text.strip()
            self.bk.setText(os.path.join(text.strip(), layout.BACKUPS, "Deluge") if text.strip() else "")

    def accept(self):
        root = self.lib.text().strip()
        if not root:
            self.setCurrentId(1)
            return
        self.result_value = {"library_root": root, "backup_root": self.bk.text().strip(),
                             "deluge": ("card", "usb", "later")[max(0, self.grp.checkedId())],
                             "make": not describe(root)[1]}
        super().accept()
