import os

import pytest

from dsm.audio.inspect import inspect
from dsm.card.card import Card
from dsm.importer import apply as ap
from dsm.importer.convert import PROFILES, Profile, convert
from dsm.importer.mapping import Mapping
from dsm.importer.naming import bpm_key, card_name, card_path, classify, guess_vendor_pack
from dsm.importer.plan import build_plan, expand
from tests.fixture import add_smpl, build_card, write_extensible, write_wav


def test_naming():
    assert card_name("Roll Kick F#m 145bpm.flac") == "ROLL_KICK_F#M_145.WAV"
    assert card_name("acid stab 03.wav") == "ACID_STAB_03.WAV"
    assert card_name("Pad & Choir (wet).aif") == "PAD_AND_CHOIR_(WET).WAV"
    assert len(card_name("x" * 80 + ".wav")) == 40 + 4
    assert bpm_key("Loop A min 120 BPM")[1:] == ("120", "AM")
    assert classify(["BD_808_01.wav"]) == ("DRUMS", "KICK")
    assert classify(["OpenHat 2.wav"]) == ("DRUMS", "HAT-OPEN")
    assert classify(["thing.wav", "Claps"]) == ("DRUMS", "SNARE-CLAP-RIM")
    assert classify(["Dark Pad 01.wav"]) == ("PADS", "")
    assert classify(["weird.wav"]) == ("OTHER", "")
    assert card_path("PAID", "Samples From Mars", "808 From Mars", "/x/BD 01.wav") == \
        "SAMPLES/PAID/SAMPLES FROM MARS/808 FROM MARS/DRUMS/KICK/BD_01.WAV"
    assert card_path("FACTORY", "", "", "/x/Kick.wav", "DRUMS/Kick.wav") == "SAMPLES/FACTORY/DRUMS/KICK.WAV"


def test_guess_vendor_pack(tmp_path):
    lib = tmp_path / "lib"
    f = lib / "PAID" / "Mars" / "808" / "Kicks" / "BD.wav"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"")
    assert guess_vendor_pack(str(f), str(lib), None) == ("PAID", "Mars", "808")
    other = tmp_path / "Downloads" / "Vendor X" / "Pack Y"
    assert guess_vendor_pack(str(other / "a.wav"), str(lib), str(other)) == (None, "Vendor X", "Pack Y")


def test_convert_resample_keeps_loops(tmp_path):
    src = str(tmp_path / "in.wav")
    write_wav(src, rate=48000, n=48000)
    add_smpl(src, 4800, 24000, root=57)
    out = convert(src, str(tmp_path / "out.wav"), Profile())
    assert (out.rate, out.bits, out.header) == (44100, 24, "PCM")
    assert abs(out.frames - 44100) <= 1
    assert (out.loop_start, out.loop_end, out.root_note) == (4410, 22050, 57)


def test_convert_fixes_extensible_and_flac(tmp_path):
    e = str(tmp_path / "e.wav")
    write_extensible(e)
    out = convert(e, str(tmp_path / "e2.wav"), Profile())
    assert out.header == "PCM" and out.deluge_plays
    import soundfile as sf
    import numpy as np
    fl = str(tmp_path / "x.flac")
    sf.write(fl, np.zeros((441, 1), dtype="float32"), 44100)
    assert convert(fl, str(tmp_path / "x.wav"), Profile()).deluge_plays


def test_native_copied_untouched(tmp_path):
    src = str(tmp_path / "n.wav")
    write_wav(src)
    add_smpl(src, 10, 400)
    dst = str(tmp_path / "n2.wav")
    convert(src, dst, Profile())
    assert open(src, "rb").read() == open(dst, "rb").read()
    out = convert(src, str(tmp_path / "mono.wav"), PROFILES["Drums mono"])
    assert out.channels == 1


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    card_root = tmp_path / "card"
    build_card(str(card_root))
    card = Card(str(card_root))
    card.scan()
    lib = tmp_path / "lib"
    (lib / "PAID" / "Mars" / "Psy Kicks" / "Kicks").mkdir(parents=True)
    write_wav(str(lib / "PAID" / "Mars" / "Psy Kicks" / "Kicks" / "Roll Kick F#m 145.wav"), rate=48000)
    dl = tmp_path / "Downloads" / "FreeVendor" / "Pads Pack"
    write_extensible(str(dl / "Dark Pad.wav"))
    write_wav(str(dl / "sub" / "Closed Hat 1.wav"))
    return card, str(lib), dl, tmp_path


def test_plan_apply_undo(env):
    card, lib, dl, tmp = env
    m = Mapping(lib)
    # from the library: no copy, vendor/pack from the path
    f1 = os.path.join(lib, "PAID", "Mars", "Psy Kicks", "Kicks", "Roll Kick F#m 145.wav")
    b1 = build_plan([(f1, None)], "PAID", "Mars", "Psy Kicks", Profile(), card, lib, m)
    i1 = b1.items[0]
    assert i1.card_rel == "SAMPLES/PAID/MARS/PSY KICKS/DRUMS/KICK/ROLL_KICK_145_F#M.WAV"
    assert not i1.copy_original and i1.library_rel == "PAID/Mars/Psy Kicks/Kicks/Roll Kick F#m 145.wav"
    assert "resampled 48 → 44.1 kHz" in i1.note
    # from Downloads: original copied into the library
    b2 = build_plan(expand([str(dl)]), "FREE", "FreeVendor", "Pads Pack", Profile(), card, lib, m,
                    taken_extra=[i1.card_rel])
    assert {i.library_rel for i in b2.items} == {"Sources/Free/FreeVendor/Pads Pack/Dark Pad.wav",
                                                "Sources/Free/FreeVendor/Pads Pack/sub/Closed Hat 1.wav"}
    assert all(i.copy_original for i in b2.items)
    before = set(card.files)
    dry = ap.apply([b1, b2], card, lib, m, str(tmp / "bk"), True, dry_run=True)
    assert len(dry.log) == 5 and not os.path.exists(card.abs(i1.card_rel))
    j = ap.apply([b1, b2], card, lib, m, str(tmp / "bk"), True)
    assert len(j.created_card) == 3 and len(j.created_library) == 2
    for rel in j.created_card:
        info = inspect(card.abs(rel))
        assert info.deluge_plays and info.rate == 44100 and info.header == "PCM"
    assert os.path.exists(os.path.join(lib, "Sources", "Free", "FreeVendor", "Pads Pack", "Dark Pad.wav"))
    assert os.path.exists(os.path.join(j.backup, "presets-and-songs.zip"))
    m2 = Mapping(lib)  # reloaded from disk
    assert m2.for_card(i1.card_rel).library == i1.library_rel
    # importing the same library file again is skipped
    card.scan()
    again = build_plan([(f1, None)], "PAID", "Mars", "Psy Kicks", Profile(), card, lib, m2)
    assert again.items[0].skip and "already on the card" in again.items[0].skip
    # no temp files left anywhere
    assert not any(".dsmtmp" in k for k in card.files)
    # undo
    probs = ap.undo(j, card, lib, m2)
    assert probs == []
    card.scan()
    assert set(card.files) - before == {k.lower() for k in card.files if k.startswith("dsm_trash/")}
    assert m2.for_card(i1.card_rel) is None
    assert ap.history()[0].undone


def test_apply_refuses_existing_destination(env):
    card, lib, dl, tmp = env
    m = Mapping(lib)
    b = build_plan(expand([str(dl)]), "FREE", "V", "P", Profile(), card, lib, m)
    dest = card.abs(b.active[0].card_rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    open(dest, "wb").close()
    with pytest.raises(RuntimeError, match="already exists"):
        ap.apply([b], card, lib, m, str(tmp / "bk"), False)
