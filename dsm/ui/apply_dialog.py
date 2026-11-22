"""Apply (spec 3.4) and History with Undo (spec 7.4)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout)

from dsm.importer import apply as ap


class ApplyDialog(QDialog):
    """Shows every file that will be written. Result: 'apply', 'dry' or rejected."""

    def __init__(self, batches, card, backup_root, full_backup, edit_plan=None, parent=None):
        super().__init__(parent)
        self.choice = None
        label = card.facts.label or card.root
        imports = [b for b in batches if getattr(b, "kind", "import") != "edit"]
        n = sum(len(b.active) for b in batches)
        self.setWindowTitle(f"Apply {n} change{'s' if n != 1 else ''} to {label}")
        self.resize(980, 560)
        lay = QVBoxLayout(self)
        text = []
        for k, b in enumerate(batches, 1):
            text.append(f"{k}  {b.summary()}")
            if b not in imports:
                continue
            for i in b.active:
                text.append(f"      {i.card_rel}")
                text.append(f"          from {i.src}   ({i.note})")
                if i.copy_original:
                    text.append(f"          original kept in the library as {i.library_rel}")
        if edit_plan is not None:
            text += ["", "CHANGES TO FILES ALREADY ON THE CARD"] + edit_plan.lines()
            if edit_plan.problems:
                text = ["CAN'T APPLY — fix these first (remove the change from Pending):"] + \
                       [f"  ✗ {x}" for x in edit_plan.problems] + [""] + text
        box = QPlainTextEdit("\n".join(text))
        box.setReadOnly(True)
        box.setStyleSheet("font-family:Consolas,monospace;")
        lay.addWidget(box, 1)
        what = "a list of every file on the card" + (" and a zip of every preset and song" if full_backup else "")
        if edit_plan is not None and edit_plan.xml:
            what += f", plus a copy of the {len(edit_plan.xml)} preset and song files that change"
        after = "re-read every new and changed file and check every rewritten path points at a real file."
        lay.addWidget(QLabel(f"<span style='color:#065f46'>Before writing: back up {what} to {backup_root}.<br/>"
                             f"After writing: {after}<br/>"
                             "Only the sample path inside each preset changes — every other byte stays as the Deluge "
                             "wrote it.</span>"))
        if card.is_usb:
            from dsm.importer.plan import bytes_needed
            from dsm.usb.usbcard import eta
            need = bytes_needed(imports) if imports else 0
            lay.addWidget(QLabel("<span style='color:#9a3412'>Over USB to the Deluge" +
                                 (f": copying samples takes {eta(need)}" if need else "") +
                                 ". Moves and renames are instant. Don't use the Deluge's file browser until it "
                                 "finishes.</span>"))
        warn = QLabel("<b>Don't remove the card or unplug the Deluge until this finishes.</b> Undo is in History "
                      "afterwards.")
        warn.setStyleSheet("background:#fff7ed;color:#7c2d12;padding:6px;border-left:3px solid #9a3412;")
        lay.addWidget(warn)
        row = QHBoxLayout()
        row.addStretch()
        for label_, choice in (("Cancel", None), ("Dry run only", "dry"), ("Apply", "apply")):
            b = QPushButton(label_)
            b.clicked.connect(lambda _=False, c=choice: self._pick(c))
            row.addWidget(b)
            if choice == "apply":
                b.setDefault(True)
            if choice and edit_plan is not None and edit_plan.problems:
                b.setEnabled(False)
        lay.addLayout(row)

    def _pick(self, c):
        self.choice = c
        self.accept() if c else self.reject()


class HistoryDialog(QDialog):
    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        self.setWindowTitle("History")
        self.resize(900, 520)
        lay = QVBoxLayout(self)
        self.list = QListWidget()
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.detail, 1)
        row = QHBoxLayout()
        self.undo_btn = QPushButton("Undo this change")
        self.undo_btn.clicked.connect(self._undo)
        row.addWidget(self.undo_btn)
        row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.list.currentItemChanged.connect(self._show)
        self._fill()

    def _fill(self):
        self.list.clear()
        for j in ap.history():
            if j.dry_run:
                continue
            state = "  (undone)" if j.undone else ""
            it = QListWidgetItem(f"{j.when} · {j.card_label or j.card_root} · " + "; ".join(j.summary) + state)
            it.setData(Qt.UserRole, j.id)
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.detail.setPlainText("No changes yet.")
            self.undo_btn.setEnabled(False)

    def _journal(self):
        it = self.list.currentItem()
        if not it:
            return None
        return next((j for j in ap.history() if j.id == it.data(Qt.UserRole)), None)

    def _show(self, *_):
        j = self._journal()
        if not j:
            return
        lines = [f"Card: {j.card_label} ({j.card_root})", f"Backup: {j.backup}", ""] + j.log
        if j.created_library:
            lines += ["", "Originals copied into the library:"] + [f"  {x}" for x in j.created_library]
        self.detail.setPlainText("\n".join(lines))
        self.undo_btn.setEnabled(not j.undone and bool(j.created_card or j.moved or j.trashed or j.xml_backups))

    def _undo(self):
        j = self._journal()
        card = self.main.card
        if not j or not card:
            return
        if j.kind == "edit":
            q = (f"Put back {len(j.moved)} moved and {len(j.trashed)} trashed files, and restore the "
                 f"{len(j.xml_backups)} preset and song files from the backup?")
        else:
            q = (f"Move the {len(j.created_card)} files this change added into the card's DSM_TRASH folder, "
                 "and the originals it copied into the library's _DSM/trash folder?")
        if QMessageBox.question(self, "Undo", q) != QMessageBox.Yes:
            return
        if j.kind == "edit":
            from dsm.edit import edits as ed
            problems = ed.undo(j, card, self.main.mapping)
        else:
            problems = ap.undo(j, card, self.main.settings["library_root"], self.main.mapping)
        if problems:
            QMessageBox.warning(self, "Undo", "\n".join(problems))
        self._fill()
        self.main._refresh()
