import hashlib
import os

import pytest

from dsm.card.card import Card
from dsm.edit import edits as ed
from dsm.edit import factory
from dsm.importer.mapping import Mapping
from dsm.index.index import build_index
from tests.fake_deluge import FakeDeluge
from tests.fixture import build_card


def _factory_move(card, tmp):
    lib = str(tmp / "lib")
    os.makedirs(lib, exist_ok=True)
    m = Mapping(lib)
    folders, loose = ed.factory_candidates(card)
    b = ed.factory_move(card, folders, loose)
    j = ed.apply(ed.plan([b], build_index(card)), [b], card, m, str(tmp / "bk"), True)
    rec = factory.make(card, [tuple(x) for x in j.moved], j.id)
    factory.save(rec, lib, card)
    return rec, lib, m, j


@pytest.fixture()
def card(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    build_card(str(tmp_path / "card"))
    c = Card(str(tmp_path / "card"))
    c.scan()
    return c


def test_record_copy_and_check(card, tmp_path):
    rec, lib, m, j = _factory_move(card, tmp_path)
    origs = {e.original for e in rec.entries}
    assert origs == {"SAMPLES/OLDSTUFF/KICKS/kick_unused.wav", "SAMPLES/LOOSE.WAV"}
    assert os.path.exists(card.abs(factory.CARD_NAME))
    assert factory.load(lib).entries[0].card.startswith("SAMPLES/FACTORY/")
    out = factory.copy_to_library(rec, card, lib, m)
    assert out["copied"] == 2 and not out["problems"]
    e = rec.by_card()["samples/factory/oldstuff/kicks/kick_unused.wav"]
    copy = os.path.join(lib, "Factory", "Deluge", "OLDSTUFF", "KICKS", "kick_unused.wav")
    assert open(copy, "rb").read() == open(card.abs(e.card), "rb").read()
    assert e.sha1 == hashlib.sha1(open(copy, "rb").read()).hexdigest()
    assert m.for_card(e.card).library == "Factory/Deluge/OLDSTUFF/KICKS/kick_unused.wav"
    assert factory.missing_from_library(rec, lib) == []
    assert factory.copy_to_library(factory.load(lib), card, lib, m)["skipped"] == 2
    assert factory.check(rec, card) == []
    with open(card.abs(e.card), "ab") as f:
        f.write(b"xx")
    card.scan()
    assert [k for k, _ in factory.check(rec, card)] == ["changed"]


def test_undo_marks_record(card, tmp_path):
    rec, lib, m, j = _factory_move(card, tmp_path)
    ed.undo(j, card, m, lib)
    assert factory.load(lib).undone


def test_record_over_usb(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    from dsm.usb.link import DelugeLink
    from dsm.usb.usbcard import UsbCard
    root = str(tmp_path / "sd")
    build_card(root)
    link = DelugeLink(FakeDeluge(root), timeout=2)
    c = UsbCard(link, cache_dir=str(tmp_path / "cache"))
    c.scan()
    rec, lib, m, j = _factory_move(c, tmp_path)
    assert os.path.exists(os.path.join(root, "DSM", "FACTORY_RECORD.JSON"))
    assert factory.load(None, c).entries
    assert factory.copy_to_library(rec, c, lib, m)["copied"] == 2


def test_factory_locks(card, tmp_path):
    rec, lib, m, j = _factory_move(card, tmp_path)
    idx = build_index(card)
    fk = "SAMPLES/FACTORY/OLDSTUFF/KICKS/kick_unused.wav"
    for b in (ed.EditBatch("r", moves=[(fk, fk.replace("kick_unused", "x"))]),
              ed.EditBatch("t", trash=[fk]),
              ed.EditBatch("in", moves=[("SAMPLES/FREE/MINE/PS01/MOVED_AWAY.WAV", "SAMPLES/FACTORY/X.WAV")]),
              ed.folder_move(card, "SAMPLES/FACTORY/OLDSTUFF", "SAMPLES/FREE/OLDSTUFF")):
        assert any("locked" in p for p in ed.plan([b], idx).problems)
    from dsm.importer.convert import Profile
    from dsm.importer.plan import build_plan
    from tests.fixture import write_wav
    src = str(tmp_path / "x.wav")
    write_wav(src)
    plan = build_plan([(src, None)], "FACTORY", "", "", Profile(), card, lib, m)
    assert plan.items[0].skip and "locked" in plan.items[0].skip
    # the library copy is locked by fingerprint (spec 2.2), not by the read-only mark Drive drops


def test_origin_and_existing_record(card, tmp_path):
    assert factory.origin("SAMPLES/FACTORY/DRUMS/K.WAV") == "Factory"
    assert factory.origin("SAMPLES/PAID/X/Y/K.WAV") == "Paid"
    assert factory.origin("SAMPLES/CLIPS/R.WAV") == "Deluge"
    assert factory.origin("FREE/V/P/k.wav", on_card=False) == "Free"
    rec = factory.record_from_existing(card)
    assert {e.original for e in rec.entries} == {"SAMPLES/SYNTHS/PAD & CHOIR.WAV"}


def test_copy_keeps_what_is_already_in_sources(card, tmp_path):
    """A copy already in Sources at the right size is never overwritten; its checksum is recorded."""
    rec, lib, m, j = _factory_move(card, tmp_path)
    e = rec.by_card()["samples/factory/loose.wav"]
    there = os.path.join(lib, "Factory", "Deluge", "loose.WAV")         # another case, as on Drive
    os.makedirs(os.path.dirname(there), exist_ok=True)
    with open(card.abs(e.card), "rb") as f, open(there, "wb") as o:
        o.write(f.read())
    os.utime(there, (1, 1))
    assert [x.card for x in factory.missing_from_library(rec, lib)] == ["SAMPLES/FACTORY/OLDSTUFF/KICKS/kick_unused.wav"]
    out = factory.copy_to_library(rec, card, lib, m)
    assert out == {"copied": 1, "skipped": 1, "problems": []}
    assert os.path.getmtime(there) == 1                     # untouched
    assert e.library == "Factory/Deluge/loose.WAV" and e.sha1


def test_sort_moves_factory_and_record_follows(card, tmp_path):
    """0.15: with a factory record, Sort the card by type moves factory samples too (named after their IDs); the record
    follows them, they stay locked where they land, their library copies are still found, and Undo puts it all back."""
    from dsm.edit.sortcard import sort_batch
    rec, lib, m, j = _factory_move(card, tmp_path)
    factory.copy_to_library(rec, card, lib, m)
    factory.remember(rec)
    kick = "SAMPLES/FACTORY/OLDSTUFF/KICKS/kick_unused.wav"
    assert factory.is_factory_card(kick)
    types = {kick: "Kick", "SAMPLES/FACTORY/LOOSE.WAV": "Unknown"}
    b0, _ = sort_batch(card, types, {}, {})
    assert b0 is None                                                    # without with_factory they stay put
    batch, _note = sort_batch(card, types, {kick.lower(): "a1"}, {"a1": "KK0001 Kick Unused"}, with_factory=True)
    assert batch.factory_sort and dict(batch.moves)[kick] == "SAMPLES/DRUMS/KICK/KK0001_KICK_UNUSED.WAV"
    p = ed.plan([batch], build_index(card))
    assert not p.problems
    j2 = ed.apply(p, [batch], card, m, str(tmp_path / "bk2"), False, library_root=lib)
    card.scan()
    rec2 = factory.load(lib)
    e = next(x for x in rec2.entries if x.original.endswith("kick_unused.wav"))
    assert e.card == "SAMPLES/DRUMS/KICK/KK0001_KICK_UNUSED.WAV"
    assert factory.load(None, card).by_card().get(e.card.lower())         # the card's copy of the record too
    assert factory.is_factory_card(e.card) and factory.origin(e.card) == "Factory"
    assert factory.missing_from_library(rec2, lib) == []                 # library copies found by their home path
    assert factory.check(rec2, card) == []
    assert m.for_card(e.card).library == "Factory/Deluge/OLDSTUFF/KICKS/kick_unused.wav"
    # still locked: no rename, no trash
    p3 = ed.plan([ed.EditBatch("x", moves=[(e.card, "SAMPLES/DRUMS/KICK/OTHER.WAV")])], build_index(card))
    assert p3.problems and "locked" in p3.problems[0]
    p4 = ed.plan([ed.EditBatch("t", trash=[e.card])], build_index(card))
    assert p4.problems
    # undo: files and record go back
    ed.undo(j2, card, m, lib)
    card.scan()
    assert card.exists(kick) and factory.load(lib).by_card().get(kick.lower())
    assert factory.is_factory_card(kick)
    factory.remember(None)


def test_non_audio_files_leave_the_record(card, tmp_path):
    """0.15.2: files that aren't samples (REAPER's .reapeaks) are not factory samples: never recorded, and dropped
    from an older record."""
    open(card.abs("SAMPLES/OLDSTUFF/KICKS/kick_unused.wav.reapeaks"), "wb").write(b"RPKx")
    card.scan()
    rec, lib, m, j = _factory_move(card, tmp_path)
    assert all(factory.is_audio(e.card) for e in rec.entries) and len(rec.entries) == 2
    rec.entries.append(factory.Entry(original="SAMPLES/DRUMS/Clap/808 Clap.WAV.reapeaks",
                                     card="SAMPLES/FACTORY/DRUMS/Clap/808 Clap.WAV.reapeaks", size=10, mtime=0))
    assert factory.drop_non_audio(rec) == ["SAMPLES/FACTORY/DRUMS/Clap/808 Clap.WAV.reapeaks"]
    assert len(rec.entries) == 2 and factory.drop_non_audio(rec) == []
