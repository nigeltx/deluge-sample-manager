"""Health check: everything wrong or worth knowing about a card, as a list of findings.
Read-only: it only finds things. The health dialog offers the fixes (relink, factory move, files to the card trash),
which go through Pending like every other change."""
from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from dsm.audio.inspect import AUDIO_EXTS, inspect
from dsm.card.card import DELUGE_OWN_SAMPLE_DIRS, SOURCE_DIRS
from dsm.index.index import CardIndex, basename_map, unused_samples

NAME_OK = re.compile(r"^[A-Za-z0-9 _\-#()]+$")
MAX_NAME = 40
MAX_PATH = 120


@dataclass
class Finding:
    kind: str           # missing, unused, convert, name, temp, layout, xml, junk (0.16)
    title: str
    detail: str = ""
    path: str = ""      # the card path this is about
    suggestions: list[str] = field(default_factory=list)


@dataclass
class HealthReport:
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def of(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]


def _area(rel: str) -> str:
    """'SAMPLES/PAID/VENDOR/PACK/...' -> 'PAID/VENDOR/PACK'; Deluge's own folders by name."""
    parts = rel.split("/")[1:-1]
    if not parts:
        return "SAMPLES (top level)"
    if parts[0].upper() in SOURCE_DIRS:
        return "/".join(parts[:3])
    return parts[0]


# Files other programs leave beside samples (0.16): waveform and analysis caches, Windows and Mac folder files.
# The Deluge never reads them. Moved to the card trash on request, never deleted.
JUNK_EXTS = {".reapeaks", ".reapindex", ".asd", ".pk", ".pkf", ".sfk", ".sfap0", ".gpk", ".peak", ".ovw", ".mrk"}
JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db"}


def junk_files(card) -> list[str]:
    """Card files left by other programs (REAPER .reapeaks, Ableton .asd, Thumbs.db …), outside the card trash and
    the app's DSM folder."""
    out = []
    for low, rel in card.files.items():
        if low.startswith(("dsm_trash/", "samples/dsm_trash/", "dsm/")):
            continue
        name = low.rsplit("/", 1)[-1]
        if name in JUNK_NAMES or os.path.splitext(name)[1] in JUNK_EXTS:
            out.append(rel)
    return sorted(out, key=str.lower)


def check(idx: CardIndex, library_files: list[str] | None = None, progress=None) -> HealthReport:
    card = idx.card
    rep = HealthReport()
    F = rep.findings.append
    junk = junk_files(card)
    if junk:
        F(Finding("junk", f"{len(junk)} file{'s' if len(junk) != 1 else ''} left by other programs",
                  "Not samples: waveform caches (REAPER .reapeaks, Ableton .asd …) and folder files (Thumbs.db, "
                  "desktop.ini …). The Deluge never reads them. Press Move them to the card trash.\n\n" +
                  "\n".join(junk), "", list(junk)))
    lib_names = basename_map(library_files or [])
    card_names = basename_map(card.sample_files())

    # missing — one finding per distinct missing path, listing the presets that use it
    by_path = defaultdict(list)
    for r in idx.missing:
        by_path[r.path.lower()].append(r)
    for refs in by_path.values():
        r0 = refs[0]
        base = os.path.basename(r0.path).lower()
        sugg = [f"On the card: {p}" for p in card_names.get(base, [])]
        sugg += [f"In the library: {p}" for p in lib_names.get(base, [])[:5]]
        users = sorted({r.xml for r in refs})
        F(Finding("missing", r0.path, f"Used by {len(users)}: " + ", ".join(users[:6]) + (" …" if len(users) > 6 else ""),
                  r0.path, sugg))

    # unused, grouped by area
    groups = defaultdict(list)
    for s in unused_samples(idx):
        groups[_area(s)].append(s)
    for area, files in sorted(groups.items()):
        own = area.split("/")[0].upper() in DELUGE_OWN_SAMPLE_DIRS
        note = " (the Deluge's own folder — often unused on purpose)" if own else ""
        F(Finding("unused", f"{area}: {len(files)} unused{note}", "\n".join(files[:200]), f"SAMPLES/{area}"))

    # formats and names
    samples = card.sample_files()
    for i, s in enumerate(samples):
        if progress and i % 25 == 0:
            progress(i, len(samples), s)
        info = card.inspect(s)
        bad = [p[1:] for p in info.problems if p.startswith("!")]
        if info.error or bad:
            F(Finding("convert", s, info.error or "; ".join(bad), s))
        name = os.path.splitext(os.path.basename(s))[0]
        factory = s.upper().startswith("SAMPLES/FACTORY/")
        if not factory:
            why = []
            if not NAME_OK.match(name):
                why.append("characters outside A–Z 0–9 space _ - # ( )")
            if len(name) > MAX_NAME:
                why.append(f"name longer than {MAX_NAME}")
            if len(s) > MAX_PATH:
                why.append(f"path longer than {MAX_PATH}")
            if why:
                F(Finding("name", s, "; ".join(why), s))
        if s.upper().startswith("SAMPLES/CLIPS/TEMP/"):
            F(Finding("temp", s, "Unsaved audio-clip take; safe to clear once the song is saved", s))
    if progress:
        progress(len(samples), len(samples), "")

    # layout (0.14): samples are sorted by type — SAMPLES/DRUMS/KICK, SAMPLES/BASS … — beside FACTORY and the
    # Deluge's own folders. Anything else directly under SAMPLES is the old layout or put there by hand.
    from dsm.importer.naming import TYPE_TOPS
    known = {"FACTORY", "DSM_TRASH"} | set(DELUGE_OWN_SAMPLE_DIRS) | TYPE_TOPS
    tops = sorted({d.split("/")[1] for d in card.dirs if d.upper().startswith("SAMPLES/") and len(d.split("/")) >= 2})
    loose = [t for t in tops if t.upper() not in known]
    if loose:
        F(Finding("layout", f"{len(loose)} folder{'s' if len(loose) != 1 else ''} outside the type folders",
                  "Right-click TARGETS › DELUGE › Sort the card by type… to move their samples into SAMPLES/DRUMS/KICK, "
                  "SAMPLES/BASS and so on:\n" + "\n".join(loose), "SAMPLES"))
    loose_files = [p for p in samples if p.count("/") == 1]
    if loose_files:
        F(Finding("layout", f"{len(loose_files)} sample{'s' if len(loose_files) != 1 else ''} loose directly in SAMPLES", "\n".join(loose_files[:100]), "SAMPLES"))

    for d in idx.docs:
        if d.error:
            F(Finding("xml", d.rel, d.error, d.rel))

    for k in ("missing", "unused", "convert", "name", "temp", "layout", "xml"):
        rep.counts[k] = len(rep.of(k))
    rep.counts["unused_files"] = sum(len(v) for v in groups.values())
    return rep


def library_files(root: str) -> list[str]:
    """Every audio file under the library root (names only; does not open files, so a
    Google Drive streamed folder is not downloaded)."""
    out = []
    if not root or not os.path.isdir(root):
        return out
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.startswith(".")]
        for fn in fns:
            if os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
                out.append(os.path.join(dp, fn))
    return out
