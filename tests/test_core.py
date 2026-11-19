import os

import pytest

from dsm.audio.inspect import inspect
from dsm.card.card import Card
from dsm.index.health import check
from dsm.index.index import build_index, snippet, unused_samples
from tests.fixture import build_card


@pytest.fixture()
def card(tmp_path):
    build_card(str(tmp_path))
    c = Card(str(tmp_path))
    c.scan()
    return c


def test_headers(card):
    i = inspect(card.abs("SAMPLES/PAID/MARS/808/DRUMS/KICK/BD_808_01.WAV"))
    assert (i.container, i.header, i.rate, i.bits, i.channels, i.frames) == ("WAV", "PCM", 44100, 24, 1, 4410)
    assert i.native and i.deluge_plays
    e = inspect(card.abs("SAMPLES/FREE/PACKS/VOL1/LOOPS/ext header loop.wav"))
    assert e.header == "EXTENSIBLE" and e.bits == 24 and e.frames == 1000 and not e.deluge_plays
    lp = inspect(card.abs("SAMPLES/FREE/PACKS/VOL1/PADS/LOOPED_PAD.WAV"))
    assert (lp.loop_start, lp.loop_end, lp.root_note, lp.rate) == (1000, 9000, 57, 48000)
    assert lp.deluge_plays and not lp.native
    fl = inspect(card.abs("SAMPLES/FREE/PACKS/VOL1/MP3/vox.flac"))
    assert fl.container == "FLAC" and not fl.deluge_plays


def test_garbage_file_is_reported_not_fatal(tmp_path):
    p = tmp_path / "bad.wav"
    p.write_bytes(b"RIFF\x04\x00\x00\x00WAVEjunk")
    i = inspect(str(p))
    assert not i.deluge_plays


def test_index_forms_case_and_missing(card):
    idx = build_index(card)
    forms = sorted({r.form for r in idx.refs})
    assert forms == ["attr", "element", "filePath"]
    kick = idx.uses("SAMPLES/PAID/MARS/808/DRUMS/KICK/BD_808_01.WAV")
    assert {r.xml for r in kick} == {"KITS/PSY KIT 1.XML", "SONGS/PS01 FRACTAL DAWN.XML"}
    assert kick[0].row == "KICK"
    # lower-case path in the XML still resolves (FAT is case-insensitive)
    assert idx.uses("SAMPLES/PAID/MARS/808/DRUMS/SNARE/SD_808_01.WAV")
    # escaped ampersand in an element reference
    assert idx.uses("SAMPLES/FACTORY/SYNTHS/PAD & CHOIR.WAV")
    assert [r.path for r in idx.missing] == ["SAMPLES/PAID/MARS/808/DRUMS/PERC/MOVED_AWAY.WAV"]
    # byte offsets point exactly at the path text
    r = kick[0]
    with open(card.abs(r.xml), "rb") as f:
        assert f.read()[r.start:r.end].decode() == r.raw
    text, a, b = snippet(card, r)
    assert text[a:b] == r.raw
    assert any(d.error for d in idx.docs if d.rel == "KITS/BROKEN.XML")


def test_unused(card):
    idx = build_index(card)
    un = set(unused_samples(idx))
    assert "SAMPLES/OLDSTUFF/KICKS/kick_unused.wav" in un
    assert "SAMPLES/CLIPS/REC00001.WAV" not in un
    assert "SAMPLES/PAID/MARS/808/DRUMS/KICK/BD_808_01.WAV" not in un


def test_health(card, tmp_path):
    lib = tmp_path / "lib"
    os.makedirs(lib / "PAID" / "X")
    (lib / "PAID" / "X" / "MOVED_AWAY.WAV").write_bytes(b"")
    idx = build_index(card)
    rep = check(idx, [str(lib / "PAID" / "X" / "MOVED_AWAY.WAV")])
    m = rep.of("missing")[0]
    assert any("FREE/MINE/PS01/MOVED_AWAY.WAV" in s for s in m.suggestions)
    assert any("In the library" in s for s in m.suggestions)
    conv = {f.path for f in rep.of("convert")}
    assert "SAMPLES/FREE/PACKS/VOL1/LOOPS/ext header loop.wav" in conv
    assert "SAMPLES/FREE/PACKS/VOL1/MP3/vox.flac" in conv
    names = {f.path for f in rep.of("name")}
    assert "SAMPLES/FACTORY/SYNTHS/PAD & CHOIR.WAV" not in names     # factory is exempt
    assert "SAMPLES/FREE/PACKS/VOL1/LOOPS/ext header loop.wav" not in names  # spaces are allowed
    assert rep.counts["temp"] == 1
    layout = " ".join(f.title + f.detail for f in rep.of("layout"))
    assert "OLDSTUFF" in layout and "loose directly in SAMPLES" in layout
    assert rep.counts["xml"] == 1
