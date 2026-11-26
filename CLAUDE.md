# Sample Manager

Windows GUI app (Python 3.12 + PySide6): one sample library (Studio\\Samples) for every device — for now the
Synthstrom Deluge and the Elektron Syntakt. Called Deluge Sample Manager before 0.9 (the Python package is still `dsm`).
The design is the spec PDF in Google Drive: Studio → Software → Sample Manager → Sample_Manager_Spec_v2.x.pdf (latest).

The owner (Nigel) does not code or debug. Keep the code easy for Claude to change:
plain modules, tests for every core behaviour, no clever metaprogramming.

## Layout
- `dsm/card` — card detection, volume facts, case-insensitive file map, 8.3 name resolution
- `dsm/audio` — header inspection (WAV/AIFF by hand), peaks, playback
- `dsm/index` — XML reference extraction with byte offsets, card index, health check
- `dsm/importer` — naming rules, conversion profiles, library↔card mapping, plan / apply / undo with journal
- `dsm/edit` — M4: rename / move / relink / trash of card files, with byte-exact path rewrites in every kit, synth and song; factory move
- `dsm/edit/factory.py` — the factory record (original path, new path, size, date, SHA-1 once copied) made at the factory move; saved in the library (_Sample Manager/factory-record.json) and on the card (DSM/FACTORY_RECORD.JSON); untouched copies go to Factory/Deluge/… in the library, which must stay complete (missing_from_library)
- `dsm/usb` — the Deluge's card over USB (Community Firmware 1.3 SysEx file commands): `link.py` protocol, `usbcard.py` a Card backend with a PC cache of presets and sample headers, `winmm_in.py` big-buffer MIDI input (from the bench test)
- `dsm/library` — the library (spec v2.2 section 2): Studio\Samples holds Factory (Deluge, Syntakt), Paid, Free, User,
  Backups, _Sample Manager — no copies. `layout.py` folders; `sources.py` zips read in place ("zip::member" paths),
  audio identity (hash of the audio data), PC cache for audition/send; `scanner.py` fingerprints (size + SHA-1,
  states ok/changed/missing); `db.py` SQLite at %LOCALAPPDATA%\SampleManager\library.db — user types by audio
  identity, `listed` = the by-type view (one row per sample, the copy that counts; refresh_view);
  `reorganize.py` the one-time move from 0.8 or 0.9.0 (Sources\* up a level, Samples copies to the Cleanup list,
  db.flatten_paths so nothing is re-read); `getdevice.py` Get from the Deluge
- `dsm/updater.py` — self-update from GitHub releases (same file as the Patch Manager's; repo nigeltx/sample-manager must be public;
  renamed from deluge-sample-manager on 26 Nov 2026 — GitHub redirects the old name, which 0.8.1 still uses).
  The workflow also publishes the installer as DelugeSampleManager-Setup-x.exe so 0.8.1 can update across the rename
- `dsm/ui` — PySide6 windows; no file logic here. Layout follows the Patch Manager: left = SAMPLES by type, FOLDERS (Factory / Paid / Free / User → pack), ON THE DELUGE (card folders), SMART LISTS; middle = sample list (green bold = on the card); right = details
- `tests` — `fixture.py` builds a fake card; `fake_deluge.py` answers the USB file commands from a folder; `pytest -q` must pass
- `packaging` — PyInstaller spec and Inno Setup script; `.github/workflows/build.yml` builds the installer and
  publishes a GitHub release on every push to main

## Rules
- M1–M2 were read-only; M3 adds files; M4 (current) moves, renames, relinks and trashes existing card files and rewrites preset paths.
- Every card read or write goes through the Card methods (read_bytes, write_bytes, put_file, move, stat, inspect …), never open()/os calls on card.abs(), so the reader and USB backends both work.
- Factory samples are LOCKED: nothing may rename, move, trash or write into SAMPLES/FACTORY except Initialize Factory Samples (EditBatch.factory=True); library FACTORY copies are read-only. Import offers Free and Paid only.
- Never delete a card file: removals go to DSM_TRASH. Undo restores preset files from the per-apply backup, but never over a preset the Deluge has saved again since.
- The library (Factory/Paid/Free/User) is never written except new reads (factory copies from the card, Get from
  device) and Reorganize's one-time folder moves; Backups, Archive and _Sample Manager are never scanned.
- The library keeps originals only; the card gets converted copies; `_Sample Manager/mapping.json` in the library root links them.
- When writes arrive (M3+), every write goes through the pending tray, backup, atomic write, verify, journal.
- XML is never re-serialised; only the recorded byte ranges of path values change.
- Bump `dsm/__init__.py` `__version__` and `RELEASE_NOTES.txt` for each release.
- After each release, put the Setup .exe and a source zip (`git archive`, named `Sample Manager X.Y.Z - source.zip`)
  in Google Drive at Studio → Software → Sample Manager, and move the previous version's pair into its Archive subfolder.
- Test the UI offscreen (QT_QPA_PLATFORM=offscreen) and look at screenshots before pushing.
