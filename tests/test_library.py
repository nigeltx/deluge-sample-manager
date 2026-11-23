import os
import time

from dsm.library.db import LibraryDB, split_rel
from dsm.library.scanner import info_from_row, scan
from tests.fixture import write_extensible, write_wav


def test_scan_counts_and_incremental(tmp_path):
    lib = tmp_path / "lib"
    write_wav(str(lib / "PAID" / "Mars" / "808" / "Kicks" / "BD 01.wav"))
    write_wav(str(lib / "PAID" / "Mars" / "808" / "Kicks" / "BD 02.wav"), rate=48000)
    write_extensible(str(lib / "FREE" / "Co" / "Pads" / "Pad.wav"))
    write_wav(str(lib / "FACTORY" / "DRUMS" / "KICK" / "K.WAV"))
    write_wav(str(lib / "loose.wav"))
    write_wav(str(lib / "_DSM" / "trash" / "x.wav"))
    db = LibraryDB(str(tmp_path / "l.db"))
    r = scan(db, str(lib))
    assert r == {"seen": 5, "read": 5, "removed": 0}
    c = db.counts()
    assert c[()] == 5 and c[("Paid",)] == 2 and c[("Paid", "Mars", "808")] == 2 and c[("Factory", "DRUMS", "KICK")] == 1
    assert c[("Other",)] == 1
    assert scan(db, str(lib))["read"] == 0                     # nothing changed: nothing read
    rows = db.query(source="Paid", level1="Mars")
    assert [x["name"] for x in rows] == ["BD 01.wav", "BD 02.wav"]
    assert [x["rel"] for x in db.query(wont_play=True)] == ["FREE/Co/Pads/Pad.wav"]
    assert [x["rel"] for x in db.query(text="bd 02")] == ["PAID/Mars/808/Kicks/BD 02.wav"]
    i = info_from_row(rows[1], str(lib))
    assert i.rate == 48000 and i.deluge_plays and not i.native
    os.remove(lib / "loose.wav")
    time.sleep(0.01)
    write_wav(str(lib / "PAID" / "Mars" / "808" / "Kicks" / "BD 01.wav"), n=100)
    os.utime(lib / "PAID" / "Mars" / "808" / "Kicks" / "BD 01.wav", (1, 1))
    assert scan(db, str(lib)) == {"seen": 4, "read": 1, "removed": 1}


def test_split_rel():
    assert split_rel("PAID/V/P/a/b.wav") == ("Paid", "V", "P")
    assert split_rel("FREE/V/b.wav") == ("Free", "V", "")
    assert split_rel("x.wav") == ("Other", "", "")
