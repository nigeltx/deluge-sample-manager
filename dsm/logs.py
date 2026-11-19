"""Logging and crash capture. Any unexpected error is written to the log and shown in a
dialog with a button that opens the log folder, so the file can be sent for a fix."""
from __future__ import annotations

import logging
import os
import sys
import traceback

from dsm.settings import app_dir

log = logging.getLogger("dsm")


def log_dir() -> str:
    d = os.path.join(app_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def setup() -> str:
    path = os.path.join(log_dir(), "dsm.log")
    from logging.handlers import RotatingFileHandler
    h = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)
    return path


def install_excepthook():
    def hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        log.error("Unhandled error\n%s", text)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance():
                box = QMessageBox(QMessageBox.Critical, "Something went wrong",
                                  "The app hit an unexpected error. Nothing on your card was changed.\n\n"
                                  f"{etype.__name__}: {value}\n\n"
                                  "The details are in the log file — send it to Claude to get it fixed.")
                box.setDetailedText(text)
                open_btn = box.addButton("Open log folder", QMessageBox.ActionRole)
                box.addButton(QMessageBox.Ok)
                box.exec()
                if box.clickedButton() is open_btn:
                    open_folder(log_dir())
        except Exception:  # noqa: BLE001
            pass
    sys.excepthook = hook


def open_folder(path: str):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        import subprocess
        subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", path])
