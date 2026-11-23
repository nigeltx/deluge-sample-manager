"""Set type…: choose the type(s) of one or more library samples, or go back to the app's guess."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QLabel, QPushButton,
                               QRadioButton, QVBoxLayout)

from dsm.library import types as stypes


class TypeDialog(QDialog):
    """result: ("set", types, loop) / ("guess",) / None when cancelled."""

    def __init__(self, parent, count: int, current: list[str], loop: bool):
        super().__init__(parent)
        self.setWindowTitle("Set type")
        self.result_value = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Type for {count:,} sample{'s' if count != 1 else ''}. Tick one or more."))
        self.boxes = {}
        for title, names in (("Drums", stypes.DRUMS), ("Other sounds", stypes.MUSIC)):
            g = QGroupBox(title)
            grid = QGridLayout(g)
            for k, t in enumerate(names):
                cb = QCheckBox(stypes.label(t))
                cb.setChecked(t in current)
                self.boxes[t] = cb
                grid.addWidget(cb, k // 3, k % 3)
            lay.addWidget(g)
        g = QGroupBox("One-shot or loop")
        gl = QVBoxLayout(g)
        self.one = QRadioButton("One-shot")
        self.loop = QRadioButton("Loop")
        (self.loop if loop else self.one).setChecked(True)
        gl.addWidget(self.one)
        gl.addWidget(self.loop)
        lay.addWidget(g)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        back = QPushButton("Go back to the app's guess")
        back.setToolTip("Forget your choice for these samples and use the guess from the folder and file names")
        bb.addButton(back, QDialogButtonBox.ResetRole)
        back.clicked.connect(self._back)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _ok(self):
        chosen = [t for t, cb in self.boxes.items() if cb.isChecked()]
        self.result_value = ("set", chosen or [stypes.UNKNOWN], self.loop.isChecked())
        self.accept()

    def _back(self):
        self.result_value = ("guess",)
        self.accept()
