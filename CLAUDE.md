# Deluge Sample Manager

Windows GUI app (Python 3.12 + PySide6) for managing the samples on a Synthstrom Deluge SD card.
The design is the spec PDF in Google Drive: Studio → Software → Deluge_Sample_Manager_Spec_v1.1.pdf.

The owner (Nigel) does not code or debug. Keep the code easy for Claude to change:
plain modules, tests for every core behaviour, no clever metaprogramming.

## Layout
- `dsm/card` — card detection, volume facts, case-insensitive file map, 8.3 name resolution
- `dsm/audio` — header inspection (WAV/AIFF by hand), peaks, playback
- `dsm/index` — XML reference extraction with byte offsets, card index, health check
- `dsm/importer` — naming rules, conversion profiles, library↔card mapping, plan / apply / undo with journal
- `dsm/edit` — M4: rename / move / relink / trash of card files, with byte-exact path rewrites in every kit, synth and song; factory move
- `dsm/edit/factory.py` — the factory record (original path, new path, size, date, SHA-1 once copied) made at the factory move; saved in the library (_DSM/factory-record.json) and on the card (DSM/FACTORY_RECORD.JSON); untouched copies go to LIBRARY/FACTORY/…
- `dsm/usb` — the Deluge's card over USB (Community Firmware 1.3 SysEx file commands): `link.py` protocol, `usbcard.py` a Card backend with a PC cache of presets and sample headers, `winmm_in.py` big-buffer MIDI input (from the bench test)
- `dsm/ui` — PySide6 windows; no file logic here
- `tests` — `fixture.py` builds a fake card; `fake_deluge.py` answers the USB file commands from a folder; `pytest -q` must pass
- `packaging` — PyInstaller spec and Inno Setup script; `.github/workflows/build.yml` builds the installer and
  publishes a GitHub release on every push to main

## Rules
- M1–M2 were read-only; M3 adds files; M4 (current) moves, renames, relinks and trashes existing card files and rewrites preset paths.
- Every card read or write goes through the Card methods (read_bytes, write_bytes, put_file, move, stat, inspect …), never open()/os calls on card.abs(), so the reader and USB backends both work.
- Never delete a card file: removals go to DSM_TRASH. Undo restores preset files from the per-apply backup, but never over a preset the Deluge has saved again since.
- The library keeps originals only; the card gets converted copies; `_DSM/mapping.json` in the library root links them.
- When writes arrive (M3+), every write goes through the pending tray, backup, atomic write, verify, journal.
- XML is never re-serialised; only the recorded byte ranges of path values change.
- Bump `dsm/__init__.py` `__version__` and `RELEASE_NOTES.txt` for each release.
- After each release, put the Setup .exe and a source zip (`git archive`, named `Deluge Sample Manager X.Y.Z - source.zip`)
  in Google Drive at Studio → Software → Deluge Sample Manager, and move the previous version's pair into its Archive subfolder.
- Test the UI offscreen (QT_QPA_PLATFORM=offscreen) and look at screenshots before pushing.
