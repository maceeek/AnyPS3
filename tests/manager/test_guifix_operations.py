"""Synthetic manager operation walkthroughs; no game or installed library."""

import errno
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from anyps3.sfo import encode_sfo
from anyps3_manager import processes, runs, saves, settings
from anyps3_manager.storage import Library, ManagerError, atomic_json, sha256, tree_hashes


class GuiFixOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="anyps3-guifix-operations-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lib = Library(self.root/"library")
        self.lib.init()
        self.title = "TEST00001"
        self.lib.register(dict(id=self.title, status="built", active_build="fixture"))
        self.title_root = self.lib.title_dir(self.title)
        self.user = self.title_root/"userdata/hdd0/home/00000001"
        self.user.mkdir(parents=True)
        (self.user/"localusername").write_text("Fixture")
        (self.user/"savedata").mkdir()
        settings.set_value(self.lib, "audio.enabled", False)

    def save(self, name="PROGRESS"):
        path = self.user/"savedata"/name
        path.mkdir(parents=True)
        (path/"PARAM.SFO").write_bytes(encode_sfo({"TITLE": "Fixture progress"}))
        (path/"DATA").write_bytes(b"irreplaceable progress")
        return path

    def test_save_roundtrip_preserves_nested_empty_directories(self):
        path = self.save()
        (path/"empty/nested").mkdir(parents=True)
        output = self.root/"portable.zip"
        saves.export_save(self.lib, self.title, path.name, output)
        shutil.rmtree(path)
        saves.import_save(self.lib, self.title, output)
        self.assertEqual((path/"DATA").read_bytes(), b"irreplaceable progress")
        self.assertTrue((path/"empty/nested").is_dir())
        self.assertEqual(saves.list_saves(self.lib, self.title)[0]["title"], "Fixture progress")

    def test_failed_export_does_not_publish_partial_file(self):
        path = self.save()
        (path/"ZZ-link").symlink_to(path/"DATA")
        output = self.root/"portable.zip"
        with self.assertRaises(ManagerError):
            saves.export_save(self.lib, self.title, path.name, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".save-export-*")), [])

    def test_export_without_hardlinks_produces_readable_portable_zip(self):
        self.save()
        output = self.root/"portable.zip"
        with patch("anyps3_manager.saves.os.link", side_effect=OSError(errno.EOPNOTSUPP, "No hardlinks")):
            saves.export_save(self.lib, self.title, "PROGRESS", output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("save/DATA"), b"irreplaceable progress")
            self.assertEqual(json.loads(archive.read("anyps3-save.json"))["save"], "PROGRESS")
        self.assertEqual(list(self.root.glob(".save-export-*")), [])

    def test_export_without_hardlinks_preserves_raced_in_destination(self):
        self.save()
        output = self.root/"portable.zip"
        def unavailable(*args):
            output.write_bytes(b"existing destination")
            raise OSError(errno.EOPNOTSUPP, "No hardlinks")
        with patch("anyps3_manager.saves.os.link", side_effect=unavailable):
            with self.assertRaises(FileExistsError):
                saves.export_save(self.lib, self.title, "PROGRESS", output)
        self.assertEqual(output.read_bytes(), b"existing destination")
        self.assertEqual(list(self.root.glob(".save-export-*")), [])

    def test_failed_export_copy_without_hardlinks_removes_partial_destination(self):
        self.save()
        output = self.root/"portable.zip"
        original_copy = shutil.copyfileobj
        def failed_copy(source, destination, *args):
            if destination.name != str(output):
                return original_copy(source, destination, *args)
            destination.write(b"partial zip")
            raise OSError(errno.ENOSPC, "Full destination")
        with patch("anyps3_manager.saves.os.link", side_effect=OSError(errno.EOPNOTSUPP, "No hardlinks")), \
             patch("anyps3_manager.saves.shutil.copyfileobj", side_effect=failed_copy):
            with self.assertRaises(OSError) as caught:
                saves.export_save(self.lib, self.title, "PROGRESS", output)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".save-export-*")), [])

    def test_export_rejects_selected_save_and_ancestor_symlinks(self):
        external = self.root/"external"
        external.mkdir()
        (external/"secret").write_bytes(b"outside save storage")
        (self.user/"savedata/LINK").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ManagerError):
            saves.export_save(self.lib, self.title, "LINK", self.root/"export.zip")
        (self.user/"savedata/LINK").unlink()
        (self.user/"savedata").rmdir()
        (self.user/"savedata").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ManagerError):
            saves.backup(self.lib, self.title)
        self.assertEqual(tree_hashes(external), {"secret": sha256(external/"secret")})

    def test_import_rejects_metadata_without_touching_userdata(self):
        self.save()
        before = tree_hashes(self.title_root/"userdata")
        for metadata in ([], None, {"schema": saves.EXPORT_SCHEMA, "title": self.title}):
            with self.subTest(metadata=metadata):
                output = self.root/"invalid.zip"
                with zipfile.ZipFile(output, "w") as archive:
                    archive.writestr("anyps3-save.json", json.dumps(metadata))
                with self.assertRaises(ManagerError):
                    saves.import_save(self.lib, self.title, output)
                self.assertEqual(tree_hashes(self.title_root/"userdata"), before)

    def test_restore_of_empty_snapshot_clears_later_saves_and_preserves_pre_restore_backup(self):
        (self.user/"savedata").rmdir()
        snapshot = saves.backup(self.lib, self.title)["backup"]
        self.save()
        saves.restore(self.lib, self.title, snapshot)
        self.assertEqual(list((self.user/"savedata").iterdir()), [])
        backups = [p.name for p in (self.title_root/"backups").iterdir() if p.name != snapshot]
        self.assertEqual(len(backups), 1)
        saves.restore(self.lib, self.title, backups[0])
        self.assertEqual((self.user/"savedata/PROGRESS/DATA").read_bytes(), b"irreplaceable progress")

    def test_manual_backups_survive_automatic_retention(self):
        self.save()
        settings.set_value(self.lib, "saves.keep", 1)
        manual = saves.backup(self.lib, self.title)["backup"]
        saves.backup(self.lib, self.title, "run", automatic=True)
        recent = saves.backup(self.lib, self.title, "run", automatic=True)["backup"]
        self.assertEqual({p.name for p in (self.title_root/"backups").iterdir()}, {manual, recent})

    def test_save_mutations_refuse_while_run_lock_is_held(self):
        self.save()
        output = self.root/"portable.zip"
        saves.export_save(self.lib, self.title, "PROGRESS", output)
        backup = saves.backup(self.lib, self.title)["backup"]
        before = tree_hashes(self.title_root/"userdata")
        with self.lib.run_lock(self.title):
            for operation in (lambda: saves.import_save(self.lib, self.title, output),
                              lambda: saves.restore(self.lib, self.title, backup)):
                with self.assertRaises(ManagerError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, "lock-busy")
        self.assertEqual(tree_hashes(self.title_root/"userdata"), before)

    def test_audio_and_rumble_layers_compose_without_launching_enabled_audio(self):
        settings.set_value(self.lib, "audio.enabled", True, title=self.title)
        settings.set_value(self.lib, "input.rumble", False, title=self.title)
        values = settings.effective(self.lib, self.title)
        enabled = settings.run_environment(self.lib, self.title, values, ["ANYPS3_PAD_RUMBLE"], self.root)
        self.assertEqual(enabled["ANYPS3_AUDIO_SINK"], "sdl")
        self.assertEqual(enabled["ANYPS3_AUDIO_AUDIBLE"], "1")
        self.assertEqual(enabled["ANYPS3_PAD_RUMBLE"], "0")
        values = settings.effective(self.lib, self.title, {"audio.enabled": False, "input.rumble": True})
        silent = settings.run_environment(self.lib, self.title, values, ["ANYPS3_PAD_RUMBLE"], self.root)
        self.assertEqual(silent["ANYPS3_AUDIO_SINK"], "null")
        self.assertEqual(silent["SDL_AUDIODRIVER"], "dummy")
        self.assertNotIn("ANYPS3_AUDIO_AUDIBLE", silent)
        self.assertEqual(silent["ANYPS3_PAD_RUMBLE"], "1")
        self.assertNotIn("ANYPS3_PAD_RUMBLE", settings.run_environment(self.lib, self.title, values, [], self.root))

    def fixture_build(self, tail=""):
        native = self.title_root/"builds/fixture/native"
        native.mkdir(parents=True)
        program = native/"program"
        program.write_text("#!"+sys.executable+"\nimport os,sys\n"
            "assert os.environ['SDL_AUDIODRIVER']=='dummy'\n"
            "assert os.environ['ANYPS3_AUDIO_SINK']=='null'\n"
            "assert 'ANYPS3_AUDIO_AUDIBLE' not in os.environ\n"
            "print('PERF interval_s=2.000 flips_per_s=60.0 flips=120', file=sys.stderr, flush=True)\n"+tail)
        program.chmod(0o700)
        (native/"program.elf").write_bytes(b"synthetic fixture, never game data")
        atomic_json(native.parent/"record.json", dict(status="built", available_env=[]))
        atomic_json(native.parent/"verification.json", dict(program_sha256=sha256(program), elf_sha256=sha256(native/"program.elf")))
        return native

    def test_boot_observation_preserves_existing_play_verification(self):
        self.fixture_build()
        self.lib.update_title(self.title, status="verified-play")
        result = runs.run(self.lib, self.title, unscoped=True)
        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["fps_mean"], 60.0)
        self.assertEqual(self.lib.title(self.title)["status"], "verified-play")
        self.assertFalse(runs.stop(self.lib, self.title)["stopped"])

    def test_manual_stop_keeps_flip_and_capture_boot_evidence(self):
        settings.set_value(self.lib, "run.keep", 1)
        for run_id, state in (("0001", "finished"), ("0002", "finished"), ("0003", "running")):
            atomic_json(self.title_root/"runs"/run_id/"run.json", dict(id=run_id, state=state))
        self.fixture_build("print('RSX captured frame=2 buffer=1 size=1280x720 file=fixture.ppm', file=sys.stderr, flush=True)\n"
                           "import time;time.sleep(30)\n")
        failures = []
        observed = []
        def request_stop():
            try:
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    live = [record for record in runs.records(self.lib, self.title)
                            if record["state"] == "running" and record.get("flips") == 120
                            and record.get("captured_frames") == 2]
                    if live:
                        observed.append(live[0])
                        runs.stop(self.lib, self.title, live[0]["id"])
                        return
                    time.sleep(.01)
                failures.append("Fixture did not produce its evidence before timeout")
            except BaseException as exc:
                failures.append(str(exc))
        thread = threading.Thread(target=request_stop)
        thread.start()
        try:
            with self.assertRaises(ManagerError) as caught:
                runs.run(self.lib, self.title, unscoped=True)
            self.assertEqual(caught.exception.code, "cancelled")
        finally:
            thread.join(timeout=8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["fps_mean"], 60.0)
        self.assertEqual(observed[0]["fps_samples"], 1)
        self.assertNotIn("verification_status", observed[0])
        result = runs.records(self.lib, self.title)[0]
        self.assertEqual(result["state"], "finished")
        self.assertTrue(result["stopped_by_user"])
        self.assertEqual(result["flips"], 120)
        self.assertEqual(result["captured_frames"], 2)
        self.assertEqual(result["verification_status"], "verified-boot")
        self.assertEqual(self.lib.title(self.title)["status"], "verified-boot")
        self.assertEqual({r["id"] for r in runs.records(self.lib, self.title)}, {result["id"], "0003"})

    def test_lost_supervisor_stop_publishes_boot_evidence_and_prunes_finished_runs(self):
        settings.set_value(self.lib, "run.keep", 1)
        atomic_json(self.title_root/"runs/0001/run.json", dict(id="0001", state="finished"))
        directory = self.title_root/"runs/lost-supervisor"
        atomic_json(directory/"run.json", dict(id="lost-supervisor", state="running", build="fixture"))
        (directory/"stderr.log").write_text("PERF interval_s=2.000 flips_per_s=60.0 flips=120\n")
        self.assertTrue(runs.stop(self.lib, self.title)["stop_requested"])
        result = runs.records(self.lib, self.title)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["supervisor_lost"])
        self.assertEqual(result[0]["verification_status"], "verified-boot")
        self.assertEqual(result[0]["flips"], 120)
        self.assertEqual(self.lib.title(self.title)["status"], "verified-boot")

    def test_log_summary_uses_cumulative_counters_and_ignores_malformed_rates(self):
        log = self.root/"stderr.log"
        log.write_text("PERF interval_s=2 flips_per_s=60.0 flips=120\n"
                       "PERF interval_s=2 flips_per_s=59.0 flips=238\n"
                       "RSX captured frame=1 buffer=1 size=1280x720 file=fixture.ppm\n"
                       "RSX captured_frames=3 flips=240 draws=30 vblank_ticks=241\n"
                       "PERF interval_s=2 flips_per_s=1.2.3\n")
        result = runs.summarize(log)
        self.assertEqual(result["flips"], 240)
        self.assertEqual(result["captured_frames"], 3)
        self.assertEqual(result["fps_mean"], 59.5)
        self.assertEqual(result["fps_samples"], 2)

    def test_live_summary_reads_each_line_once_and_waits_for_partial_lines(self):
        log = self.root/"stderr.log"
        log.write_bytes(b"PERF interval_s=2 flips_per_s=60.0 flips=120\nRSX captured frame=")
        summary = runs.RunSummary()
        with log.open("rb") as reader:
            summary.read_new(reader)
            self.assertEqual(summary.result()["fps_samples"], 1)
            self.assertEqual(summary.result()["captured_frames"], 0)
            with log.open("ab") as writer:
                writer.write(b"2 buffer=1 size=1280x720 file=fixture.ppm\n")
            summary.read_new(reader)
            summary.read_new(reader)
        self.assertEqual(summary.result()["fps_samples"], 1)
        self.assertEqual(summary.result()["flips"], 120)
        self.assertEqual(summary.result()["captured_frames"], 2)
        self.assertEqual(summary.result(), runs.summarize(log))

    def test_rate_without_flip_count_does_not_claim_boot(self):
        native = self.fixture_build()
        program = native/"program"
        program.write_text(program.read_text().replace(" flips=120", ""))
        atomic_json(native.parent/"verification.json", dict(program_sha256=sha256(program), elf_sha256=sha256(native/"program.elf")))
        result = runs.run(self.lib, self.title, unscoped=True)
        self.assertEqual(result["fps_mean"], 60.0)
        self.assertNotIn("verification_status", result)
        self.assertEqual(self.lib.title(self.title)["status"], "built")

    def test_tool_cancellation_stops_process_and_releases_log(self):
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        seen = []
        def cancel(process, observed):
            seen.append(processes.identity(process.pid))
            raise ManagerError("cancelled", "Synthetic user cancellation", 5)
        with self.assertRaises(ManagerError):
            processes.execute(command, self.root/"tool.log", env=settings.clean_environment(),
                unit="unused-fixture", memory="1G", cpus="", unscoped=True, progress=cancel)
        self.assertEqual(len(seen), 1)
        self.assertFalse(processes.alive(seen[0]))
        (self.root/"tool.log").unlink()
