"""0.13 (spec v2.4 section 8): tags, BPM and key, the search filters, saved smart lists."""
import os
import struct

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dsm.library import layout, search as ls, tags as lt, tempokey as tk
from dsm.library.db import LibraryDB
from dsm.library.scanner import scan
from tests.fixture import write_wav


def add_acid(path, tempo=128.0, root=66, flags=0x02):
    """Append an acid chunk (tempo, root note) to a WAV."""
    body = struct.pack("<IHHfIHHf", flags, root, 0x8000, 0.0, 8, 4, 4, tempo)
    with open(path, "r+b") as f:
        f.seek(0, 2)
        f.write(b"acid" + struct.pack("<I", len(body)) + body)
        size = f.tell() - 8
        f.seek(4)
        f.write(struct.pack("<I", size))


# ---------------------------------------------------------------- BPM and key from names (8.3)
def test_bpm_and_key_from_names():
    assert tk.from_path("Paid/P/Bass Loop 145 F#m.wav", True) == (145.0, "name", "F#m", "name")
    assert tk.from_path("pad_Gbm_90bpm.wav", False)[0::2] == (90.0, "F#m")      # flats written as sharps
    assert tk.from_path("Kick 120.wav", False)[0] is None                       # numbered one-shot: no tempo
    assert tk.from_path("Loop 120.wav", True)[0] == 120.0                        # …but a loop's bare number is
    assert tk.from_path("Snare B.wav", False)[2] is None                         # a variation, not a key
    assert tk.from_path("HDR_E_Syn_Bass_Oneshot.wav", False, tonal=True)[2] == "E"
    assert tk.from_path("Fsharp minor pad.wav", False)[2] == "F#m"
    assert tk.from_path("C maj stab.wav", False)[2] == "C"
    assert tk.from_path("-6db kick.wav", False)[2] is None
    assert tk.from_path("Paid/x.zip::Pack/120 BPM/Loop A.wav", True)[:2] == (120.0, "folder")
    assert tk.norm_key("gb minor") == "F#m" and tk.norm_key("A major") == "A" and tk.norm_key("zz") is None
    assert tk.parse_bpm("145") == 145 and tk.parse_bpm("fast") is None


def test_order_typed_then_acid_then_name():
    row = {"bpm": None, "key": None, "acid_bpm": 128.0, "acid_root": 66}
    r = tk.resolve("Loop 120 Am.wav", True, row)
    assert (r["bpm"], r["bpm_how"]) == (128.0, "from the acid chunk")
    assert (r["key"], r["key_how"]) == ("Am", "from the name")              # the name says minor; acid can't
    r = tk.resolve("Loop.wav", True, row)
    assert (r["key"], r["key_how"]) == ("F#", "root note from the acid chunk")
    r = tk.resolve("Loop 120 Am.wav", True, dict(row, bpm=140.0, key="C"))
    assert (r["bpm"], r["key"], r["bpm_how"]) == (140.0, "C", "set by you")


def test_acid_chunk_read(tmp_path):
    p = str(tmp_path / "loop.wav")
    write_wav(p)
    assert tk.acid_from_file(p) == (None, None)
    add_acid(p, 132.0, 69)
    assert tk.acid_from_file(p) == (132.0, 69)
    assert tk.acid_from_bytes(open(p, "rb").read()) == (132.0, 69)
    q = str(tmp_path / "noroot.wav")
    write_wav(q)
    add_acid(q, 90.0, 60, flags=0)
    assert tk.acid_from_file(q) == (90.0, None)


# ---------------------------------------------------------------- tags (8.2)
def test_tag_names_and_guesses():
    assert lt.norm("Dark Ambient") == "dark-ambient" and lt.norm("  ") is None
    assert lt.split_input("psy, Dark;  warm ,psy") == ["psy", "dark", "warm"]
    assert lt.guesses("Paid/Psytrance Kicks.zip::Kicks/K1.wav") == ["psytrance"]
    assert lt.guesses("Free/LoFi Dark/Pads/p.wav") == ["dark", "lo-fi"]
    assert lt.guesses("Free/Pack/Dark.wav") == []                           # never from the file name


def _lib(tmp_path):
    lib = tmp_path / "Samples"
    write_wav(str(lib / "Paid" / "Psychill Pack" / "Kicks" / "BD Deep 01.wav"), n=3000)
    write_wav(str(lib / "Paid" / "Psychill Pack" / "Loops" / "Bass Loop 145 F#m.wav"), n=8000)
    write_wav(str(lib / "Free" / "Co" / "Pads" / "Warm Pad.wav"), n=60000)
    write_wav(str(lib / "Free" / "Co" / "Loops" / "Groove Loop.wav"), n=9000)
    add_acid(str(lib / "Free" / "Co" / "Loops" / "Groove Loop.wav"), 122.0, 64)
    os.makedirs(lib / "_Sample Manager", exist_ok=True)
    return lib


def test_db_tags_tempo_and_json_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "app"))
    lib = _lib(tmp_path)
    db = LibraryDB(str(tmp_path / "a.db"))
    scan(db, str(lib))
    by = {r["name"]: r["audio"] for r in db.query(listed=True)}
    groove, kick = by["Groove Loop.wav"], by["BD Deep 01.wav"]
    assert db.tempo_map()[groove]["acid_bpm"] == 122.0                       # read at scan time
    db.set_tags([kick], add=["dark", "punchy"])
    db.set_tags([kick], remove=["punchy"])
    db.set_tempo([kick], key="Am", set_key=True)
    assert db.tag_map()[kick] == {"yes": {"dark"}, "no": {"punchy"}}
    assert db.confirm_tags([(kick, "punchy"), (kick, "psychill")]) == 1     # a guess taken off stays off
    data = db.export_user_tags()
    # a rebuilt database (new PC): tags.json brings everything back
    db2 = LibraryDB(str(tmp_path / "b.db"))
    scan(db2, str(lib))
    db2.import_user_tags(data)
    assert db2.tag_map()[kick] == {"yes": {"dark", "psychill"}, "no": {"punchy"}}
    assert db2.tempo_map()[kick]["key"] == "Am"
    assert db2.tempo_map()[groove]["acid_bpm"] == 122.0
    p = str(lib / "_Sample Manager" / "tags.json")
    assert lt.save(p, data) and lt.load(p)["tags"][kick]["yes"] == ["dark", "psychill"]


# ---------------------------------------------------------------- search filters (8.4)
def ctx(**kw):
    c = {"id": "KK0001", "text": "kk0001 bd deep paid/psychill pack/kicks/bd deep 01.wav dark", "path": "Paid/Psychill Pack/Kicks/BD Deep 01.wav",
         "tags": {"dark"}, "types": ["Kick"], "loop": False, "bpm": None, "key": None, "seconds": 0.07,
         "on": set(), "source": "Paid", "dupe": False, "plays": True}
    c.update(kw)
    return c


def m(q, **kw):
    terms, _ = ls.parse(q)
    return ls.matches(terms, ctx(**kw))


def test_every_filter_finds_what_it_says():
    assert m("deep") and m("kk0001") and not m("snare")
    assert m("type:kick") and m("type:drums") and not m("type:hat") and m("type:hat", types=["Open hat"])
    assert m("-loop") and not m("loop") and m("loop", loop=True)
    assert m("tag:dark") and not m("tag:warm") and m("-tag:warm")
    assert m("bpm:140-150", bpm=145.0) and not m("bpm:140-150", bpm=128.0) and not m("bpm:145")
    assert m("key:Gbm", key="F#m") and not m("key:F#", key="F#m")
    assert m("len:<1s") and not m("len:>4s") and m("len:1-2s", seconds=1.5)
    assert m("-on:any") and m("on:syntakt", on={"syntakt"}) and m("on:any", on={"deluge"}) and not m("on:deluge")
    assert m("src:paid") and not m("src:factory") and m('pack:"psychill pack"') and not m('pack:"dark mass"')
    assert m("dupe", dupe=True) and not m("dupe") and m("wontplay", plays=False) and not m("wontplay")
    assert m("untagged", tags=set()) and not m("untagged")
    assert m("type:kick tag:dark -loop len:<1s src:paid")                       # all words must match


def test_unknown_filters_are_text():
    terms, unknown = ls.parse("colour:red kick")
    assert unknown == ["colour:red"] and [t.kind for t in terms] == ["text", "text"]
    assert ls.parse('pack:"dark')[0][0].kind == "pack"                         # an unclosed quote is tolerated


def test_saved_lists_file(tmp_path):
    p = str(tmp_path / "smart-lists.json")
    assert ls.load_lists(p) == []
    assert ls.save_lists(p, [{"name": "Dark kicks", "query": "type:kick tag:dark"}, {"bad": 1}])
    assert ls.load_lists(p) == [{"name": "Dark kicks", "query": "type:kick tag:dark"}]


# ---------------------------------------------------------------- in the window
def test_window_tags_search_and_smart_lists(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "app"))
    from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox
    from dsm.settings import Settings
    from dsm.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    lib = _lib(tmp_path)
    monkeypatch.setattr(MainWindow, "_scan_library", lambda self, quiet=False, then=None: None)
    monkeypatch.setattr(MainWindow, "_syn_connect", lambda self, *a, **k: None)
    w = MainWindow(Settings.load())
    w.settings["library_root"] = str(lib)
    scan(w.libdb, str(lib))
    w._tags_load()
    w._build_library_root()
    w._list_library(w.libdb.query(listed=True), "Library")
    names = lambda: sorted(w._row_at(i)["file_name"] for i in range(w.table.rowCount()))
    assert len(names()) == 4

    # BPM and key columns from names and the acid chunk
    rows = {w._row_at(i)["file_name"]: i for i in range(w.table.rowCount())}
    assert w.table.item(rows["Bass Loop 145 F#m.wav"], 10).data(0) == 145.0
    assert w.table.item(rows["Bass Loop 145 F#m.wav"], 11).text() == "F#m"
    assert w.table.item(rows["Groove Loop.wav"], 10).data(0) == 122.0
    assert "psychill?" in w.table.item(rows["BD Deep 01.wav"], 12).text()       # guessed from the pack name

    # tag the kick in the details pane; confirm the guess; type a key
    w.table.selectRow(rows["BD Deep 01.wav"])
    w._row_select(rows["BD Deep 01.wav"])
    w.tagbox.added.emit("dark")
    w.tagbox.confirmed.emit("psychill")
    w.key_edit.setText("am")
    w._key_typed()
    kick = w.current["audio"]
    assert w.libdb.tag_map()[kick]["yes"] == {"dark", "psychill"}
    assert w.libdb.tempo_map()[kick]["key"] == "Am"
    assert lt.load(str(lib / "_Sample Manager" / "tags.json"))["tags"][kick]["yes"] == ["dark", "psychill"]

    # searches over the whole library (LIBRARY selected)
    w.tree.setCurrentItem(w._find_tree_item(("samplesroot", None)))
    for q, want in (("tag:dark", ["BD Deep 01.wav"]), ("loop bpm:140-150", ["Bass Loop 145 F#m.wav"]),
                    ("key:F#m", ["Bass Loop 145 F#m.wav"]), ("-loop src:free", ["Warm Pad.wav"]),
                    ("type:kick tag:psychill", ["BD Deep 01.wav"]), ("untagged loop", ["Bass Loop 145 F#m.wav", "Groove Loop.wav"])):
        w.search.setText(q)
        w._do_search()
        assert names() == want, q
    w.search.setText("colour:red")
    w._do_search()
    assert "fff3e0" in w.search.styleSheet()                                # not a filter: amber
    w.search.clear()                                                         # back to the list it filtered
    assert len(names()) == 4

    # save a search as a smart list; it shows under SMART LISTS with its count, and lists what it finds
    w.search.setText("tag:dark")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Dark ones", True))
    w._save_smart_list()
    assert ls.load_lists(str(lib / "_Sample Manager" / "smart-lists.json")) == [{"name": "Dark ones", "query": "tag:dark"}]
    w._update_saved_counts()
    it = w._find_tree_item(("saved", "Dark ones"))
    assert it is not None and it.text(0).endswith("(1)")
    w.tree.setCurrentItem(it)
    assert names() == ["BD Deep 01.wav"]

    # Tags… for many: add to all, take one off all, set BPM
    sel = [w._row_at(i) for i in range(w.table.rowCount())]
    from dsm.ui import tag_widgets
    class Fake:
        def __init__(self, *a, **k):
            self.result_value = {"add": ["warm"], "remove": ["dark"], "bpm": (True, 150.0), "key": (False, None)}
        def exec(self):
            return True
    monkeypatch.setattr("dsm.ui.tags_ui.TagsDialog", Fake)
    w._tags_dialog(sel)
    assert w.libdb.tag_map()[kick]["yes"] == {"warm", "psychill"}
    assert w.libdb.tempo_map()[kick]["bpm"] == 150.0

    # Confirm guessed tags for a whole pack
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    w._confirm_pack_guesses({"source": "Paid", "level1": "Psychill Pack"})
    loop = next(r["audio"] for r in w.libdb.query(listed=True) if r["name"].startswith("Bass Loop"))
    assert "psychill" in w.libdb.tag_map()[loop]["yes"]
    w.pending = []
    w.close()
