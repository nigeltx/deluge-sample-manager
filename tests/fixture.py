"""Builds a fake Deluge card in a temp folder: every header kind, every reference form,
mixed case, a missing sample, unused samples, loose folders."""
from __future__ import annotations

import os
import struct

import numpy as np
import soundfile as sf


def _sine(n=4410, ch=1):
    t = np.arange(n) / 44100
    x = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    return np.stack([x] * ch, axis=1)


def write_wav(path, rate=44100, subtype="PCM_24", ch=1, n=4410):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sf.write(path, _sine(n, ch), rate, subtype=subtype)


def write_extensible(path):
    """A 24-bit stereo WAV with a WAVE_FORMAT_EXTENSIBLE header, as many DAWs write."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames = 1000
    data = b"\x00" * (frames * 6)
    fmt = struct.pack("<HHIIHH", 0xFFFE, 2, 48000, 48000 * 6, 6, 24)
    fmt += struct.pack("<HHI", 22, 24, 3) + b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def add_smpl(path, start, end, root=60):
    """Append a smpl chunk with one loop to an existing WAV."""
    loop = struct.pack("<IIIIII", 0, 0, start, end, 0, 0)
    smpl = struct.pack("<IIIIIIIII", 0, 0, 22676, root, 0, 0, 0, 1, 0) + loop
    with open(path, "r+b") as f:
        f.seek(0, 2)
        f.write(b"smpl" + struct.pack("<I", len(smpl)) + smpl)
        size = f.tell() - 8
        f.seek(4)
        f.write(struct.pack("<I", size))


KIT_ATTR = """<?xml version="1.0" encoding="UTF-8"?>
<kit firmwareVersion="c1.3.0" earliestCompatibleFirmware="4.1.0">
\t<soundSources>
\t\t<sound name="KICK">
\t\t\t<osc1 type="sample" loopMode="1" fileName="SAMPLES/PAID/MARS/808/DRUMS/KICK/BD_808_01.WAV">
\t\t\t\t<zone startSamplePos="0" endSamplePos="4410" />
\t\t\t</osc1>
\t\t</sound>
\t\t<sound name="SNARE">
\t\t\t<osc1 type="sample" fileName="samples/paid/mars/808/drums/snare/sd_808_01.wav" />
\t\t</sound>
\t\t<sound name="GONE">
\t\t\t<osc1 type="sample" fileName="SAMPLES/PAID/MARS/808/DRUMS/PERC/MOVED_AWAY.WAV" />
\t\t</sound>
\t\t<sound name="EMPTY">
\t\t\t<osc1 type="sample" fileName="" />
\t\t</sound>
\t</soundSources>
</kit>
"""

SYNTH_ELEM = """<?xml version="1.0" encoding="UTF-8"?>
<sound>
\t<osc1>
\t\t<type>sample</type>
\t\t<fileName>SAMPLES/FACTORY/SYNTHS/PAD &amp; CHOIR.WAV</fileName>
\t</osc1>
</sound>
"""

SONG = """<?xml version="1.0" encoding="UTF-8"?>
<song>
\t<instruments>
\t\t<audioTrack name="AUDIO1" />
\t\t<kit presetName="PSY KIT 1">
\t\t\t<soundSources>
\t\t\t\t<sound name="KICK">
\t\t\t\t\t<osc1 type="sample" fileName="SAMPLES/PAID/MARS/808/DRUMS/KICK/BD_808_01.WAV" />
\t\t\t\t</sound>
\t\t\t</soundSources>
\t\t</kit>
\t</instruments>
\t<sessionClips>
\t\t<audioClip trackName="AUDIO1" filePath="SAMPLES/CLIPS/REC00001.WAV" />
\t</sessionClips>
</song>
"""


def build_card(root: str) -> str:
    S = os.path.join(root, "SAMPLES")
    write_wav(os.path.join(S, "PAID/MARS/808/DRUMS/KICK/BD_808_01.WAV"))
    write_wav(os.path.join(S, "PAID/MARS/808/DRUMS/SNARE/SD_808_01.WAV"), subtype="PCM_16")
    write_wav(os.path.join(S, "PAID/MARS/808/DRUMS/PERC/MOVED_AWAY_NOT.WAV"))
    write_wav(os.path.join(S, "FREE/MINE/PS01/MOVED_AWAY.WAV"))           # same name as the missing one
    write_wav(os.path.join(S, "FACTORY/SYNTHS/PAD & CHOIR.WAV"), ch=2)
    write_extensible(os.path.join(S, "FREE/PACKS/VOL1/LOOPS/ext header loop.wav"))
    p = os.path.join(S, "FREE/PACKS/VOL1/PADS/LOOPED_PAD.WAV")
    write_wav(p, rate=48000, n=9600)
    add_smpl(p, 1000, 9000, root=57)
    write_wav(os.path.join(S, "CLIPS/REC00001.WAV"))
    write_wav(os.path.join(S, "CLIPS/TEMP/REC00002.WAV"))
    write_wav(os.path.join(S, "RECORD/REC00010.WAV"))
    write_wav(os.path.join(S, "OLDSTUFF/KICKS/kick_unused.wav"))
    write_wav(os.path.join(S, "LOOSE.WAV"))
    os.makedirs(os.path.join(S, "FREE/PACKS/VOL1/MP3"), exist_ok=True)
    sf.write(os.path.join(S, "FREE/PACKS/VOL1/MP3/vox.flac"), _sine(), 44100)
    for d, name, text in [("KITS", "PSY KIT 1.XML", KIT_ATTR), ("SYNTHS", "CHOIR PAD.XML", SYNTH_ELEM),
                          ("SONGS", "PS01 FRACTAL DAWN.XML", SONG)]:
        os.makedirs(os.path.join(root, d), exist_ok=True)
        with open(os.path.join(root, d, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    with open(os.path.join(root, "KITS", "BROKEN.XML"), "w") as f:
        f.write("<kit><soundSources>")
    return root
