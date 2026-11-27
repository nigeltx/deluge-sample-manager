"""The Syntakt in the main window (0.12): TARGETS › SYNTAKT by type, audition, the Twin Shot check, Send, Remove,
Undo, Back up and Get from Syntakt. The device logic is in dsm/syntakt; this is only the window's side.

Unlike the Deluge's card, a Syntakt change is not queued in the pending tray: each Send or Remove says exactly what
it will do and asks first, saves the slot it changes to Backups › Syntakt, reads the result back, and goes in the
Syntakt journal so Undo can put it back."""
from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QTreeWidgetItem

from dsm import APP_NAME
from dsm.audio.inspect import AudioInfo
from dsm.library import layout
from dsm.library import sources as lsrc
from dsm.library import types as stypes
from dsm.settings import app_dir
from dsm.syntakt import device as sdev
from dsm.syntakt import proto
from dsm.ui.widgets import Job, run_job

log = logging.getLogger("dsm.syntakt")
RESTART = "Restart the Syntakt before using the changed samples — it reads its samples when it starts."


class SyntaktMixin:
    # ---------------------------------------------------------------- state and connection
    def _syn_init(self):
        self.syn = None                  # sdev.Syntakt while connected
        self.syn_state = "not looked for yet"
        self.syn_check = {}
        self.syn_types = {}              # slot → type list
        self.on_syntakt = {}             # audio identity → slot number
        self.syn_audio = {}              # slot → audio identity of the library sample it holds
        self._syn_busy = False

    def _syn_connect(self, quiet=True):
        """Look for the Syntakt on USB and read its sample list (a second or two). Quietly does nothing if absent."""
        if self._syn_busy:
            return
        try:
            ins, outs = sdev.ports()
        except Exception as e:  # noqa: BLE001
            ins, outs = [], []
            log.info("No MIDI for the Syntakt: %s", e)
        if not ins or not outs:
            self._syn_close()
            self.syn_state = "not connected — plug it in by USB and press Refresh"
            self._build_library_root()
            if not quiet:
                QMessageBox.information(self, APP_NAME, "No Syntakt found. Plug it into the PC by USB, turn it on, "
                                                        "then press Refresh.")
            return
        self._syn_close()
        self._syn_busy = True
        cache = os.path.join(app_dir(), "syntakt-cache")

        def work():
            d = sdev.Syntakt.connect(cache)
            d.refresh()
            return d

        def done(d):
            self._syn_busy = False
            self.syn = d
            self.syn_state = f"OS {d.version}"
            self._syn_match()
            self._build_library_root()
            self.status_lbl.setText(f"Syntakt connected: {len(d.slots)} samples · {self.syn_check.get('text', '')}")

        def failed(msg):
            self._syn_busy = False
            self.syn = None
            self.syn_state = f"not connected — {msg}"
            self._build_library_root()
            if not quiet:
                QMessageBox.warning(self, APP_NAME, f"The Syntakt didn't connect: {msg}")
        run_job(Job(work), done=done, failed=failed)

    def _syn_close(self):
        if self.syn is not None:
            self.syn.close()
        self.syn = None
        self.syn_types, self.on_syntakt, self.syn_audio = {}, {}, {}

    def _syn_factory_rows(self):
        return self.libdb.query(area=lsrc.AREA, source="Factory", level1="Syntakt", limit=10 ** 6)

    def _syn_match(self):
        """Which library sample each slot holds, its type, and the Twin Shot check."""
        if not self.syn:
            return
        root = self.settings["library_root"]
        rows = self._syn_factory_rows()
        fac_keys = {sdev.name_key(r["name"]) for r in rows}
        others, seen = [], set()
        for s in self.syn.slots:                 # your own samples: look them up in the library by name (0.12.1)
            if s.name not in fac_keys and s.name.split():
                for r in self.libdb.query(text=s.name.lower(), limit=50):
                    if r["rel"] not in seen and sdev.name_key(r["name"]) == s.name:
                        seen.add(r["rel"])
                        others.append(r)
        sdev.match_slots(self.syn.slots, rows, sdev.load_record(root), others)
        self.syn_check = sdev.twin_check(self.syn.slots, rows)
        audio = self.libdb.audio_of([s.match for s in self.syn.slots if s.match])
        folders = self.libdb.listed_folders()
        self.syn_audio, self.on_syntakt, self.syn_types = {}, {}, {}
        for s in self.syn.slots:
            a = audio.get((s.match or "").lower())
            if a:
                self.syn_audio[s.slot] = a
                self.on_syntakt[a] = s.slot
            self.syn_types[s.slot] = folders.get(a) or layout.type_folder(stypes.guess(s.name, s.seconds)[0])

    # ---------------------------------------------------------------- the left pane
    def _syn_tree(self, targets):
        if self.syn:
            counts = {}
            for f in self.syn_types.values():
                counts[f] = counts.get(f, 0) + 1
            it = QTreeWidgetItem([f"SYNTAKT  ({len(self.syn.slots)} of {proto.SLOTS} slots · "
                                  f"{self.syn_check.get('text', '')})"])
            it.setData(0, Qt.UserRole, ("synroot", 1))
            it.setToolTip(0, "The samples on the Syntakt's +Drive, by type. Right-click for Send, Undo, Back up, "
                             "Get from Syntakt and the Twin Shot check.")
            targets.addChild(self._bold(it))
            self._type_branch(it, counts, "syntype")
            it.setExpanded(True)
        else:
            it = QTreeWidgetItem([f"SYNTAKT  ({self.syn_state})"])
            it.setData(0, Qt.UserRole, ("synroot", None))
            it.setForeground(0, Qt.gray)
            it.setToolTip(0, "Right-click to look for the Syntakt again")
            targets.addChild(it)

    def _syn_list(self, folders=None, title="Syntakt"):
        if not self.syn:
            return
        labels = self.libdb.id_labels()
        rows = []
        for s in sorted(self.syn.slots, key=lambda x: x.slot):
            if folders is not None and self.syn_types.get(s.slot) not in folders:
                continue
            a = self.syn_audio.get(s.slot)
            key = f"syntakt:{s.slot}"
            rows.append({"where": "syntakt", "slot": s.slot, "abs": key, "rel": f"Syntakt slot {s.slot} · {s.name}",
                         "name": labels.get(a) or s.name, "file_name": s.name, "pack": f"Slot {s.slot:02d}",
                         "audio": a, "librel": s.match or "", "how": s.how,
                         "types": [self.syn_types.get(s.slot, stypes.UNKNOWN)], "is_loop": False,
                         "type_why": "library sample it matches" if a else "name on the Syntakt", "type_by_user": False,
                         "info": AudioInfo(path=key, container="Syntakt", header="PCM", rate=proto.RATE, bits=16,
                                           channels=1, frames=s.frames)})
        self._fill_rows(rows, title)

    def _syn_tree_menu(self, d, pos):
        m = QMenu(self)
        acts = {}
        if not self.syn:
            acts[m.addAction("Look for the Syntakt again")] = "connect"
        else:
            acts[m.addAction("Twin Shot check…")] = "check"
            acts[m.addAction("Back up all its samples to Backups › Syntakt")] = "backup"
            acts[m.addAction("Get from Syntakt… (samples that aren't in the library)")] = "get"
            j = sdev.load_journal(app_dir())
            a = m.addAction(f"Undo the last Syntakt change ({j[-1]['action']} slot {j[-1]['slot']})" if j
                            else "Undo the last Syntakt change")
            a.setEnabled(bool(j))
            acts[a] = "undo"
            m.addSeparator()
            acts[m.addAction("Read the Syntakt again")] = "connect"
        what = acts.get(m.exec(self.tree.viewport().mapToGlobal(pos)))
        if what == "connect":
            self._syn_connect(quiet=False)
        elif what == "check":
            c = self.syn_check
            text = c.get("text", "")
            if c.get("missing"):
                text += "\n\nMissing from the Syntakt:\n" + "\n".join(c["missing"][:30])
            if c.get("added"):
                text += "\n\nNot from the factory pack (slot, name):\n" + "\n".join(c["added"][:30])
            QMessageBox.information(self, APP_NAME, text)
        elif what == "backup":
            self._syn_backup_all()
        elif what == "get":
            self._syn_get()
        elif what == "undo":
            self._syn_undo()

    # ---------------------------------------------------------------- actions
    def _syn_backup_dir(self) -> str:
        return os.path.join(layout.backups_dir(self.settings["library_root"], "Syntakt"),
                            time.strftime("%Y-%m-%d %H%M%S"))

    def _syn_run(self, work, done, what):
        if self._syn_busy:
            QMessageBox.information(self, APP_NAME, "The Syntakt is still busy with the last job.")
            return
        self._syn_busy = True
        self.status_lbl.setText(what)

        def ok(res):
            self._syn_busy = False
            self.prog.hide()
            try:
                self.syn.refresh()
            except Exception as e:  # noqa: BLE001
                log.warning("Syntakt refresh: %s", e)
            self._syn_match()
            self._build_library_root()
            done(res)

        def bad(msg):
            self._syn_busy = False
            self.prog.hide()
            QMessageBox.warning(self, APP_NAME, f"{what.rstrip('…')} stopped: {msg}\n\nNothing else was changed. "
                                                "Backups of any slot already changed are in Backups › Syntakt, and "
                                                "Undo on the SYNTAKT menu puts it back.")
            try:
                self.syn.refresh()
                self._syn_match()
            except Exception:  # noqa: BLE001
                pass
            self._build_library_root()
        run_job(Job(work, wants_progress=True), done=ok, failed=bad, progress=self._progress)

    def _syn_backup_all(self):
        syn, bdir = self.syn, self._syn_backup_dir()

        def work(progress):
            os.makedirs(bdir, exist_ok=True)
            for k, s in enumerate(syn.slots):
                progress(k, len(syn.slots), f"Backing up slot {s.slot} {s.name}")
                with open(os.path.join(bdir, f"slot {s.slot:02d} {s.name}.bin"), "wb") as fh:
                    fh.write(syn.el.read(s.slot))
            return len(syn.slots)
        self._syn_run(work, lambda n: QMessageBox.information(self, APP_NAME, f"Saved all {n} samples to\n{bdir}"),
                      "Backing up the Syntakt's samples…")

    def _syn_send(self, lib_rows):
        """Send library samples (factory ones too) to the Syntakt: empty slots first, then a slot you pick to replace
        (0.12.1). Replacing asks again, says what will change, backs the slot up, and Undo puts it back."""
        syn, root = self.syn, self.settings["library_root"]
        todo = [r for r in lib_rows if r.get("audio") not in self.on_syntakt]
        if not todo:
            QMessageBox.information(self, APP_NAME, "Those samples are on the Syntakt already.")
            return
        free = sdev.free_slots(syn.slots)
        plan = []
        for r in todo:
            if free:
                plan.append((free.pop(0), r))
                continue
            taken = {n for n, _ in plan}
            choices = [f"{s.slot:2d}  {s.name}  —  " + ("factory" if s.how.startswith("factory") else
                                                         "sent by the app" if s.how == "sent by the app" else "yours")
                       for s in sorted(syn.slots, key=lambda x: x.slot) if s.slot not in taken]
            pick, ok = QInputDialog.getItem(self, APP_NAME, f"Every slot is in use. Which slot should "
                                                            f"{proto.clean_name(r['name'])} replace?\n(The sample in "
                                                            "it is saved to Backups › Syntakt first.)", choices, 0, False)
            if not ok:
                return
            plan.append((int(pick.split()[0]), r))
        lines, warn = [], []
        for n, r in plan:
            old = syn.slot(n)
            lines.append(f"slot {n:2d}  ←  {proto.clean_name(r['name'])}"
                         + (f"   (replaces {old.name})" if old else "   (empty slot)"))
            if old and old.how.startswith("factory"):
                warn.append(f"slot {n} {old.name} is a FACTORY sample — the Twin Shot check will show it missing")
            elif old and old.how != "sent by the app":
                warn.append(f"slot {n} {old.name} is a sample you put there")
        more = len(lib_rows) - len(todo)
        if QMessageBox.question(
                self, APP_NAME, f"Send {len(plan)} sample{'s' if len(plan) != 1 else ''} to the Syntakt?\n\n"
                + "\n".join(lines[:20]) + ("\n…" if len(lines) > 20 else "")
                + ("\n\nReplacing:\n• " + "\n• ".join(warn) + "\nSamples are global on the Syntakt: every project "
                   "that uses a replaced sample will play the new one. Each replaced sample is saved to Backups › "
                   "Syntakt first, and Undo (right-click SYNTAKT) puts it back." if warn else "")
                + "\n\nEach is converted to 48 kHz 16-bit mono (stereo is folded to mono), its name cut to 16 "
                  "characters, and read back to check it." + (f"\n\n{more} not sent: on the Syntakt already."
                                                                 if more else "")
                + "\n\n" + RESTART) != QMessageBox.Yes:
            return
        bdir = self._syn_backup_dir()

        def work(progress):
            rec = sdev.load_record(root)
            notes = []
            for k, (n, r) in enumerate(plan):
                progress(k, len(plan), f"Sending to slot {n}: {r['name']}")
                src = lsrc.materialize(root, r["librel"], os.path.join(app_dir(), "cache")) if r.get("in_zip") \
                    else r["abs"]
                audio, changed = proto.to_syntakt_audio(src)
                data = proto.build_sample(n, r["name"], audio)
                prev = rec.get("slots", {}).get(str(n))
                bk = syn.send(n, data, bdir, replace=True)          # the user has just agreed to the plan
                syn.slots = [x for x in syn.slots if x.slot != n] + [sdev.Slot(n, proto.clean_name(r["name"]),
                                                                               len(data) - 43, r["librel"],
                                                                               "sent by the app")]
                rec.setdefault("slots", {})[str(n)] = {"library": r["librel"], "name": proto.clean_name(r["name"]),
                                                       "size": len(data) - 43, "sent": time.strftime("%Y-%m-%d %H:%M")}
                sdev.save_record(root, rec)
                sdev.add_journal(app_dir(), {"action": "send", "slot": n, "backup": bk, "library": r["librel"],
                                             "record": prev})
                if changed:
                    notes.append(f"slot {n}: " + ", ".join(changed))
            return notes

        def done(notes):
            QMessageBox.information(self, APP_NAME, f"Sent {len(plan)} sample{'s' if len(plan) != 1 else ''}."
                                    + ("\n\nConverted: " + "; ".join(notes[:10]) if notes else "") + "\n\n" + RESTART)
        self._syn_run(work, done, "Sending to the Syntakt…")

    def _syn_remove(self, rows):
        syn, root = self.syn, self.settings["library_root"]
        ok = [r for r in rows if syn.slot(r["slot"]) and not syn.slot(r["slot"]).how.startswith("factory")]
        if not ok:
            QMessageBox.information(self, APP_NAME, "Factory samples can't be removed from the Syntakt. To use a "
                                                    "factory slot for something else, send a sample to it: it asks "
                                                    "which slot to replace when every slot is in use.")
            return
        if QMessageBox.question(
                self, APP_NAME, f"Remove {len(ok)} sample{'s' if len(ok) != 1 else ''} from the Syntakt?\n\n"
                + "\n".join(f"slot {r['slot']}  {r['file_name']}" for r in ok[:20])
                + "\n\nSamples are global on the Syntakt: any project that uses one will lose it. Each is saved to "
                  "Backups › Syntakt first, and Undo puts it back.\n\n" + RESTART) != QMessageBox.Yes:
            return
        bdir = self._syn_backup_dir()

        def work(progress):
            rec = sdev.load_record(root)
            for k, r in enumerate(ok):
                progress(k, len(ok), f"Removing slot {r['slot']}")
                bk = syn.remove(r["slot"], bdir, confirmed=True)
                entry = rec.get("slots", {}).pop(str(r["slot"]), None)
                sdev.save_record(root, rec)
                sdev.add_journal(app_dir(), {"action": "remove", "slot": r["slot"], "backup": bk, "record": entry})
            return len(ok)
        self._syn_run(work, lambda n: QMessageBox.information(self, APP_NAME, f"Removed {n}.\n\n{RESTART}"),
                      "Removing from the Syntakt…")

    def _syn_undo(self):
        e = sdev.load_journal(app_dir())
        if not e:
            return
        e = e[-1]
        what = ("empty slot {slot} again" if e["action"] == "send" and not e.get("backup")
                else "put slot {slot} back as it was").format(**e)
        if QMessageBox.question(self, APP_NAME, f"Undo the last Syntakt change ({e['action']} slot {e['slot']}, "
                                                f"{e.get('when', '')}): {what}?") != QMessageBox.Yes:
            return
        syn, root = self.syn, self.settings["library_root"]

        def work(progress):
            progress(0, 1, f"Putting slot {e['slot']} back")
            syn.put_back(e["slot"], e.get("backup", ""))
            sdev.pop_journal(app_dir())
            rec = sdev.load_record(root)
            if e["action"] == "send" and not e.get("record"):
                rec.get("slots", {}).pop(str(e["slot"]), None)
            elif e.get("record"):
                rec.setdefault("slots", {})[str(e["slot"])] = e["record"]
            sdev.save_record(root, rec)
            return e["slot"]
        self._syn_run(work, lambda n: QMessageBox.information(self, APP_NAME, f"Slot {n} is back as it was.\n\n{RESTART}"),
                      "Undoing the last Syntakt change…")

    def _syn_get(self):
        """Get from Syntakt (spec 4.5): samples on it that are nowhere in the library go to User › <date> Syntakt read."""
        syn, root = self.syn, self.settings["library_root"]
        new = [s for s in syn.slots if not s.match]
        if not new:
            QMessageBox.information(self, APP_NAME, "Every sample on the Syntakt is in the library already.")
            return
        user = layout.find_child(root, "User") or "User"
        dest = os.path.join(root, user, time.strftime("%Y-%m-%d") + " Syntakt read")
        if QMessageBox.question(self, APP_NAME, f"{len(new)} sample{'s are' if len(new) != 1 else ' is'} on the Syntakt "
                                f"but nowhere in the library:\n\n" + "\n".join(f"slot {s.slot}  {s.name}" for s in new[:20])
                                + f"\n\nCopy {'them' if len(new) != 1 else 'it'} into\n{dest}?") != QMessageBox.Yes:
            return

        def work(progress):
            os.makedirs(dest, exist_ok=True)
            for k, s in enumerate(new):
                progress(k, len(new), f"Reading slot {s.slot} {s.name}")
                p = os.path.join(dest, f"{s.name}.wav")
                if not os.path.exists(p):
                    proto.audio_to_wav(proto.parse_sample(syn.read(s))["audio"], p)
            return len(new)
        self._syn_run(work, lambda n: (QMessageBox.information(self, APP_NAME, f"Copied {n} to {dest}."),
                                       self._scan_library(quiet=True)), "Getting samples from the Syntakt…")
