"""The clean-PC check (1.0): `SampleManager.exe --selftest <folder>` builds a small library and a small Deluge card
in <folder>, runs the app against them with its own settings (never the user's), and writes selftest.txt there.
Exit code 0 when every step passed. The release workflow runs it on a fresh Windows machine after installing the
Setup .exe, so a release whose installed app can't start, read a library, read a card or plan a change is never
published.

Each step is one line in selftest.txt: "ok   <step>" or "FAIL <step>: <reason>". Nothing here touches a real card,
library or the Syntakt; MIDI is only listed.
"""
from __future__ import annotations

import os
import struct
import sys
import time
import traceback


def _wav(path: str, frames: int, rate: int = 44100, bits: int = 16, chans: int = 1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    block = chans * bits // 8
    data = bytes((i * 7) % 251 for i in range(frames * block))
    fmt = struct.pack("<HHIIHH", 1, chans, rate, rate * block, block, bits)
    body = b"WAVEfmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def _build(where: str) -> tuple[str, str]:
    lib = os.path.join(where, "Samples")
    card = os.path.join(where, "card")
    _wav(os.path.join(lib, "Paid", "Test Pack", "Kicks", "BD Deep 01.wav"), 3000)
    _wav(os.path.join(lib, "Free", "Co", "Loops", "Bass Loop 145 F#m.wav"), 9000)
    _wav(os.path.join(lib, "User", "Pad Warm.wav"), 40000, bits=24, chans=2)
    _wav(os.path.join(card, "SAMPLES", "PAID", "V", "P", "KICK", "BD_808.WAV"), 2000)
    _wav(os.path.join(card, "SAMPLES", "DRUMS", "SNARE", "SD_01.WAV"), 2500)
    os.makedirs(os.path.join(card, "KITS"), exist_ok=True)
    with open(os.path.join(card, "KITS", "KIT000.XML"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<firmwareVersion>4.1.0</firmwareVersion>\n<kit>\n'
                '<soundSources><sound><osc1><type>sample</type>'
                '<fileName>SAMPLES/PAID/V/P/KICK/BD_808.WAV</fileName></osc1></sound>'
                '<sound><osc1><type>sample</type><fileName>SAMPLES/DRUMS/SNARE/SD_01.WAV</fileName></osc1>'
                '</sound></soundSources>\n</kit>\n')
    return lib, card


def run(where: str) -> int:
    where = os.path.abspath(where)
    os.makedirs(where, exist_ok=True)
    os.environ["LOCALAPPDATA"] = os.path.join(where, "appdata")     # the app's own settings and db, not the user's
    lines: list[str] = []
    failed = False

    def step(name, fn):
        nonlocal failed
        t = time.time()
        try:
            got = fn()
            lines.append(f"ok   {name}" + (f" — {got}" if isinstance(got, str) else "") + f" ({time.time() - t:.1f} s)")
            return got
        except Exception as e:  # noqa: BLE001
            failed = True
            lines.append(f"FAIL {name}: {e}\n" + traceback.format_exc())
            return None

    from dsm import __version__
    lines.append(f"Sample Manager {__version__} self-test, {time.strftime('%Y-%m-%d %H:%M')}, Python {sys.version.split()[0]}")
    lib, card_root = step("make a test library and card", lambda: _build(where)) or (None, None)

    def imports():
        import lxml.etree, numpy, soxr, soundfile, sounddevice, rtmidi, psutil   # noqa: F401,E401
        return "lxml, numpy, soxr, soundfile, sounddevice, rtmidi, psutil"
    step("libraries load", imports)

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([sys.argv[0]])

    def window():
        from dsm.settings import Settings
        from dsm.ui.main_window import MainWindow
        MainWindow._startup = lambda self: None              # no card search, no Syntakt, no update check
        s = Settings.load()
        s["library_root"] = lib
        s["first_run_done"] = True
        w = MainWindow(s)
        w.show()
        app.processEvents()
        return w
    w = step("main window opens", window)


    def library():
        from dsm.library.scanner import scan
        scan(w.libdb, lib)
        w._tags_load()
        w._build_library_root()
        rows = w.libdb.query(listed=True)
        assert len(rows) == 3, f"{len(rows)} samples listed, expected 3"
        assert all(r["sid"] for r in rows), "a sample has no ID"
        return f"{len(rows)} samples, IDs " + ", ".join(sorted(r["sid"] for r in rows))
    if w:
        step("library scan, types and IDs", library)

    def search():
        w.tree.setCurrentItem(w._find_tree_item(("samplesroot", None)))
        w.search.setText("loop bpm:140-150 key:F#m")
        w._do_search()
        n = w.table.rowCount()
        assert n == 1, f"search found {n}, expected 1"
        return "loop bpm:140-150 key:F#m → 1"
    if w:
        step("search filters", search)

    def card():
        from dsm.card.card import Card
        from dsm.index.index import build_index
        from dsm.index.health import check
        c = Card(card_root)
        c.scan()
        idx = build_index(c)
        assert not idx.missing, f"{len(idx.missing)} missing references"
        rep = check(idx)
        assert not rep.of("xml"), "the test kit reads as damaged"
        w.card, w.index = c, idx
        w._card_ready((c, idx))
        app.processEvents()
        return f"{len(c.sample_files())} samples, {len(idx.docs)} preset"
    if w:
        step("card read, index and health check", card)

    def sort():
        from dsm.edit import edits as ed
        from dsm.edit.sortcard import sort_batch
        batch, _ = sort_batch(w.card, w.card_types, w.card_audio, w.id_labels)
        assert batch is not None, "nothing to sort"
        p = ed.plan([batch], w.index)
        assert not p.problems, "; ".join(p.problems)
        return f"{len(batch.moves)} move(s) planned, kit rewrite ready (nothing written)"
    if w:
        step("Sort the card by type (plan only)", sort)

    def midi():
        import rtmidi
        return f"{len(rtmidi.MidiIn().get_ports())} MIDI inputs"
    step("MIDI ports listed", midi)

    if w:
        try:
            w.pending = []
            w.close()
        except Exception:  # noqa: BLE001
            pass
    lines.append("RESULT: " + ("FAILED" if failed else "PASSED"))
    with open(os.path.join(where, "selftest.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 1 if failed else 0
