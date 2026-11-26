"""The sample list must stay quick when switching between big library lists (0.7.1 fix)."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_big_lists_switch_quickly(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication
    from dsm.settings import Settings
    from dsm.ui import main_window
    from dsm.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(MainWindow, "_scan_library", lambda self, quiet=False, then=None: None)
    w = MainWindow(Settings.load())
    w.settings["library_root"] = str(tmp_path / "lib")
    w.show()
    rows = []
    for i in range(20000):
        rows.append({"rel": f"Sources/Paid/Vendor {i % 40}/Pack/S{i:05}.wav", "area": "Sources", "audio": str(i),
                     "source": "Paid", "level1": f"Vendor {i % 40}",
                     "level2": "Pack", "name": f"S{i:05}.wav", "size": 1000, "mtime": 0, "container": "WAV",
                     "header": "PCM", "rate": 44100, "bits": 24, "channels": 1, "frames": 44100, "loop_start": None,
                     "loop_end": None, "root_note": None, "plays": 1, "native": 1, "problems": "", "error": None,
                     "types": ",Kick,", "is_loop": 0, "type_why": "file name", "type_by_user": 0})
    t = time.time()
    for title in ("One", "Two", "Three"):
        w._list_library(rows, title)
        app.processEvents()
    assert w.table.rowCount() == 20000
    assert time.time() - t < 20, "switching between big lists is too slow"
    w.pending = []
    w.close()
