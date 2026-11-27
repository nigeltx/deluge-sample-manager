"""0.10 duplicates: same audio on the card found, tidied through a normal edit, and undone; library groups."""
import os

from dsm.edit import duplicates as dup
from dsm.edit import edits as ed
from dsm.importer.mapping import Link
from dsm.index.index import build_index
from tests.fixture import write_wav
from tests.test_edit import KICK, env, snapshot  # noqa: F401  (env is a fixture)


def test_card_duplicates_found_tidied_and_undone(env):
    card, idx, m, bk = env
    groups = dup.card_groups(card, idx)
    g = next(g for g in groups if KICK in g.files)
    assert g.keep == KICK                                    # used by the most presets
    assert len(g.files) >= 3
    assert all(not f.upper().startswith("SAMPLES/FACTORY/") for f in g.extras)
    stereo = [x for x in groups if "SAMPLES/FACTORY/SYNTHS/PAD & CHOIR.WAV" in x.files]
    assert not stereo                                         # the stereo pad has no twin
    extra = next(f for f in g.extras)
    m.add(Link(card=extra, library="Free/Mine/extra.wav"))
    used_extras = [f for f in g.extras if idx.uses(f)]
    before = snapshot(card)
    batch = dup.tidy([g])
    p = ed.plan([batch], idx)
    assert not p.problems, p.problems
    j = ed.apply(p, [batch], card, m, bk, True)
    for f in g.extras:
        assert not os.path.exists(card.abs(f))
    idx2 = build_index(card)
    for f in used_extras:
        assert not idx2.uses(f)
    assert not idx2.missing or all(r.path not in g.extras for r in idx2.missing)
    assert m.for_card(KICK).library == "Free/Mine/extra.wav"     # the library sample follows to the kept copy
    assert ed.undo(j, card, m) == []
    assert snapshot(card) == before
    assert m.for_card(extra).library == "Free/Mine/extra.wav"


def test_factory_copies_are_never_removed(env):
    card, idx, m, bk = env
    twin = "SAMPLES/FREE/MINE/PAD COPY.WAV"
    write_wav(card.abs("SAMPLES/FACTORY/SYNTHS/PAD & CHOIR.WAV").replace("PAD & CHOIR", "PAD COPY")
              .replace("FACTORY" + os.sep + "SYNTHS", "FREE" + os.sep + "MINE"), ch=2)
    card.scan()
    idx = build_index(card)
    g = next(g for g in dup.card_groups(card, idx) if twin in g.files)
    assert g.keep == "SAMPLES/FACTORY/SYNTHS/PAD & CHOIR.WAV"
    assert g.extras == [twin]
    bad = ed.EditBatch("x", repoints=[(g.keep, twin)], trash=[g.keep])
    assert any("locked" in x for x in ed.plan([bad], idx).problems)


def test_library_duplicate_groups(tmp_path):
    from dsm.library.db import LibraryDB
    db = LibraryDB(str(tmp_path / "l.db"))
    rows = [{"rel": r, "area": "Library", "source": r.split("/")[0], "name": r.rsplit("/", 1)[1], "file": r,
             "audio": a, "size": 1, "mtime": 0} for r, a in
            [("Paid/A/Kick.wav", "x"), ("Free/B/Kick copy.wav", "x"), ("Paid/A.zip::Kick.wav", "x"),
             ("Paid/A/Snare.wav", "y")]]
    db.upsert(rows)
    assert db.duplicate_audios() == {"x": 3}
    assert sorted(r["rel"] for r in db.duplicate_rows()) == ["Free/B/Kick copy.wav", "Paid/A.zip::Kick.wav",
                                                            "Paid/A/Kick.wav"]
