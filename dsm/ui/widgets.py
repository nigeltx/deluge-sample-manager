"""Small reusable pieces: a background worker and the waveform view."""
from __future__ import annotations

import logging
import traceback

import numpy as np
from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

log = logging.getLogger("dsm.ui")


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)


class Job(QRunnable):
    """Run fn(progress=...) on the thread pool. Signals arrive on the UI thread."""

    def __init__(self, fn, *args, wants_progress=False, **kw):
        super().__init__()
        self.fn, self.args, self.kw, self.wants_progress = fn, args, kw, wants_progress
        self.signals = _Signals()
        self.setAutoDelete(True)

    def run(self):
        try:
            if self.wants_progress:
                self.kw["progress"] = lambda i, n, s="": self.signals.progress.emit(i, n, s)
            self.signals.done.emit(self.fn(*self.args, **self.kw))
        except Exception as e:  # noqa: BLE001
            log.error("Background job failed\n%s", traceback.format_exc())
            self.signals.failed.emit(f"{e.__class__.__name__}: {e}")


def run_job(job: Job, done=None, failed=None, progress=None) -> Job:
    if done:
        job.signals.done.connect(done)
    if failed:
        job.signals.failed.connect(failed)
    if progress:
        job.signals.progress.connect(progress)
    QThreadPool.globalInstance().start(job)
    return job


class Waveform(QWidget):
    def __init__(self):
        super().__init__()
        self.peaks = None
        self.frames = 0
        self.loop = None
        self.message = "Select a sample"
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, peaks: np.ndarray | None, frames: int = 0, loop=None, message: str = ""):
        self.peaks, self.frames, self.loop, self.message = peaks, frames, loop, message
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.fillRect(r, QColor("#f1f4fb"))
        p.setPen(QPen(QColor("#c9cbd0")))
        p.drawRect(r)
        if self.peaks is None or len(self.peaks) == 0:
            p.setPen(QColor("#6b6f77"))
            p.drawText(r, Qt.AlignCenter, self.message)
            return
        mid, h, w = r.center().y(), r.height() / 2 - 4, r.width()
        n = len(self.peaks)
        top = max(float(self.peaks.max()), 1e-6)
        pen = QPen(QColor("#2563eb"))
        pen.setWidthF(max(1.0, w / n))
        p.setPen(pen)
        for i, v in enumerate(self.peaks):
            x = r.left() + i * w / n
            a = float(v) / top * h
            p.drawLine(int(x), int(mid - a), int(x), int(mid + a))
        if self.loop and self.frames:
            s, e = self.loop
            p.setPen(QPen(QColor("#065f46"), 1.5, Qt.DashLine))
            for f in (s, e):
                x = r.left() + f / self.frames * w
                p.drawLine(int(x), r.top(), int(x), r.bottom())
            p.fillRect(QRectF(r.left() + s / self.frames * w, r.top(), (e - s) / self.frames * w, 12), QColor(6, 95, 70, 60))
