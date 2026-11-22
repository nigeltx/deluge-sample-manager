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
    copy = os.path.join(lib, "FACTORY", "OLDSTUFF", "KICKS", "kick_unused.wav")
    assert open(copy, "rb").read() == open(card.abs(e.card), "rb").read()
    assert e.sha1 == hashlib.sha1(open(copy, "rb").read()).hexdigest()
    assert m.for_card(e.card).library == "FACTORY/OLDSTUFF/KICKS/kick_unused.wav"
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
