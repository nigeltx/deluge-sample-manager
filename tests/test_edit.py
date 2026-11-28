import os

import pytest

from dsm.card.card import Card
from dsm.edit import edits as ed
from dsm.importer import apply as ap
from dsm.importer.mapping import Link, Mapping
from dsm.index.index import build_index
from tests.fixture import build_card

KICK = "SAMPLES/PAID/MARS/808/DRUMS/KICK/BD_808_01.WAV"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    build_card(str(tmp_path / "card"))
    card = Card(str(tmp_path / "card"))
    card.scan()
    return card, build_index(card), Mapping(str(tmp_path / "lib")), str(tmp_path / "bk")


def snapshot(card):
    out = {}
    for dp, _, fns in os.walk(card.root):
        for fn in fns:
            p = os.path.join(dp, fn)
            if "DSM_TRASH" not in p:
                out[os.path.relpath(p, card.root)] = open(p, "rb").read()
    return out


def test_rename_rewrites_only_path_bytes_and_undo_restores(env):
    card, idx, m, bk = env
    m.add(Link(card=KICK, library="PAID/Mars/808/Kicks/BD 01.wav"))
    before = snapshot(card)
    kit_before = before[os.path.join("KITS", "PSY KIT 1.XML")]
    new = "SAMPLES/PAID/MARS/808/DRUMS/KICK/KICK_808_LONG.WAV"
    b = ed.EditBatch("rename", moves=[(KICK, new)])
    p = ed.plan([b], idx)
    assert not p.problems
    assert {x.xml for x in p.xml} == {"KITS/PSY KIT 1.XML", "SONGS/PS01 FRACTAL DAWN.XML"}
    j = ed.apply(p, [b], card, m, bk, True)
    kit_after = open(card.abs("KITS/PSY KIT 1.XML"), "rb").read()
    assert kit_after == kit_before.replace(KICK.encode(), new.encode())
    assert os.path.exists(card.abs(new)) and not os.path.exists(card.abs(KICK))
    assert m.for_card(new).library == "PAID/Mars/808/Kicks/BD 01.wav"
    idx2 = build_index(card)
    assert {r.xml for r in idx2.uses(new)} == {"KITS/PSY KIT 1.XML", "SONGS/PS01 FRACTAL DAWN.XML"}
    assert ed.undo(j, card, m) == []
    assert snapshot(card) == before
    assert m.for_card(KICK) is not None


def test_folder_move_and_ampersand(env):
    card, idx, m, bk = env
    b = ed.folder_move(card, "SAMPLES/PAID/MARS/808/DRUMS/SNARE", "SAMPLES/PAID/MARS/808/DRUMS/SNARE & CLAP")
    p = ed.plan([b], idx)
    assert not p.problems
    ed.apply(p, [b], card, m, bk, False)
    data = open(card.abs("KITS/PSY KIT 1.XML"), encoding="utf-8").read()
    assert 'fileName="SAMPLES/PAID/MARS/808/DRUMS/SNARE &amp; CLAP/SD_808_01.WAV"' in data
    assert build_index(card).uses("SAMPLES/PAID/MARS/808/DRUMS/SNARE & CLAP/SD_808_01.WAV")
    assert not os.path.isdir(card.abs("SAMPLES/PAID/MARS/808/DRUMS/SNARE"))      # emptied folder removed


def test_relink_missing(env):
    card, idx, m, bk = env
    missing = idx.missing[0].path
    b = ed.EditBatch("relink", relinks=[(missing, "SAMPLES/FREE/MINE/PS01/MOVED_AWAY.WAV")])
    p = ed.plan([b], idx)
    assert p.ref_count == 1 and not p.moves
    ed.apply(p, [b], card, m, bk, False)
    assert build_index(card).missing == []


def test_factory_move(env):
    card, idx, m, bk = env
    folders, loose = ed.factory_candidates(card)
    assert folders == ["OLDSTUFF"] and loose == ["SAMPLES/LOOSE.WAV"]
    b = ed.factory_move(card, folders, loose)
    p = ed.plan([b], idx)
    assert not p.problems
    ed.apply(p, [b], card, m, bk, False)
    assert os.path.exists(card.abs("SAMPLES/FACTORY/OLDSTUFF/KICKS/kick_unused.wav"))
    assert os.path.exists(card.abs("SAMPLES/FACTORY/LOOSE.WAV"))
    assert ed.factory_candidates(card) == ([], [])


def test_refusals(env):
    card, idx, m, bk = env
    p = ed.plan([ed.EditBatch("t", trash=[KICK])], idx)
    assert p.problems and "Still used" in p.problems[0]
    p = ed.plan([ed.EditBatch("c", moves=[(KICK, "SAMPLES/FREE/MINE/PS01/MOVED_AWAY.WAV")])], idx)
    assert any("Already on the card" in x for x in p.problems)
    p = ed.plan([ed.EditBatch("o", moves=[(KICK, "KITS/X.WAV")])], idx)
    assert any("inside SAMPLES" in x for x in p.problems)


def test_changed_preset_is_refused_and_undo_keeps_resaved_preset(env):
    card, idx, m, bk = env
    b = ed.EditBatch("rename", moves=[(KICK, KICK.replace("BD_808_01", "BD_808_X"))])
    p = ed.plan([b], idx)
    kit = card.abs("KITS/PSY KIT 1.XML")
    with open(kit, "ab") as f:
        f.write(b"\n")
    with pytest.raises(RuntimeError, match="changed since"):
        ed.apply(p, [b], card, m, bk, False)
    card.scan()
    idx = build_index(card)
    p = ed.plan([b], idx)
    j = ed.apply(p, [b], card, m, bk, False)
    with open(kit, "ab") as f:                       # the Deluge saves the kit again
        f.write(b"<!-- resaved -->")
    probs = ed.undo(j, card, m)
    assert any("saved again" in x for x in probs)
    assert os.path.exists(card.abs(KICK))            # the sample still moved back
    assert ap.history()[0].undone


def test_trash_unused(env):
    card, idx, m, bk = env
    b = ed.EditBatch("t", trash=["SAMPLES/OLDSTUFF/KICKS/kick_unused.wav"])
    j = ed.apply(ed.plan([b], idx), [b], card, m, bk, False)
    assert os.path.exists(card.abs(f"DSM_TRASH/{j.id}/SAMPLES/OLDSTUFF/KICKS/kick_unused.wav"))
    ed.undo(j, card, m)
    assert os.path.exists(card.abs("SAMPLES/OLDSTUFF/KICKS/kick_unused.wav"))


def test_sort_card_by_type(env):
    """0.14: Sort the card by type — samples move to SAMPLES/DRUMS/KICK …, presets follow, factory stays."""
    from dsm.edit.sortcard import sort_batch
    card, idx, m, bk = env
    snare = "SAMPLES/PAID/MARS/808/DRUMS/SNARE/SD_808_01.WAV"
    types = {KICK: "Kick", snare: "Snare", "SAMPLES/FREE/PACKS/VOL1/PADS/LOOPED_PAD.WAV": "Pad"}
    types.update({r: "Kick" for r in card.sample_files() if r.upper().startswith(("SAMPLES/CLIPS/", "SAMPLES/RECORD/"))})
    audio = {KICK.lower(): "a1"}
    batch, note = sort_batch(card, types, audio, {"a1": "KK0007 BD 808 01"})
    got = dict(batch.moves)
    assert got[KICK] == "SAMPLES/DRUMS/KICK/KK0007_BD_808_01.WAV"           # named after its ID
    assert got[snare] == "SAMPLES/DRUMS/SNARE/SD_808_01.WAV"                # no ID: keeps its name
    assert got["SAMPLES/FREE/PACKS/VOL1/PADS/LOOPED_PAD.WAV"] == "SAMPLES/PAD/LOOPED_PAD.WAV"
    assert not any(o.upper().startswith(("SAMPLES/CLIPS/", "SAMPLES/RECORD/")) for o in got)
    p = ed.plan([batch], idx)
    missing_before = len(idx.missing)
    ed.apply(p, [batch], card, m, bk, False)
    card.scan()
    idx2 = build_index(card)
    assert idx2.uses("SAMPLES/DRUMS/KICK/KK0007_BD_808_01.WAV") and len(idx2.missing) == missing_before
    assert not os.path.isdir(card.abs("SAMPLES/PAID/MARS/808/DRUMS/SNARE"))   # emptied folders removed
    again, _ = sort_batch(card, {"SAMPLES/DRUMS/KICK/KK0007_BD_808_01.WAV": "Kick"}, {}, {})
    assert again is None                                                     # in place already


def test_junk_files_found_and_trashed(env):
    """0.16: files other programs leave (.reapeaks, .asd, Thumbs.db) are found by the health check and can be moved
    to the card trash — also beside factory samples, where they aren't locked."""
    from dsm.index.health import check, junk_files
    card, idx, m, bk = env
    for rel in ("SAMPLES/FACTORY/DRUMS/Clap/808 Clap.WAV.reapeaks", "SAMPLES/PAID/MARS/808/x.wav.asd", "KITS/Thumbs.db"):
        os.makedirs(os.path.dirname(card.abs(rel)), exist_ok=True)
        open(card.abs(rel), "wb").write(b"junk")
    card.scan()
    junk = junk_files(card)
    assert junk == ["KITS/Thumbs.db", "SAMPLES/FACTORY/DRUMS/Clap/808 Clap.WAV.reapeaks", "SAMPLES/PAID/MARS/808/x.wav.asd"]
    idx = build_index(card)
    rep = check(idx)
    assert rep.of("junk") and rep.of("junk")[0].suggestions == junk
    b = ed.EditBatch("junk", trash=junk)
    p = ed.plan([b], idx)
    assert not p.problems, p.problems
    ed.apply(p, [b], card, m, bk, False)
    card.scan()
    assert junk_files(card) == [] and not card.exists("KITS/Thumbs.db")
    assert any("reapeaks" in r for r in card.files if r.startswith("dsm_trash/"))
