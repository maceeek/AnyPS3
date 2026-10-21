import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from anyps3.sfo import encode_sfo
from anyps3_manager import jobs, pipeline, runs, saves, settings
from anyps3_manager.cli import main
from anyps3_manager.processes import Toolchain
from anyps3_manager.storage import Library, ManagerError, atomic_json, lock, read_json, tree_hashes
from tests.test_ingest import both, record

SOURCE = Path(__file__).resolve().parents[2]


def elf_fixture():
    data = bytearray(0x110)
    data[:16] = b"\x7fELF\x02\x02\x01"+bytes(9)
    struct.pack_into(">HHIQQQIHHHHHH", data, 16, 2, 21, 1, 0x10000, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into(">IIQQQQQQ", data, 64, 1, 5, 0x100, 0x10000, 0, 16, 16, 0x1000)
    data[0x100:] = bytes(range(16))
    return bytes(data)


def iso_fixture(version="01.00"):
    data = bytearray(32*2048)
    pvd = bytearray(2048)
    pvd[:7] = b"\x01CD001\x01"
    pvd[40:72] = b"MANAGER TEST".ljust(32, b" ")
    pvd[80:88], pvd[128:132] = both(32, 4), both(2048, 2)
    root = record(b"\0", 20, 2048, 2)
    pvd[156:156+len(root)] = root
    data[16*2048:17*2048] = pvd
    sfo = encode_sfo(dict(TITLE_ID="BCES00511", TITLE="Synthetic fixture", CATEGORY="DG", APP_VER=version))
    entries = {
        20: root+record(b"\1", 20, 2048, 2)+record(b"PS3_GAME", 21, 2048, 2),
        21: record(b"\0", 21, 2048, 2)+record(b"\1", 20, 2048, 2)+record(b"PARAM.SFO;1", 23, len(sfo))+record(b"USRDIR", 22, 2048, 2),
        22: record(b"\0", 22, 2048, 2)+record(b"\1", 21, 2048, 2)+record(b"EBOOT.BIN;1", 24, len(elf_fixture())),
        23: sfo, 24: elf_fixture(),
    }
    for sector, payload in entries.items():
        data[sector*2048:sector*2048+len(payload)] = payload
    return bytes(data)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="anyps3-manager-test-")
        self.root = Path(self.temp.name)
        self.lib = Library(self.root/"library", SOURCE)
        self.lib.init()
        self.iso = self.root/"disc.iso"
        self.iso.write_bytes(iso_fixture())
        self.key = self.root/"key.json"
        self.key.write_text("{}")
        pipeline.set_key(self.lib, "app", self.key)
        firmware = self.lib.root/"firmware/test"
        (firmware/"dev_flash/sys/external").mkdir(parents=True)
        atomic_json(firmware/"extraction.json", dict(version="test", pup_sha256="0"*64, files=[]))
        self.tools = self.root/"toolchain.json"
        stub = Path(__file__).with_name("toolchain_stub.py")
        atomic_json(self.tools, {name: [sys.executable, str(stub), name] for name in ("cli", "translate", "compile")})
        self.no = 0

    def tearDown(self):
        from anyps3_manager.storage import remove_tree
        remove_tree(self.root)
        self.temp.cleanup()

    def pipeline(self):
        self.no += 1
        job = jobs.Job(self.lib, f"test-{self.no}")
        job.path.mkdir()
        atomic_json(job.path/"job.json", dict(state="running", stage="test"))
        return pipeline.Pipeline(self.lib, Toolchain(SOURCE, self.tools), job, unscoped=True)

    def import_title(self):
        self.pipeline().import_iso(str(self.iso), no_build=True)
        return self.lib.title_dir("BCES00511")

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([*args, "--library", str(self.lib.root), "--json"])
        return code, json.loads(output.getvalue())

    def test_iso_identification_and_partial_refusal(self):
        self.assertEqual(pipeline.disc_identity(self.iso)["id"], "BCES00511")
        self.iso.write_bytes(iso_fixture()[:-2048])
        with self.assertRaisesRegex(ManagerError, "incomplete"):
            pipeline.disc_identity(self.iso)

    def test_userdata_unchanged_across_build_activate_prune_and_reimport(self):
        root = self.import_title()
        save = root/"userdata/hdd0/home/00000001/savedata/TEST"
        save.mkdir()
        (save/"DATA").write_bytes(b"irreplaceable progress")
        before = tree_hashes(root/"userdata")
        first = self.pipeline().build("BCES00511")["build"]
        second = self.pipeline().build("BCES00511")["build"]
        self.assertNotEqual(first, second)
        pipeline.activate(self.lib, "BCES00511", first)
        self.lib.update_title("BCES00511", pinned_builds=[second])
        self.assertEqual(pipeline.prune(self.lib, "BCES00511", 0)["removed"], [])
        self.lib.update_title("BCES00511", pinned_builds=[])
        self.assertEqual(pipeline.prune(self.lib, "BCES00511", 0)["removed"], [second])
        self.iso.write_bytes(iso_fixture("02.00"))
        self.pipeline().import_iso(str(self.iso), no_build=True, replace_disc="BCES00511")
        self.assertEqual(tree_hashes(root/"userdata"), before)
        self.assertEqual(self.lib.title("BCES00511")["active_build"], first)
        self.assertEqual(self.lib.title("BCES00511")["status"], "stale")

    def test_same_iso_noop_and_key_reference_permissions(self):
        self.import_title()
        self.assertTrue(self.pipeline().import_iso(str(self.iso), no_build=True)["already_imported"])
        self.assertEqual((self.lib.root/"keys.json").stat().st_mode & 0o777, 0o600)
        self.key.write_text('{"changed":true}')
        with self.assertRaisesRegex(ManagerError, "changed"):
            pipeline.key_path(self.lib, "app")

    def test_settings_layers_validation_and_lock(self):
        self.import_title()
        settings.set_value(self.lib, "build.jobs", 7)
        settings.set_value(self.lib, "build.jobs", 3, title="BCES00511")
        self.assertEqual(settings.effective(self.lib, "BCES00511", {"build.jobs": 2})["build.jobs"], 2)
        settings.set_value(self.lib, "build.jobs", None, title="BCES00511", reset=True)
        self.assertEqual(settings.effective(self.lib, "BCES00511")["build.jobs"], 7)
        for key, value in (("build.jobs", 0), ("build.jobs", True), ("run.memory_max", "0G"), ("video.present_mode", "invalid")):
            with self.subTest(key=key), self.assertRaises(ManagerError):
                settings.set_value(self.lib, key, value)
        for overrides in ({"audio.audible_allowed": True}, {"audio.audible_allowed": False}):
            with self.assertRaises(ManagerError):
                settings.effective(self.lib, "BCES00511", overrides)
        self.assertEqual(self.cli("settings", "set", "audio.audible_allowed", "true")[0], 2)
        self.assertEqual(self.cli("settings", "set", "--global", "audio.audible_allowed", "true")[0], 2)
        self.assertEqual(self.cli("settings", "set", "--title", "BCES00511", "build.jobs", "4")[0], 0)
        self.assertEqual(self.cli("settings", "get", "--title", "BCES00511")[1]["settings"]["build.jobs"], 4)

    def test_game_rumble_setting_reaches_supported_runtime_only(self):
        self.import_title()
        values = settings.effective(self.lib, "BCES00511")
        self.assertIs(values["input.rumble"], True)
        self.assertFalse(settings.schema([])["input.rumble"]["available"])
        available = ["ANYPS3_PAD_RUMBLE"]
        self.assertTrue(settings.schema(available)["input.rumble"]["available"])
        env = settings.run_environment(self.lib, "BCES00511", values, available, self.root)
        self.assertEqual(env["ANYPS3_PAD_RUMBLE"], "1")
        values["input.rumble"] = False
        env = settings.run_environment(self.lib, "BCES00511", values, available, self.root)
        self.assertEqual(env["ANYPS3_PAD_RUMBLE"], "0")
        env = settings.run_environment(self.lib, "BCES00511", values, [], self.root)
        self.assertNotIn("ANYPS3_PAD_RUMBLE", env)

    def test_all_process_environments_silent_and_unavailable_switch_omitted(self):
        self.import_title()
        ambient = {"ANYPS3_AUDIO_AUDIBLE": "1", "SDL_AUDIODRIVER": "pulseaudio",
                   "ANYPS3_VIRTUAL_PAD_CONTROL": "/bad", "ANYPS3_RSX_RECORDER": "1"}
        with patch.dict(os.environ, ambient):
            for env in (settings.clean_environment(), Toolchain(SOURCE).environment(),
                        settings.run_environment(self.lib, "BCES00511", settings.effective(self.lib, "BCES00511"), [], self.root)):
                self.assertEqual(env["SDL_AUDIODRIVER"], "dummy")
                self.assertEqual(env["ANYPS3_AUDIO_SINK"], "null")
                self.assertNotIn("ANYPS3_AUDIO_AUDIBLE", env)
                self.assertNotIn("ANYPS3_VIRTUAL_PAD_CONTROL", env)
                self.assertNotIn("ANYPS3_RSX_RECORDER", env)
            self.pipeline().build("BCES00511")
            with self.assertRaises(ManagerError) as caught:
                runs.run(self.lib, "BCES00511", unscoped=True)
            self.assertEqual(caught.exception.exit_code, 3)
            run = runs.records(self.lib, "BCES00511")[0]
            self.assertEqual(run["fps_mean"], 12.5)
            self.assertIn("synthetic unsupported", run["stop"])

    def test_locks_and_stale_pid_do_not_signal_other_processes(self):
        from anyps3_manager.processes import identity, signal_process
        with lock(self.lib.root/"locks/test.lock"):
            with self.assertRaises(ManagerError) as caught:
                with lock(self.lib.root/"locks/test.lock"):
                    pass
            self.assertEqual(caught.exception.exit_code, 4)
        record = identity(os.getpid())
        record["start_ticks"] = "wrong"
        self.assertFalse(signal_process(record, signal.SIGTERM))

    def test_detached_job_survives_client_exit_and_replays(self):
        self.import_title()
        command = [sys.executable, "-m", "anyps3_manager", "--library", str(self.lib.root),
                   "--toolchain", str(self.tools), "--unscoped", "build", "BCES00511", "--json"]
        client = subprocess.run(command, capture_output=True, text=True, cwd=SOURCE, timeout=10)
        self.assertEqual(client.returncode, 0, client.stdout+client.stderr)
        job_id = json.loads(client.stdout)["job"]
        job = jobs.Job(self.lib, job_id)
        events = list(job.follow())
        self.assertEqual(job.record()["state"], "succeeded", (job.path/"log.txt").read_text())
        self.assertEqual(events[-1]["event"], "result")
        self.assertTrue(any(e["event"] == "progress" for e in events))
        self.assertEqual(events, list(job.follow()))

    def test_save_roundtrip_and_zip_traversal_rejection(self):
        self.import_title()
        root = saves.save_root(self.lib, "BCES00511")/"TEST"
        root.mkdir()
        (root/"PARAM.SFO").write_bytes(encode_sfo({"TITLE": "Fixture save"}))
        (root/"DATA").write_bytes(b"before")
        backup = saves.backup(self.lib, "BCES00511")["backup"]
        output = self.root/"save.zip"
        saves.export_save(self.lib, "BCES00511", "TEST", output)
        (root/"DATA").write_bytes(b"after")
        saves.restore(self.lib, "BCES00511", backup)
        self.assertEqual((root/"DATA").read_bytes(), b"before")
        (root/"DATA").write_bytes(b"after")
        saves.import_save(self.lib, "BCES00511", output)
        self.assertEqual((root/"DATA").read_bytes(), b"before")
        with zipfile.ZipFile(output, "a") as archive:
            archive.writestr("save/../../escaped", b"bad")
        with self.assertRaises(ManagerError):
            saves.import_save(self.lib, "BCES00511", output)
        self.assertFalse((self.root/"escaped").exists())

    def test_usage_errors_are_versioned_json(self):
        code, value = self.cli("nonsense")
        self.assertEqual(code, 2)
        self.assertEqual(value["schema"], "anyps3.manager/1")
        self.assertEqual(value["event"], "error")

    def test_missing_systemd_refuses_before_run_side_effects(self):
        root = self.import_title()
        self.pipeline().build("BCES00511")
        before = tree_hashes(root/"userdata")
        with patch("anyps3_manager.processes.scope_available", return_value=False):
            with self.assertRaises(ManagerError) as caught:
                runs.run(self.lib, "BCES00511")
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertEqual(tree_hashes(root/"userdata"), before)
        self.assertEqual(runs.records(self.lib, "BCES00511"), [])

    def test_resume_keeps_verified_extraction_and_userdata(self):
        first = self.pipeline()
        with patch.object(first, "choose_firmware", side_effect=ManagerError("missing-firmware", "Fixture", 6)):
            with self.assertRaises(ManagerError):
                first.import_iso(str(self.iso), no_build=True)
        stage = next(self.lib.title_dir("BCES00511").glob(".import-*"))
        marker = stage/"disc/PS3_GAME/PARAM.SFO"
        inode = marker.stat().st_ino
        self.pipeline().import_iso(str(self.iso), no_build=True)
        self.assertEqual((self.lib.title_dir("BCES00511")/"disc/PS3_GAME/PARAM.SFO").stat().st_ino, inode)

    def test_run_lock_prevents_restore_and_title_removal(self):
        self.import_title()
        backup = saves.backup(self.lib, "BCES00511")["backup"]
        with self.lib.run_lock("BCES00511"):
            with self.assertRaises(ManagerError) as caught:
                saves.restore(self.lib, "BCES00511", backup)
            self.assertEqual(caught.exception.exit_code, 4)
            self.assertEqual(self.cli("titles", "remove", "BCES00511")[0], 4)

    def test_title_removal_preserves_userdata_and_last_backup(self):
        root = self.import_title()
        before = tree_hashes(root/"userdata")
        self.assertEqual(self.cli("titles", "remove", "BCES00511")[0], 0)
        self.assertEqual(tree_hashes(root/"userdata"), before)
        self.assertEqual(self.lib.titles(), [])

    def test_cancel_stops_compiler_descendants(self):
        from anyps3_manager.processes import identity, alive, stop_tree
        child_file = self.root/"child.pid"
        command = [sys.executable, "-c",
            "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
            "open(sys.argv[1],'w').write(str(p.pid));time.sleep(30)", str(child_file)]
        process = subprocess.Popen(command)
        try:
            deadline = time.monotonic()+3
            while not child_file.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            child = identity(int(child_file.read_text()))
            stop_tree(process.pid, timeout=.1)
            process.wait(timeout=3)
            deadline = time.monotonic()+3
            while alive(child) and time.monotonic() < deadline:
                # A zombie has exited; its eventual reaping belongs to init.
                stat = Path(f"/proc/{child['pid']}/stat")
                if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    break
                time.sleep(.01)
            if alive(child):
                self.assertEqual(Path(f"/proc/{child['pid']}/stat").read_text().rsplit(")", 1)[1].split()[0], "Z")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_scoped_import_build_and_run(self):
        from anyps3_manager.processes import scope_available
        if not scope_available():
            self.skipTest("A systemd user manager is required for this integration check.")
        worker = self.pipeline()
        worker.unscoped = False
        worker.import_iso(str(self.iso), no_build=True)
        built = worker.build("BCES00511")
        self.assertEqual(built["status"], "built")
        with self.assertRaises(ManagerError) as caught:
            runs.run(self.lib, "BCES00511")
        self.assertEqual(caught.exception.exit_code, 3)
        record = runs.records(self.lib, "BCES00511")[0]
        self.assertTrue(record["scope"].startswith("anyps3-bces00511-"))

    def test_verify_rejects_modified_game_bytes(self):
        root = self.import_title()
        built = self.pipeline().build("BCES00511")["build"]
        path = root/"builds"/built
        data = bytearray((path/"native/program.elf").read_bytes())
        data[-1] ^= 1
        (path/"native/program.elf").chmod(0o644)
        (path/"native/program.elf").write_bytes(data)
        with self.assertRaisesRegex(ManagerError, "PT_LOAD"):
            pipeline.verify_build(root/"eboot/EBOOT.analysis.elf", path)

    def sleep_build(self):
        root = self.import_title()
        built = self.pipeline().build("BCES00511")["build"]
        path = root/"builds"/built
        program = path/"native/program"
        program.chmod(0o755)
        program.write_text("#!"+sys.executable+"\nimport time\ntime.sleep(60)\n")
        path.chmod(0o755)
        verified = read_json(path/"verification.json")
        verified["program_sha256"] = hashlib.sha256(program.read_bytes()).hexdigest()
        atomic_json(path/"verification.json", verified)
        settings.set_value(self.lib, "saves.backup_before_run", False)
        return root

    def test_launch_exception_terminates_and_reaps_child(self):
        from anyps3_manager.processes import identity
        self.sleep_build()
        children = []
        popen = subprocess.Popen

        def launch(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        def fail_child_identity(pid):
            if pid != os.getpid():
                raise ManagerError("cancelled", "Injected immediate launch cancellation.", 5)
            return identity(pid)

        try:
            with patch("anyps3_manager.runs.subprocess.Popen", side_effect=launch), \
                 patch("anyps3_manager.runs.identity", side_effect=fail_child_identity):
                with self.assertRaises(ManagerError) as caught:
                    runs.run(self.lib, "BCES00511", unscoped=True)
            self.assertEqual(caught.exception.exit_code, 5)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            with self.lib.run_lock("BCES00511"), self.lib.title_lock("BCES00511"):
                pass
            self.assertEqual(runs.records(self.lib, "BCES00511")[0]["state"], "finished")
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()

    def test_supervisor_death_preserves_locks_until_child_stops(self):
        from anyps3_manager.processes import alive, stop_tree
        self.sleep_build()
        code = ("import sys; from anyps3_manager.storage import Library; "
                "from anyps3_manager.runs import run; run(Library(sys.argv[1],sys.argv[2]),"
                "'BCES00511',unscoped=True)")
        worker = subprocess.Popen([sys.executable, "-P", "-c", code, str(self.lib.root), str(SOURCE)],
                                  env=Toolchain(SOURCE).environment())
        child = None
        try:
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                records = runs.records(self.lib, "BCES00511")
                if records and records[0].get("program_process"):
                    child = records[0]["program_process"]
                    break
                time.sleep(.02)
            self.assertIsNotNone(child)
            worker.kill()
            worker.wait(timeout=3)
            self.assertTrue(alive(child))
            for acquire in (self.lib.run_lock, self.lib.title_lock):
                with self.assertRaises(ManagerError) as caught:
                    with acquire("BCES00511"):
                        pass
                self.assertEqual(caught.exception.exit_code, 4)
            self.assertTrue(runs.stop(self.lib, "BCES00511")["stop_requested"])
            self.assertFalse(alive(child))
            with self.lib.run_lock("BCES00511"), self.lib.title_lock("BCES00511"):
                pass
            record = runs.records(self.lib, "BCES00511")[0]
            self.assertEqual(record["state"], "finished")
            self.assertTrue(record["supervisor_lost"])
        finally:
            if child and alive(child):
                stop_tree(child["pid"], timeout=.1)
            if worker.poll() is None:
                worker.kill()
            worker.wait()

    def test_stop_during_launch_prevents_child_start(self):
        self.sleep_build()
        write_json = runs.atomic_json

        def stop_after_record(path, value):
            write_json(path, value)
            if Path(path).name == "run.json" and value["state"] == "running":
                write_json(Path(path).parent/"stop-request.json", {})

        with patch("anyps3_manager.runs.atomic_json", side_effect=stop_after_record), \
             patch("anyps3_manager.runs.subprocess.Popen") as launch:
            with self.assertRaises(ManagerError) as caught:
                runs.run(self.lib, "BCES00511", unscoped=True)
        self.assertEqual(caught.exception.exit_code, 5)
        launch.assert_not_called()
        with self.lib.run_lock("BCES00511"):
            pass

    def test_capabilities_emit_actual_runtime_worker_default(self):
        from anyps3_manager.cli import available
        self.import_title()
        capabilities = available(self.lib, None)
        self.assertIn("ANYPS3_RSX_WORKER", capabilities)
        self.assertIn("ANYPS3_PAD_RUMBLE", capabilities)
        self.assertTrue(settings.schema(capabilities)["performance.rsx_worker"]["available"])
        env = settings.run_environment(self.lib, "BCES00511", settings.effective(self.lib, "BCES00511"),
                                       capabilities, self.root)
        self.assertEqual(env["ANYPS3_RSX_WORKER"], "1")
        self.assertNotIn("ANYPS3_NONEXISTENT_SETTING", capabilities)

    def test_detached_worker_builds_explicit_source_and_profile(self):
        alternate = self.root/"alternate-source"
        shutil.copytree(SOURCE/"anyps3", alternate/"anyps3", ignore=shutil.ignore_patterns("__pycache__"))
        marker = alternate/"anyps3/manager_fixture_marker.py"
        marker.write_text("VALUE = 'alternate source; not installed checkout'\n")
        profile = alternate/"profiles/fixture.json"
        atomic_json(profile, dict(schema_version=1, title="Alternate profile",
                    executable_sha256=hashlib.sha256(elf_fixture()).hexdigest(), static_seed_entries=[]))
        lib = Library(self.lib.root, alternate)
        job_id = jobs.start(lib, "import", dict(iso=str(self.iso)), self.tools, unscoped=True)["job"]
        job = jobs.Job(lib, job_id)
        list(job.follow())
        self.assertEqual(job.record()["state"], "succeeded", (job.path/"log.txt").read_text())
        title = lib.title("BCES00511")
        self.assertEqual(title["profile_path"], str(profile))
        build = lib.title_dir("BCES00511")/"builds"/title["active_build"]
        self.assertEqual((build/"source/anyps3"/marker.name).read_bytes(), marker.read_bytes())
        self.assertEqual((build/"profile.json").read_bytes(), profile.read_bytes())
        self.assertIn("ANYPS3_RSX_WORKER", read_json(build/"record.json")["available_env"])

    def adoption_fixture(self):
        root = self.import_title()
        source = self.root/"old-ratchet"
        shutil.copytree(root/"disc", source/"game-complete")
        shutil.copytree(root/"userdata/hdd0", source/"hdd0")
        shutil.copytree(self.lib.root/"firmware/test", source/"firmware-verified")
        shutil.copyfile(root/"eboot/EBOOT.analysis.elf", source/"EBOOT.analysis.elf")
        destination = Library(self.root/"adopted-library", SOURCE)
        destination.init()
        job = jobs.Job(destination, "adoption-test")
        job.path.mkdir()
        atomic_json(job.path/"job.json", dict(state="running", stage="test"))
        worker = pipeline.Pipeline(destination, Toolchain(SOURCE, self.tools), job, unscoped=True)
        profile = dict(executable_sha256=hashlib.sha256(elf_fixture()).hexdigest(), status="Synthetic")
        return source, destination, worker, profile

    def test_adoption_missing_key_is_retryable_without_published_data(self):
        source, lib, worker, profile = self.adoption_fixture()
        before = tree_hashes(source/"hdd0")
        with patch("anyps3_manager.pipeline.profile_for", return_value=(SOURCE/"profiles/fixture.json", profile)):
            with self.assertRaises(ManagerError) as caught:
                worker.adopt(source)
            self.assertEqual(caught.exception.code, "missing-key")
            self.assertFalse(lib.title_dir("BCES00511").exists())
            (source/"app-key.json").write_text("{}")
            (source/"firmware-keys.json").write_text("{}")
            self.assertEqual(worker.adopt(source)["status"], "imported")
        self.assertEqual(tree_hashes(source/"hdd0"), before)
        self.assertEqual(tree_hashes(lib.title_dir("BCES00511")/"userdata/hdd0"), before)

    def test_adoption_publication_resumes_after_interrupted_rename(self):
        source, lib, worker, profile = self.adoption_fixture()
        (source/"app-key.json").write_text("{}")
        (source/"firmware-keys.json").write_text("{}")
        before = tree_hashes(source/"hdd0")
        rename = Path.rename
        root = lib.title_dir("BCES00511")

        def interrupted(path, target):
            if target == root/"eboot":
                raise OSError("Injected interruption between directory renames")
            return rename(path, target)

        with patch("anyps3_manager.pipeline.profile_for", return_value=(SOURCE/"profiles/fixture.json", profile)):
            with patch.object(Path, "rename", interrupted), self.assertRaises(OSError):
                worker.adopt(source)
            self.assertEqual(lib.title("BCES00511")["status"], "incomplete")
            self.assertTrue((root/"disc").is_dir())
            self.assertTrue((root/"adopt-transaction.json").is_file())
            self.assertEqual(worker.adopt(source)["status"], "imported")
        self.assertFalse((root/"adopt-transaction.json").exists())
        self.assertEqual(tree_hashes(source/"hdd0"), before)
        self.assertEqual(tree_hashes(root/"userdata/hdd0"), before)


if __name__ == "__main__":
    unittest.main()
