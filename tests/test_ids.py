"""0.11 sample IDs: HO0046 Fun Hat — by audio, per-type numbers never reused, card renames through an edit."""
from dsm.edit import edits as ed
from dsm.edit.idnames import rename_batch
from dsm.library import ids
from dsm.library.db import LibraryDB
from tests.test_edit import KICK, env  # noqa: F401


def test_names_and_codes():
    assert ids.tidy_name("HESOS_Kick_Oneshot_222.wav") == "HESOS Kick Oneshot 222"
    assert ids.tidy_name("Paid/A/HO0046_FUN_HAT.WAV") == "FUN HAT"          # an old ID is not doubled
    assert ids.code_for(["Open hat"]) == "HO" and ids.code_for(["Hat"]) == "HH" and ids.code_for([]) == "UN"
    assert ids.label("HO", 46, "Fun Hat") == "HO0046 Fun Hat"


def _row(rel, audio, frames=44100):
    return {"rel": rel, "area": "Library", "source": rel.split("/")[0], "level1": "V", "name": rel.rsplit("/", 1)[1],
            "file": rel, "audio": audio, "size": 1, "mtime": 0, "frames": frames, "rate": 44100}


def test_ids_assigned_kept_and_renumbered(tmp_path):
    db = LibraryDB(str(tmp_path / "l.db"))
    db.upsert([_row("Paid/V/Kick_B.wav", "b"), _row("Paid/V/Kick_A.wav", "a"), _row("Free/V/Kick A copy.wav", "a"),
               _row("Paid/V/Fun_Hat.wav", "h", frames=40000)])
    db.refresh_types()
    db.refresh_view()
    lab = db.id_labels()
    assert lab["a"] == "KK0001 Kick A" and lab["b"] == "KK0002 Kick B"       # one ID per sound, copies share it
    assert lab["h"] == "HO0001 Fun Hat"                                        # long hat → open
    assert db.labels_by_rel()["Free/V/Kick A copy.wav"] == "KK0001 Kick A"
    assert [r["sid"] for r in db.query(text="kk0002")] == ["KK0002"]          # searchable by ID
    db.refresh_view()
    assert db.id_labels() == lab                                               # stable
    db.set_user_type(["a"], ["Snare"], False)
    db.refresh_view()
    assert db.id_labels()["a"] == "SN0001 Kick A"                              # new type, new ID
    db.clear_user_type(["a"])
    db.refresh_view()
    assert db.id_labels()["a"] == "KK0003 Kick A"                              # KK0001 is never reused
    db.upsert([_row("Paid/V/Kick_C.wav", "c")])
    db.refresh_types()
    db.refresh_view()
    assert db.id_labels()["c"] == "KK0004 Kick C"


def test_rename_card_samples_to_ids(env):
    card, idx, m, bk = env
    other = "SAMPLES/PAID/MARS/808/DRUMS/SNARE/SD_808_01.WAV"
    factory = "SAMPLES/FACTORY/SYNTHS/PAD & CHOIR.WAV"
    card_audio = {KICK.lower(): "k", other.lower(): "s", factory.lower(): "p"}
    labels = {"k": "KK0007 BD 808 01", "s": "SN0003 SD 808 01", "p": "PD0001 Pad"}
    batch, note = rename_batch(card, [KICK, other, factory, "SAMPLES/LOOSE.WAV"], card_audio, labels)
    assert dict(batch.moves) == {KICK: "SAMPLES/PAID/MARS/808/DRUMS/KICK/KK0007_BD_808_01.WAV",
                                 other: "SAMPLES/PAID/MARS/808/DRUMS/SNARE/SN0003_SD_808_01.WAV"}
    assert "factory" in note and "no copy in the library" in note
    p = ed.plan([batch], idx)
    assert not p.problems
    ed.apply(p, [batch], card, m, bk, True)
    assert card.exists("SAMPLES/PAID/MARS/808/DRUMS/KICK/KK0007_BD_808_01.WAV")
    again, _ = rename_batch(card, ["SAMPLES/PAID/MARS/808/DRUMS/KICK/KK0007_BD_808_01.WAV"],
                            {"samples/paid/mars/808/drums/kick/kk0007_bd_808_01.wav": "k"}, labels)
    assert again is None                                                       # already named
