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
                     "types": ",Kick,", "is_loop": 0, "type_why": "file name", "type_by_user": 0, "sid": None, "sid_name": None})
    t = time.time()
    for title in ("One", "Two", "Three"):
        w._list_library(rows, title)
        app.processEvents()
    assert w.table.rowCount() == 20000
    assert time.time() - t < 20, "switching between big lists is too slow"
    w.pending = []
    w.close()


def test_first_run_guide(tmp_path, monkeypatch):
    """1.0: a PC without a library gets the first-run guide; finishing it makes the library folders and saves the
    settings; a PC with a library skips it."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "app"))
    from PySide6.QtWidgets import QApplication
    from dsm.library import layout
    from dsm.settings import Settings
    from dsm.ui import first_run
    from dsm.ui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    new = tmp_path / "Studio" / "Samples"
    assert first_run.describe(str(new))[1] is False
    made = layout.make_library(str(new))
    assert {os.path.basename(p) for p in made} >= {"Deluge", "Syntakt", "Paid", "Free", "User", "_Sample Manager"}
    assert first_run.describe(str(new))[1] is True and layout.make_library(str(new)) == []

    seen = {}

    class Fake:
        def __init__(self, parent, lib, bk):
            seen.setdefault("asked", lib)
            self.result_value = {"library_root": str(tmp_path / "Lib2"), "backup_root": str(tmp_path / "bk"),
                                 "deluge": "later", "make": True}

        def exec(self):
            return True
    monkeypatch.setattr(first_run, "FirstRunGuide", Fake)
    monkeypatch.setattr(MainWindow, "_startup_devices", lambda self: seen.setdefault("devices", True))
    s = Settings.load()
    s["library_root"] = str(tmp_path / "nowhere")
    w = MainWindow(s)
    w._startup()
    app.processEvents()
    import time
    t = time.time()
    while "devices" not in seen and time.time() - t < 3:
        app.processEvents()
    assert seen["asked"] == str(tmp_path / "nowhere")
    assert os.path.isdir(tmp_path / "Lib2" / "Factory" / "Deluge") and w.settings["first_run_done"]
    assert w.settings["library_root"] == str(tmp_path / "Lib2") and w.settings["backup_root"] == str(tmp_path / "bk")
    # a PC with a library: no guide
    seen.clear()
    s2 = Settings.load()
    s2["library_root"] = str(new)
    s2["first_run_done"] = False
    w2 = MainWindow(s2)
    w2._startup()
    assert "asked" not in seen and w2.settings["first_run_done"]
    w.close(); w2.close()
