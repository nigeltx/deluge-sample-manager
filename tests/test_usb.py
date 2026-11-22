"""The whole app against a pretend Deluge on USB: listing, index, health, import, rename, factory
move and undo must give the same results as with the card in a reader."""
import os

import pytest

from dsm.edit import edits as ed
from dsm.importer import apply as ap
from dsm.importer.convert import Profile
from dsm.importer.mapping import Mapping
from dsm.importer.plan import build_plan, expand
from dsm.index.health import check
from dsm.index.index import build_index
from dsm.usb.link import DelugeLink, pack7, unpack7
from dsm.usb.usbcard import UsbCard
from tests.fake_deluge import FakeDeluge
from tests.fixture import build_card, write_extensible, write_wav

KICK = "SAMPLES/PAID/MARS/808/DRUMS/KICK/BD_808_01.WAV"


def test_pack_roundtrip():
    data = bytes(range(256)) * 3 + b"\xff\x80"
    assert unpack7(pack7(data)) == data


@pytest.fixture()
def usb(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    root = str(tmp_path / "sd")
    build_card(root)
    fake = FakeDeluge(root)
    link = DelugeLink(fake, timeout=2)
    assert link.ping()
    link.start_session()
    assert (link.mid_lo, link.mid_hi) == (9, 15)
    card = UsbCard(link, cache_dir=str(tmp_path / "cache"))
    card.scan()
    return card, fake, root, tmp_path


def test_listing_and_index_match_reader(usb):
    card, fake, root, _ = usb
    from dsm.card.card import Card
    fs = Card(root)
    fs.scan()
    assert set(card.files) == set(fs.files)
    idx = build_index(card)
    assert {r.xml for r in idx.uses(KICK)} == {"KITS/PSY KIT 1.XML", "SONGS/PS01 FRACTAL DAWN.XML"}
    assert [r.path for r in idx.missing] == ["SAMPLES/PAID/MARS/808/DRUMS/PERC/MOVED_AWAY.WAV"]
    # second index comes from the PC cache: no file reads
    before = fake.sent
    build_index(card)
    assert fake.sent == before


def test_headers_over_usb_and_cache(usb):
    card, fake, root, _ = usb
    i = card.inspect("SAMPLES/FREE/PACKS/VOL1/PADS/LOOPED_PAD.WAV")
    assert (i.rate, i.loop_start, i.loop_end, i.root_note) == (48000, 1000, 9000, 57)
    e = card.inspect("SAMPLES/FREE/PACKS/VOL1/LOOPS/ext header loop.wav")
    assert e.header == "EXTENSIBLE" and not e.deluge_plays
    before = fake.sent
    card.inspect("SAMPLES/FREE/PACKS/VOL1/PADS/LOOPED_PAD.WAV")
    assert fake.sent == before
    rep = check(build_index(card), [])
    assert rep.counts["missing"] == 1 and rep.counts["convert"] == 2


def test_import_over_usb(usb):
    card, fake, root, tmp = usb
    src = tmp / "dl" / "V" / "P"
    write_wav(str(src / "Kick 9.wav"), rate=48000)
    write_extensible(str(src / "Pad.wav"))
    m = Mapping(str(tmp / "lib"))
    b = build_plan(expand([str(src)]), "FREE", "V", "P", Profile(), card, str(tmp / "lib"), m)
    j = ap.apply([b], card, str(tmp / "lib"), m, str(tmp / "bk"), True)
    assert len(j.created_card) == 2
    for rel in j.created_card:
        assert os.path.exists(os.path.join(root, *rel.split("/")))
        assert card.inspect(rel).deluge_plays
    assert not any(".dsmtmp" in f for f in card.files)
    ap.undo(j, card, str(tmp / "lib"), m)
    assert all(not os.path.exists(os.path.join(root, *r.split("/"))) for r in j.created_card)


def test_rename_and_factory_move_over_usb(usb):
    card, fake, root, tmp = usb
    kit = os.path.join(root, "KITS", "PSY KIT 1.XML")
    kit_before = open(kit, "rb").read()
    m = Mapping(str(tmp / "lib"))
    idx = build_index(card)
    new = KICK.replace("BD_808_01", "KICK_LONG")
    b1 = ed.EditBatch("rename", moves=[(KICK, new)])
    folders, loose = ed.factory_candidates(card)
    b2 = ed.factory_move(card, folders, loose)
    p = ed.plan([b1, b2], idx)
    assert not p.problems
    j = ed.apply(p, [b1, b2], card, m, str(tmp / "bk"), True)
    assert open(kit, "rb").read() == kit_before.replace(KICK.encode(), new.encode())
    assert os.path.exists(os.path.join(root, "SAMPLES", "FACTORY", "OLDSTUFF", "KICKS", "kick_unused.wav"))
    assert not [n for n in os.listdir(os.path.join(root, "KITS")) if n.startswith(".dsm")]
    # what the Deluge has now indexes cleanly
    card.scan()
    idx2 = build_index(card)
    assert idx2.uses(new) and len(idx2.missing) == 1
    assert ed.undo(j, card, m) == []
    assert open(kit, "rb").read() == kit_before
    assert os.path.exists(os.path.join(root, *KICK.split("/")))
    assert os.path.exists(os.path.join(root, "SAMPLES", "OLDSTUFF", "KICKS", "kick_unused.wav"))


def test_timeout_is_an_error(tmp_path):
    class Dead:
        import queue as _q
        q = _q.Queue()

        def send(self, m):
            pass

        def close(self):
            pass
    link = DelugeLink(Dead(), timeout=0.2)
    assert not link.ping(timeout=0.2)
    with pytest.raises(Exception, match="didn't answer"):
        link.listdir("/")
