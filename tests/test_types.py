"""Sample types: the best guess from names, and the user's own choice winning over it."""
from dsm.library import types as st
from dsm.library.db import LibraryDB


def g(rel, secs=None):
    return st.guess(rel, secs)[:2]


def test_guess_from_file_name_and_folders():
    assert g("Paid/V/P/HESOS_Kick_Oneshot_222.wav") == (["Kick"], False)
    assert g("Paid/V/P/Kicks/Bright 01.wav") == (["Kick"], False)              # from the folder
    assert g("Paid/V/P/Snares/BD 01.wav") == (["Kick"], False)                 # the file name wins
    assert g("Paid/V/P/Drums/Smile_OHat.wav") == (["Open hat"], False)
    assert g("Paid/V/P/HiHatClosed 3.wav") == (["Closed hat"], False)
    assert g("Paid/V/P/CH_02.wav") == (["Closed hat"], False)
    assert g("Paid/V/P/808 Kick.wav") == (["Kick"], False)
    assert g("Paid/V/P/808 Bass C.wav") == (["Bass"], False)
    assert g("Paid/V/P/Kick & Snare Loop.wav") == (["Kick", "Snare"], True)
    assert g("Paid/V/Loops/Groove 120 BPM.wav") == (["Other drums"], True)
    assert g("Free/drum-and-bass-pack/Synare_79.wav") == (["Percussion"], False)   # a genre is not a type
    assert g("Factory/Artists/K/3.wav") == ([st.UNKNOWN], False)


def test_hats_by_length():
    assert g("Paid/V/P/Hat_02.wav", 0.12) == (["Closed hat"], False)
    assert g("Paid/V/P/Hat_02.wav", 1.4) == (["Open hat"], False)
    assert g("Paid/V/P/Hat_02.wav", 0.45) == (["Hat"], False)
    assert g("Paid/V/P/Hat_02.wav") == (["Hat"], False)


def _row(rel, frames=44100, audio=None):
    return {"rel": rel, "area": "Sources", "source": "Paid", "level1": "V", "level2": "P", "name": rel.rsplit("/", 1)[1],
            "file": rel, "audio": audio or rel, "size": 1, "mtime": 0, "frames": frames, "rate": 44100, "plays": 1,
            "native": 1}


def test_db_types_and_user_override(tmp_path):
    db = LibraryDB(str(tmp_path / "l.db"))
    db.upsert([_row("Sources/Paid/V/P/Kick 1.wav"), _row("Sources/Paid/V/P/Hat 1.wav", 4000),
               _row("Sources/Paid/V/P/Perc Loop.wav")])
    assert db.refresh_types() == 3
    assert db.refresh_types() == 0                          # nothing new to work out
    assert [r["name"] for r in db.query(stype="Kick")] == ["Kick 1.wav"]
    assert len(db.query(stype=st.DRUMS, loop=True)) == 1
    k = "Sources/Paid/V/P/Kick 1.wav"
    assert db.types_for(k, k) == (["Kick"], False)
    db.set_user_type([k], ["Snare"], False)                 # by audio identity
    assert db.types_for(k, k) == (["Snare"], False)
    r = db.query(stype="Snare")[0]
    assert r["type_by_user"] == 1
    db.upsert([_row(k, 9999)])                              # the file changed: the guess is redone …
    db.refresh_types()
    assert db.query(stype="Snare")[0]["name"] == "Kick 1.wav"   # … but the user's choice stays
    db.clear_user_type([k])
    assert db.types_for(k, k) == (["Kick"], False)


def test_drone_type():
    from dsm.importer.naming import classify
    assert g("Paid/ModeAudio - Eclipse [WAV]/Atomspheric Drones/Following_Drone.wav") == (["Drone"], False)
    assert g("Paid/ModeAudio - Eclipse [WAV]/Dark Drones/Untitled 3.wav") == (["Drone"], False)    # from the folder
    assert g("Paid/V/P/Dark Pad 01.wav") == (["Pad"], False)
    assert "Drone" in st.MUSIC
    assert classify(["Following_Drone.wav"]) == ("DRONES", "")
