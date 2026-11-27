"""Widgets for tags (spec v2.4 8.2): the Tags box in the details pane, the Tags… dialog for many samples,
the search-filter help, and the tag words dialog."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QCompleter, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLayout,
                               QLineEdit, QMenu, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget, QHeaderView, QAbstractItemView)

from dsm.library import search as lsearch
from dsm.library import tags as ltags
from dsm.library import tempokey


class FlowLayout(QLayout):
    """Lays its widgets out in rows, wrapping like words."""

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self.items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, i):
        return self.items[i] if 0 <= i < len(self.items) else None

    def takeAt(self, i):
        return self.items.pop(i) if 0 <= i < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self.items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _do(self, rect, test):
        x, y, line = rect.x(), rect.y(), 0
        sp = self.spacing()
        for it in self.items:
            h = it.sizeHint()
            if x + h.width() > rect.right() and line > 0:
                x, y, line = rect.x(), y + line + sp, 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), h))
            x += h.width() + sp
            line = max(line, h.height())
        return y + line - rect.y()


CHIP = ("QPushButton{border:1px solid #93b0ea;background:#dbe5fb;color:#1d4ed8;border-radius:9px;"
        "padding:1px 8px;font-weight:600;}")
GUESS = ("QPushButton{border:1px dashed #b0b4bb;background:#f3f4f6;color:#6b6f77;border-radius:9px;"
         "padding:1px 8px;}")


class TagBox(QWidget):
    """The Tags row of the details pane: a chip per tag, guesses grey with '?', and a box to add one.
    Signals carry the tag; the window does the saving."""
    added = Signal(str)
    taken_off = Signal(str)
    confirmed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.flow = FlowLayout(self)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("add a tag…")
        self.edit.setFixedWidth(120)
        self.edit.returnPressed.connect(self._enter)
        self.completer = QCompleter([])
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.edit.setCompleter(self.completer)
        self.chips = []
        self.set_tags([], [], False)

    def set_words(self, words):
        from PySide6.QtCore import QStringListModel
        self.completer.setModel(QStringListModel(sorted(set(words)), self.completer))

    def set_tags(self, yes, guesses, enabled: bool):
        for c in self.chips:
            self.flow.removeWidget(c)
            c.hide()
            c.setParent(None)
            c.deleteLater()
        self.chips = []
        self.flow.removeWidget(self.edit)
        for t in yes:
            b = QPushButton(f"{t}  ×")
            b.setStyleSheet(CHIP)
            b.setToolTip("Click to take this tag off")
            b.clicked.connect(lambda _=False, t=t: self.taken_off.emit(t))
            self.chips.append(b)
        for t in guesses:
            b = QPushButton(f"{t} ?")
            b.setStyleSheet(GUESS)
            b.setToolTip("Guessed from the pack or folder name — click to confirm it or take it off.\n"
                         "Guesses don't count in searches until confirmed.")
            b.clicked.connect(lambda _=False, t=t, b=b: self._guess_menu(t, b))
            self.chips.append(b)
        for c in self.chips:
            c.setEnabled(enabled)
            self.flow.addWidget(c)
        self.flow.addWidget(self.edit)
        self.edit.setEnabled(enabled)
        self.edit.setVisible(enabled)
        self._fit()

    def _fit(self):
        """Tall enough for every row of chips at the current width (the form layout asks only for a size hint)."""
        w = max(self.width(), 200)
        self.setMinimumHeight(self.flow.heightForWidth(w))
        self.updateGeometry()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        h = self.flow.heightForWidth(max(self.width(), 200))
        if h != self.minimumHeight():
            self.setMinimumHeight(h)

    def _guess_menu(self, t, b):
        m = QMenu(self)
        yes = m.addAction(f"Confirm “{t}”")
        no = m.addAction(f"Take “{t}” off")
        got = m.exec(b.mapToGlobal(b.rect().bottomLeft()))
        if got is yes:
            self.confirmed.emit(t)
        elif got is no:
            self.taken_off.emit(t)

    def _enter(self):
        for t in ltags.split_input(self.edit.text()):
            self.added.emit(t)
        self.edit.clear()


class TagsDialog(QDialog):
    """Tags… for many samples: add tags to all, take tags off all, set or clear BPM and key.
    result: dict(add=[...], remove=[...], bpm=(set?, value), key=(set?, value)) or None."""

    def __init__(self, parent, n: int, counts: dict[str, int], words):
        super().__init__(parent)
        self.setWindowTitle("Tags")
        self.result_value = None
        self.n = n
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Tags for {n:,} sample{'s' if n != 1 else ''}. Tags belong to the sound, so every "
                             "copy gets them."))
        self.add = QLineEdit()
        self.add.setPlaceholderText("tags to add, separated by commas — e.g. psychill, dark")
        comp = QCompleter(sorted(set(words)))
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        self.add.setCompleter(comp)
        lay.addWidget(self.add)
        self.boxes = {}
        if counts:
            lay.addWidget(QLabel("Tags they have now — untick one to take it off all of them:"))
            inner = QWidget()
            il = QVBoxLayout(inner)
            for t, c in sorted(counts.items()):
                cb = QCheckBox(f"{t}   ({'all' if c == n else f'some — {c:,}'})")
                cb.setChecked(True)
                self.boxes[t] = cb
                il.addWidget(cb)
            il.addStretch()
            sc = QScrollArea()
            sc.setWidget(inner)
            sc.setWidgetResizable(True)
            sc.setMaximumHeight(200)
            lay.addWidget(sc)
        form = QFormLayout()
        self.bpm = QLineEdit()
        self.bpm.setPlaceholderText("leave empty to keep")
        self.key = QLineEdit()
        self.key.setPlaceholderText("leave empty to keep — e.g. F#m")
        form.addRow("BPM", self.bpm)
        form.addRow("Key", self.key)
        self.clear_tk = QCheckBox("Clear the BPM and key typed for them (go back to the guesses)")
        form.addRow("", self.clear_tk)
        lay.addLayout(form)
        self.msg = QLabel()
        self.msg.setStyleSheet("color:#9a3412;")
        lay.addWidget(self.msg)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(460, 300)

    def _ok(self):
        out = {"add": ltags.split_input(self.add.text()),
               "remove": [t for t, cb in self.boxes.items() if not cb.isChecked()],
               "bpm": (False, None), "key": (False, None)}
        if self.clear_tk.isChecked():
            out["bpm"], out["key"] = (True, None), (True, None)
        if self.bpm.text().strip():
            v = tempokey.parse_bpm(self.bpm.text())
            if v is None:
                self.msg.setText("BPM should be a number, such as 145.")
                return
            out["bpm"] = (True, v)
        if self.key.text().strip():
            k = tempokey.norm_key(self.key.text())
            if k is None:
                self.msg.setText("Key should be a note with an optional # or b and m for minor, such as F#m.")
                return
            out["key"] = (True, k)
        self.result_value = out
        self.accept()


class SearchHelp(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Search filters")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Every word must match. A minus in front turns a word round (-loop, -on:any).\n"
                             "The search works in the list you are looking at; select LIBRARY to search everything."))
        t = QTableWidget(len(lsearch.HELP), 3)
        t.setHorizontalHeaderLabels(["Filter", "Example", "Matches"])
        for i, row in enumerate(lsearch.HELP):
            for j, v in enumerate(row):
                t.setItem(i, j, QTableWidgetItem(v))
        t.verticalHeader().hide()
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        t.resizeColumnsToContents()
        t.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(QLabel("Save as smart list… keeps a search under SMART LISTS."))
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
        self.resize(780, 500)


class VocabDialog(QDialog):
    """The tag words that are guessed from pack and folder names (Settings › Tag words)."""

    def __init__(self, parent, vocab: dict):
        super().__init__(parent)
        self.setWindowTitle("Tag words")
        self.result_value = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("A pack or folder whose name holds one of these words gives its samples that tag as a "
                             "guess (grey, until you confirm it).\nWords separated by commas."))
        form = QFormLayout()
        self.edits = {}
        for g in ("style", "character"):
            e = QLineEdit(", ".join(vocab.get(g, [])))
            self.edits[g] = e
            form.addRow(g.title(), e)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(
            lambda: [self.edits[g].setText(", ".join(ltags.VOCAB[g])) for g in self.edits])
        lay.addWidget(bb)
        self.resize(560, 160)

    def _ok(self):
        self.result_value = {g: ltags.split_input(e.text()) for g, e in self.edits.items()}
        self.accept()
