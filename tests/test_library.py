"""The v2.2 library: one folder, fingerprints, zips read in place, every sample once by type, Reorganize, Get."""
import io
import os
import shutil
import time
import zipfile

from dsm.library import layout, sources as src
from dsm.library.db import LibraryDB
from dsm.library.scanner import scan
from tests.fixture import write_extensible, write_wav


def wav_bytes(tmp_path, name, **kw):
    p = tmp_path / "tmp" / name
    write_wav(str(p), **kw)
    return p.read_bytes()


def make_lib(tmp_path, top=""):
    """A library in the v2.2 layout (top="Sources" makes 0.9.0's)."""
    lib = tmp_path / "Samples"
    S = lib / top if top else lib
    write_wav(str(S / "Factory" / "Deluge" / "DRUMS" / "KICK" / "808 KICK.WAV"))
    write_wav(str(S / "Paid" / "Mars" / "808" / "Kicks" / "BD 01.wav"), n=3000)
    write_wav(str(S / "Paid" / "Mars" / "808" / "Loops" / "Groove 120 BPM.wav"), n=5000)
    write_extensible(str(S / "Free" / "Co" / "Pads" / "Pad.wav"))
    # a pack as a zip, with a zip inside it and a copy of the factory kick
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Snares/Snare 1.wav", wav_bytes(tmp_path, "s1.wav", n=2000))
        zf.writestr("Extra/808.wav", (S / "Factory" / "Deluge" / "DRUMS" / "KICK" / "808 KICK.WAV").read_bytes())
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as iz:
            iz.writestr("Claps/Clap.wav", wav_bytes(tmp_path, "c.wav", n=1500))
        zf.writestr("Bonus.zip", inner.getvalue())
    (S / "Paid" / "Dark Mass.zip").write_bytes(z.getvalue())
    write_wav(str(lib / "Backups" / "Deluge" / "old.wav"))          # never read
    write_wav(str(lib / "_Sample Manager" / "Removed" / "x.wav"))   # never read
    write_wav(str(S / "Paid" / "Archive" / "gone.wav"))              # never read
    write_wav(str(lib / "loose at the top.wav"))                     # outside the four folders: never read
    return lib


def test_identity_ignores_header_differences(tmp_path):
    a = wav_bytes(tmp_path, "a.wav")
    # the same audio with an extra chunk before the data has the same identity
    extra = a[:12] + b"LIST" + (4).to_bytes(4, "little") + b"abcd" + a[12:]
    extra = extra[:4] + (len(extra) - 8).to_bytes(4, "little") + extra[8:]
    assert src.identity(a) == src.identity(extra)
    assert src.identity(a) != src.identity(wav_bytes(tmp_path, "b.wav", n=100))


def test_split_rel():
    assert src.split_rel("Paid/Dark Mass.zip::Snares/Snare 1.wav") == ("Library", "Paid", "Dark Mass", "Snares")
    assert src.split_rel("Factory/Deluge/DRUMS/KICK/X.WAV") == ("Library", "Factory", "Deluge", "DRUMS")
    assert src.split_rel("PAID/x.wav") == ("Library", "Paid", "", "")


def test_scan_reads_zips_and_skips_backups(tmp_path):
    lib = make_lib(tmp_path)
    db = LibraryDB(str(tmp_path / "l.db"))
    r = scan(db, str(lib))
    rels = {x["rel"] for x in db.query()}
    assert "Paid/Dark Mass.zip::Bonus.zip::Claps/Clap.wav" in rels
    assert "Paid/Dark Mass.zip::Snares/Snare 1.wav" in rels
    assert len(rels) == 7 and r["read"] == 7
    assert not any("Backups" in x or "Removed" in x or "Archive" in x or "loose" in x for x in rels)
    assert len(db.source_files()) == 5                      # four files and one zip
    assert scan(db, str(lib))["read"] == 0                   # nothing changed: nothing read
    assert os.path.exists(lib / "_Sample Manager" / "sources.json")
    data = src.read_bytes(str(lib), "Paid/Dark Mass.zip::Bonus.zip::Claps/Clap.wav")
    assert data[:4] == b"RIFF"
    p = src.materialize(str(lib), "Paid/Dark Mass.zip::Snares/Snare 1.wav", str(tmp_path / "cache"))
    assert os.path.getsize(p) > 44 and p.startswith(str(tmp_path / "cache"))


def test_changed_missing_moved_and_user_deletions(tmp_path):
    lib = make_lib(tmp_path)
    write_wav(str(lib / "User" / "mine.wav"), n=777)
    db = LibraryDB(str(tmp_path / "l.db"))
    scan(db, str(lib))
    n0 = len(db.query())
    bd = lib / "Paid" / "Mars" / "808" / "Kicks" / "BD 01.wav"
    write_wav(str(bd), n=3333)                               # changed outside the app
    os.utime(bd, (time.time() + 5, time.time() + 5))
    os.remove(lib / "Free" / "Co" / "Pads" / "Pad.wav")      # gone
    os.remove(lib / "User" / "mine.wav")                     # gone from User: meant
    os.rename(lib / "Paid" / "Dark Mass.zip", lib / "Paid" / "Dark Mass Vol 1.zip")   # moved
    r = scan(db, str(lib))
    assert r["changed"] == 1 and r["missing"] == 1 and r["moved"] == 1 and r["removed"] == 1
    alerts = {a["path"]: a["state"] for a in db.source_alerts()}
    assert alerts == {"Paid/Mars/808/Kicks/BD 01.wav": "changed", "Free/Co/Pads/Pad.wav": "missing"}
    assert len(db.query()) == n0 - 1                         # still using what was read; only User's file forgotten
    db.accept_change("Paid/Mars/808/Kicks/BD 01.wav")
    db.accept_missing("Free/Co/Pads/Pad.wav")
    scan(db, str(lib))
    assert db.source_alerts() == []
    assert len(db.query()) == n0 - 2


def test_every_sample_once_by_type(tmp_path):
    lib = make_lib(tmp_path)
    db = LibraryDB(str(tmp_path / "l.db"))
    r = scan(db, str(lib))
    assert r["listed"] == 6                                  # 7 copies, the 808 twice
    listed = db.query(listed=True)
    assert len(listed) == 6
    kick = db.query(folder="Kick")
    assert [x["rel"] for x in kick if "808 KICK" in x["rel"]] == ["Factory/Deluge/DRUMS/KICK/808 KICK.WAV"]   # factory counts
    assert db.type_counts()["Kick"] == 2 and db.type_counts(loop=True) == {"Other drums": 1}
    assert [x["name"] for x in db.query(folder="Clap")] == ["Clap.wav"]
    # Set type… changes the list and nothing on disk
    before = sorted(p.name for p in lib.rglob("*"))
    a = kick[0]["audio"]
    db.set_user_type([a], ["Tom"], False)
    assert a in {x["audio"] for x in db.query(folder="Tom")}
    assert a not in {x["audio"] for x in db.query(folder="Kick")}
    assert sorted(p.name for p in lib.rglob("*")) == before
    assert len(db.copies(a)) == (2 if "808 KICK" in kick[0]["rel"] else 1)


def test_type_folder():
    assert layout.type_folder(["Hat"]) == "Hat"
    assert layout.type_folder(["Kick", "Snare"]) == "Kick"
    assert layout.type_folder([]) == "Unknown"


# ------------------------------------------------------------------ Reorganize and Get from the Deluge
import pytest  # noqa: E402

from dsm.card.card import Card  # noqa: E402
from dsm.edit import edits as ed, factory  # noqa: E402
from dsm.importer.mapping import Link, Mapping  # noqa: E402
from dsm.index.index import build_index  # noqa: E402
from dsm.library import getdevice, reorganize  # noqa: E402
from tests.fixture import build_card  # noqa: E402


@pytest.fixture()
def card(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    build_card(str(tmp_path / "card"))
    c = Card(str(tmp_path / "card"))
    c.scan()
    return c


def _record_08(card, tmp_path, lib):
    m = Mapping(str(tmp_path / "elsewhere"))
    folders, loose = ed.factory_candidates(card)
    b = ed.factory_move(card, folders, loose)
    j = ed.apply(ed.plan([b], build_index(card)), [b], card, m, str(tmp_path / "bk"), True)
    rec = factory.make(card, [tuple(x) for x in j.moved], j.id)
    rec.entries[0].library = "FACTORY/" + rec.entries[0].card[len("SAMPLES/FACTORY/"):]     # 0.8's place
    return rec


def _cleanup_list(tmp_path):
    cl = tmp_path / "Studio" / "Software" / "Studio Cleanup" / "Cleanup list.txt"
    os.makedirs(cl.parent, exist_ok=True)
    cl.write_text("Samples\\Old\r\n")
    return cl


class _S(dict):
    def save(self):
        self["saved"] = True


def test_reorganize_from_08(card, tmp_path):
    lib = tmp_path / "Studio" / "Samples"
    os.makedirs(lib / "_DSM", exist_ok=True)
    rec = _record_08(card, tmp_path, lib)
    with open(lib / "_DSM" / "factory-record.json", "w") as f:
        f.write(rec.to_json())
    dig = lib / "Sources" / "Factory" / "Digitakt II"
    os.makedirs(dig, exist_ok=True)
    shutil.copy(card.abs("SAMPLES/FACTORY/LOOSE.WAV"), dig / "LOOSE.WAV")
    write_wav(str(lib / "Sources" / "Free" / "Co" / "Kick A.wav"))
    m = Mapping(str(lib))
    m.path = str(lib / "_DSM" / "mapping.json")
    m.add(Link(card="SAMPLES/FREE/CO/KICK A.WAV", library="FREE/Co/Kick A.wav"))
    m.save()
    os.makedirs(tmp_path / "appdata" / "SampleManager" / "Backups" / "CARD 2026-11-01 120000", exist_ok=True)
    cl = _cleanup_list(tmp_path)
    m = Mapping(str(lib))
    assert m.path.endswith(os.path.join("_DSM", "mapping.json"))
    rec = factory.load(str(lib))
    assert reorganize.needed(str(lib))
    steps = reorganize.plan(str(lib), m, rec, card)
    kinds = [s.kind for s in steps]
    assert kinds == ["rename", "flatten", "folders", "appfolder", "mapping", "record", "factory", "backups"], kinds
    assert (lib / "Sources").exists() and not (lib / "Free").exists()          # planning changes nothing
    st = _S()
    out = reorganize.apply(str(lib), steps, m, rec, card, settings=st)
    assert not out["problems"], out["problems"]
    assert (lib / "Factory" / "Deluge" / "LOOSE.WAV").exists()
    assert (lib / "Factory" / "Deluge" / "OLDSTUFF" / "KICKS" / "kick_unused.wav").exists()
    assert (lib / "Free" / "Co" / "Kick A.wav").exists() and not os.listdir(lib / "Sources")
    assert (lib / "_Sample Manager" / "mapping.json").exists() and not os.listdir(lib / "_DSM")
    assert Mapping(str(lib)).for_card("SAMPLES/FREE/CO/KICK A.WAV").library == "Free/Co/Kick A.wav"
    rec2 = factory.load(str(lib))
    assert all(e.library.startswith("Factory/Deluge/") and e.sha1 for e in rec2.entries)
    assert (lib / "Backups" / "Deluge" / "CARD 2026-11-01 120000").is_dir()
    assert st["backup_root"].endswith(os.path.join("Backups", "Deluge"))
    assert cl.read_text().splitlines()[-2:] == ["Samples\\Sources", "Samples\\_DSM"]
    assert not reorganize.needed(str(lib))                   # old folders on the Cleanup list don't count
    os.rmdir(lib / "_DSM")                                  # what Clean up Studio.cmd does
    os.rmdir(lib / "Sources")
    assert reorganize.plan(str(lib), Mapping(str(lib)), rec2, card) == []       # nothing left to do


def test_reorganize_from_090(card, tmp_path):
    """0.9.0's layout: Sources\\…, a Samples folder of copies (and one file dropped there by hand), its database."""
    lib = tmp_path / "Studio" / "Samples"
    make_lib(tmp_path / "Studio", top="Sources")
    os.makedirs(lib / "_Sample Manager", exist_ok=True)
    shutil.copy(lib / "Sources" / "Paid" / "Mars" / "808" / "Kicks" / "BD 01.wav", tmp_path / "bd.wav")
    os.makedirs(lib / "Samples" / "Kick")
    shutil.copy(tmp_path / "bd.wav", lib / "Samples" / "Kick" / "BD 01.wav")
    write_wav(str(lib / "Samples" / "Pad" / "my own.wav"), n=999)
    # 0.9.0's index: paths with Sources/
    db = LibraryDB(str(tmp_path / "l.db"))
    os.makedirs(lib / "Paid", exist_ok=True)
    tmpview = lib / "Paid"
    os.rmdir(tmpview)
    from dsm.library import scanner
    for rel, full in src.walk_files(str(lib / "Sources"), str(lib)):
        sha, rows = scanner.read_source_file(rel, full)
        for r in rows:
            r["area"] = "Sources"
        db.upsert(rows)
        st_ = os.stat(full)
        db.set_source_file(rel, st_.st_size, st_.st_mtime, sha)
    cl = _cleanup_list(tmp_path)
    rec = factory.record_from_existing(card)
    steps = reorganize.plan(str(lib), Mapping(str(lib)), None, None, db.source_audios())
    kinds = [s.kind for s in steps]
    assert kinds == ["flatten", "folders", "samples"], kinds
    assert len(steps[2].data["hand"]) == 1
    out = reorganize.apply(str(lib), steps, Mapping(str(lib)), None, None, db=db)
    assert not out["problems"], out["problems"]
    assert (lib / "User" / "Added by hand" / "my own.wav").exists()
    assert (lib / "Paid" / "Dark Mass.zip").exists()
    assert cl.read_text().splitlines()[-2:] == ["Samples\\Sources", "Samples\\Samples"]
    r = scan(db, str(lib))
    assert r["read"] == 1                                    # only the hand file; the rest followed by path
    assert r["missing"] == 0 and db.source_alerts() == []
    assert {x["rel"] for x in db.query()} >= {"Paid/Dark Mass.zip::Snares/Snare 1.wav", "User/Added by hand/my own.wav"}
    assert not any(x["rel"].startswith(("Sources/", "Samples/")) for x in db.query())
    del rec


def test_reorganize_waits_for_the_card(card, tmp_path):
    lib = tmp_path / "Samples"
    os.makedirs(lib, exist_ok=True)
    rec = _record_08(card, tmp_path, lib)
    steps = reorganize.plan(str(lib), None, rec, None)
    assert steps[-1].kind == "factory-wait" and steps[-1].data["n"] == len(rec.entries)


def test_get_from_the_deluge(card, tmp_path):
    lib = tmp_path / "Samples"
    write_wav(str(lib / "Free" / "x.wav"), n=123)
    db = LibraryDB(str(tmp_path / "l.db"))
    scan(db, str(lib))
    # a recording made on the Deluge, a copy of something already in Sources, a TEMP take, a mapped file
    write_wav(card.abs("SAMPLES/RECORD/REC001.WAV"), n=4321)
    shutil.copy(lib / "Free" / "x.wav", card.abs("SAMPLES/RECORD/REC002.WAV"))
    write_wav(card.abs("SAMPLES/CLIPS/TEMP/T1.WAV"), n=999)
    card.scan()
    m = Mapping(str(lib))
    found = getdevice.find_new(card, db, m)
    rels = [f.card_rel for f in found]
    assert "SAMPLES/RECORD/REC001.WAV" in rels
    assert "SAMPLES/RECORD/REC002.WAV" not in rels and not any("TEMP" in r or "FACTORY" in r.upper() for r in rels)
    for f in found:
        f.keep = f.card_rel == "SAMPLES/RECORD/REC001.WAV"
    out = getdevice.keep(card, str(lib), found)
    assert out["kept"] == 1 and not out["problems"]
    kept = lib / "User" / getdevice.folder_name() / "RECORD" / "REC001.WAV"
    assert kept.exists()
    scan(db, str(lib))
    assert "SAMPLES/RECORD/REC001.WAV" not in [f.card_rel for f in getdevice.find_new(card, db, m)]
