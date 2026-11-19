# Deluge Sample Manager

Windows GUI app (Python 3.12 + PySide6) for managing the samples on a Synthstrom Deluge SD card.
The design is the spec PDF in Google Drive: Studio → Software → Deluge_Sample_Manager_Spec_v1.1.pdf.

The owner (Nigel) does not code or debug. Keep the code easy for Claude to change:
plain modules, tests for every core behaviour, no clever metaprogramming.

## Layout
- `dsm/card` — card detection, volume facts, case-insensitive file map, 8.3 name resolution
- `dsm/audio` — header inspection (WAV/AIFF by hand), peaks, playback
- `dsm/index` — XML reference extraction with byte offsets, card index, health check
- `dsm/ui` — PySide6 windows; no file logic here
- `tests` — `fixture.py` builds a fake card; `pytest -q` must pass
- `packaging` — PyInstaller spec and Inno Setup script; `.github/workflows/build.yml` builds the installer and
  publishes a GitHub release on every push to main

## Rules
- Milestones M1–M2 are read-only: nothing may write to the card.
- When writes arrive (M3+), every write goes through the pending tray, backup, atomic write, verify, journal.
- XML is never re-serialised; only the recorded byte ranges of path values change.
- Bump `dsm/__init__.py` `__version__` and `RELEASE_NOTES.txt` for each release.
- Test the UI offscreen (QT_QPA_PLATFORM=offscreen) and look at screenshots before pushing.
