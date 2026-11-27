"""0.12 Syntakt: the sample format from the bench test, matching to the library, the Twin Shot check, and Send /
Remove / Undo with backups — against a fake Syntakt that behaves like the real one did."""
import struct
import zlib

import numpy as np
import pytest

from dsm.syntakt import device as dev
from dsm.syntakt import proto
from tests.fake_syntakt import FakeSyntakt

# The first 43 bytes and the footer of slot 42 (SD 1978) as the real Syntakt returned it (27 Nov 2026).
REAL_HEAD = bytes.fromhex("ac11d30302000800 0d30303934000000 08ffffffff000000 2a00002d40000c53 414d500000000000 001680"
                          .replace(" ", ""))
REAL_FOOT_TAIL = bytes.fromhex("00002d40aaa1daaa")


def tone(frames, hz=440):
    t = np.arange(frames)
    return (np.sin(2 * np.pi * hz * t / 48000) * 9000).astype(">i2").tobytes()


def test_build_matches_the_real_layout():
    d = proto.build_sample(42, "SD 1978", tone(5760))
    assert d[:43] == REAL_HEAD
    assert d[-8:] == REAL_FOOT_TAIL
    info = proto.parse_sample(d)
    assert info["ok"] and info["slot"] == 42 and info["name"] == "SD 1978" and info["frames"] == 5760
    assert struct.unpack(">I", d[-12:-8])[0] == zlib.crc32(d[31:-12], 0xFFFFFFFF) & 0xFFFFFFFF
    assert proto.clean_name("Fun_Hat — long name here") == "FUN_HAT LONG NAM"


def test_convert_any_wav(tmp_path):
    import soundfile as sf
    p = str(tmp_path / "s.wav")
    sf.write(p, np.zeros((44100, 2)), 44100, subtype="PCM_24")
    audio, notes = proto.to_syntakt_audio(p)
    assert len(audio) == 2 * 48000
    assert "stereo folded to mono" in notes and "44.1 kHz → 48 kHz" in notes and "24-bit → 16-bit" in notes


def factory_rows():
    return [{"rel": "Factory/Syntakt/Twin Shot/SD 1978.wav", "name": "SD 1978.wav", "frames": 5292, "rate": 44100},
            {"rel": "Factory/Syntakt/Twin Shot/TONE Drift.wav", "name": "TONE Drift.wav", "frames": 220377, "rate": 44100}]


def test_match_and_twin_check():
    slots = [dev.Slot(42, "SD 1978", 64 + 2 * 5760), dev.Slot(53, "TONE DRIFT", 64 + 2 * 239866)]
    dev.match_slots(slots, factory_rows(), {"slots": {}})
    assert [s.how for s in slots] == ["factory (Twin Shot)"] * 2
    assert dev.twin_check(slots, factory_rows())["text"] == "Twin Shot pack complete (2 samples)"
    slots = [dev.Slot(42, "SD 1978", 64 + 2 * 5760), dev.Slot(7, "MY KICK", 64 + 2 * 100)]
    dev.match_slots(slots, factory_rows(), {"slots": {"7": {"library": "Paid/A/My Kick.wav", "name": "MY KICK",
                                                            "size": 264}}})
    assert slots[1].how == "sent by the app" and slots[1].match == "Paid/A/My Kick.wav"
    c = dev.twin_check(slots, factory_rows())
    assert not c["complete"] and c["missing"] == ["TONE Drift"] and c["added"] == ["7 MY KICK"]
    assert dev.locked(slots[0]) and not dev.locked(slots[1])
    assert dev.locked(dev.Slot(9, "SOMETHING", 100)) == "a sample the app did not put there"


def connected(slots):
    fake = FakeSyntakt(slots)
    s = dev.Syntakt(proto.Elektron(fake, timeout=1.0), fake, cache_dir="")
    return fake, s


def test_send_remove_undo(tmp_path):
    factory = proto.build_sample(1, "BD BAUTA", tone(2000))
    fake, s = connected({1: factory})
    s.cache_dir = str(tmp_path / "cache")
    assert s.el.ping()["id"] == 30 and s.el.version() == "1.41"
    s.refresh()
    dev.match_slots(s.slots, [{"rel": "Factory/Syntakt/BD Bauta.wav", "name": "BD Bauta.wav", "frames": 1838,
                               "rate": 44100}], {"slots": {}})
    assert s.slots[0].how.startswith("factory")
    new = proto.build_sample(2, "MY KICK", tone(30000))           # 30,000 frames: several 8 KB blocks
    with pytest.raises(proto.SyntaktError, match="factory"):
        s.send(1, new, str(tmp_path / "bk"))                       # locked
    assert s.send(2, new, str(tmp_path / "bk")) == ""             # empty slot: nothing to back up
    assert fake.slots[2] == new
    s.refresh()
    with pytest.raises(proto.SyntaktError, match="did not put"):
        s.remove(2, str(tmp_path / "bk"))                          # not in the record yet
    s.slot(2).how = "sent by the app"
    bk = s.remove(2, str(tmp_path / "bk"))
    assert 2 not in fake.slots and open(bk, "rb").read() == new
    s.put_back(2, bk)                                              # undo the remove
    assert fake.slots[2] == new
    s.put_back(2, "")                                              # undo the send: the slot was empty
    assert 2 not in fake.slots and fake.slots[1] == factory
    assert open(s.wav(s.slot(1) or dev.Slot(1, "BD BAUTA", len(factory) - 43)), "rb").read(4) == b"RIFF"


def test_footer_must_go_alone():
    fake, s = connected({})
    data = proto.build_sample(3, "X", tone(100))
    jid_open = s.el.cmd(0x57, struct.pack(">I", len(data)) + b"/samples/3\0")
    jid = struct.unpack(">I", jid_open[6:10])[0]
    s.el.cmd(0x58, struct.pack(">IIII", jid, 0, 0, len(data)) + data)      # all in one block, as 1.1 did
    r = s.el.cmd(0x59, struct.pack(">II", jid, len(data)))
    assert r[5] == 0 and b"Footer" in r                                     # the fake refuses like the real one


def test_journal(tmp_path):
    d = str(tmp_path)
    dev.add_journal(d, {"action": "send", "slot": 2, "backup": ""})
    dev.add_journal(d, {"action": "remove", "slot": 2, "backup": "x.bin"})
    assert dev.pop_journal(d)["action"] == "remove"
    assert dev.pop_journal(d)["slot"] == 2 and dev.pop_journal(d) is None


def test_replace_needs_consent_and_factory_never_removed(tmp_path):
    factory = proto.build_sample(1, "BD BAUTA", tone(2000))
    mine = proto.build_sample(2, "HALLELUJAH", tone(4000))
    fake, s = connected({1: factory, 2: mine})
    s.refresh()
    s.slot(1).how = "factory (Twin Shot)"
    new = proto.build_sample(1, "FX0040 TRANSIENT", tone(3000))
    with pytest.raises(proto.SyntaktError):
        s.send(1, new, str(tmp_path / "bk"))                       # not without replace=True
    bk = s.send(1, new, str(tmp_path / "bk"), replace=True)        # the user agreed
    assert fake.slots[1] == new and open(bk, "rb").read() == factory
    with pytest.raises(proto.SyntaktError):
        s.remove(2, str(tmp_path / "bk"))                          # yours: only when confirmed
    s.remove(2, str(tmp_path / "bk"), confirmed=True)
    assert 2 not in fake.slots
    s.refresh()
    s.slot(1).how = "factory (Twin Shot)"
    with pytest.raises(proto.SyntaktError, match="factory"):
        s.remove(1, str(tmp_path / "bk"), confirmed=True)          # never a factory sample


def test_your_samples_match_by_name():
    slots = [dev.Slot(63, "HALLELUJAH", 64 + 2 * 48000)]
    dev.match_slots(slots, factory_rows(), {"slots": {}},
                    [{"rel": "User/Syntakt/Hallelujah.wav", "name": "Hallelujah.wav", "frames": 44100, "rate": 44100}])
    assert slots[0].match == "User/Syntakt/Hallelujah.wav" and slots[0].how == "same name and length"
