"""Recovery/history behavior against synthetic discs and irreplaceable save markers."""
import os
from unittest.mock import patch
import unittest

from anyps3_manager import jobs, pipeline
from anyps3_manager.processes import identity
from anyps3_manager.storage import ManagerError, atomic_json, tree_hashes
from tests.manager import test_manager as fixtures

SOURCE = fixtures.SOURCE
iso_fixture = fixtures.iso_fixture


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.ManagerTests.setUp
    tearDown = fixtures.ManagerTests.tearDown
    pipeline = fixtures.ManagerTests.pipeline
    import_title = fixtures.ManagerTests.import_title
    cli = fixtures.ManagerTests.cli
    adoption_fixture = fixtures.ManagerTests.adoption_fixture

    def failed_import(self):
        worker = self.pipeline()
        with patch.object(worker, "choose_firmware", side_effect=ManagerError("missing-key", "Synthetic missing PUP key")):
            with self.assertRaises(ManagerError):
                worker.import_iso(self.iso, no_build=True)
        worker.job.update(state="failed")
        return self.lib.title_dir("BCES00511")

    def test_failed_import_is_resumable_and_keeps_verified_extraction(self):
        root = self.failed_import()
        stage = next(root.glob(".import-*"))
        marker = stage/"disc/PS3_GAME/PARAM.SFO"
        inode = marker.stat().st_ino
        detail = pipeline.title_details(self.lib, "BCES00511")
        self.assertEqual(detail["recovery"]["arguments"]["iso"], str(self.iso))
        self.assertTrue(detail["resume_available"])
        result = self.pipeline().import_iso(self.iso, no_build=True)
        self.assertEqual(result["status"], "imported")
        self.assertEqual((root/"disc/PS3_GAME/PARAM.SFO").stat().st_ino, inode)
        self.assertEqual(pipeline.title_details(self.lib, "BCES00511")["next_action"], "build")

    def test_foreground_run_without_job_is_visible_and_blocks_conflicts(self):
        root = self.import_title()
        path = root/"runs/foreground/run.json"
        atomic_json(path, dict(id="foreground", state="running", manager_process=identity(os.getpid())))
        detail = pipeline.title_details(self.lib, "BCES00511")
        self.assertTrue(detail["running"])
        self.assertTrue(detail["busy"])
        self.assertEqual(detail["busy_jobs"], [])
        atomic_json(path, dict(id="foreground", state="finished"))
        self.assertFalse(pipeline.title_details(self.lib, "BCES00511")["running"])

    def test_resume_cli_replays_source_and_options(self):
        self.failed_import()
        with patch("anyps3_manager.jobs.start", return_value={"job": "resumed"}) as submit:
            code, result = self.cli("titles", "resume", "BCES00511")
        self.assertEqual((code, result["job"]), (0, "resumed"))
        self.assertEqual(submit.call_args.args[1:3], ("import", dict(iso=str(self.iso), no_build=True, replace_disc=None, firmware=None)))

    def test_legacy_incomplete_import_recovers_source_from_job(self):
        self.failed_import()
        record = self.lib.title("BCES00511")
        record.pop("recovery")
        record.pop("iso_path")
        atomic_json(self.lib.title_dir("BCES00511")/"title.json", record)
        path = self.lib.root/"jobs/legacy"
        path.mkdir()
        atomic_json(path/"job.json", dict(id="legacy", state="failed", operation="import", arguments=dict(iso=str(self.iso), no_build=True)))
        detail = pipeline.title_details(self.lib, "BCES00511")
        self.assertTrue(detail["resume_available"])
        self.assertEqual(detail["recovery"]["arguments"]["iso"], str(self.iso))

    def test_different_iso_may_replace_only_disposable_incomplete_title(self):
        root = self.failed_import()
        old = next(root.glob(".import-*"))
        self.iso.write_bytes(iso_fixture("02.00"))
        result = self.pipeline().import_iso(self.iso, no_build=True)
        self.assertEqual(result["status"], "imported")
        self.assertFalse(old.exists())
        self.assertEqual(self.lib.title("BCES00511")["version"], "02.00")

    def test_replacement_refuses_userdata_builds_and_live_job(self):
        root = self.failed_import()
        self.iso.write_bytes(iso_fixture("02.00"))
        for protected, code in (("userdata/progress", "retained-userdata"), ("builds/old/program", "retained-builds")):
            with self.subTest(protected=protected):
                marker = root/protected
                marker.parent.mkdir(parents=True)
                marker.write_bytes(b"precious")
                before = tree_hashes(root)
                with self.assertRaises(ManagerError) as error:
                    self.pipeline().import_iso(self.iso, no_build=True)
                self.assertEqual(error.exception.code, code)
                self.assertEqual(tree_hashes(root).get(protected), before[protected])
                marker.unlink()
                marker.parent.rmdir()
                if protected.startswith("builds"):
                    (root/"builds").rmdir()
        live = jobs.Job(self.lib, "queued-original")
        live.path.mkdir()
        atomic_json(live.path/"job.json", dict(id=live.id, state="queued", arguments=dict(title="BCES00511")))
        with self.assertRaises(ManagerError) as error:
            self.pipeline().import_iso(self.iso, no_build=True)
        self.assertEqual(error.exception.code, "title-busy")
        self.assertTrue(pipeline.title_details(self.lib, "BCES00511")["busy"])
        self.assertEqual(self.cli("titles", "remove", "BCES00511")[1]["code"], "title-busy")

    def test_adoption_replaces_failed_import_and_cleans_staging(self):
        source, lib, worker, profile = self.adoption_fixture()
        atomic_json(source/"app-key.json", dict(type=4, revision=1, erk=bytes(range(32)).hex(), riv=bytes(range(16)).hex()))
        atomic_json(source/"firmware-keys.json", dict(erk=bytes(range(32)).hex(), riv=bytes(range(16)).hex(), pup_hmac=bytes(range(64)).hex()))
        root = lib.title_dir("BCES00511")
        lib.register(dict(id="BCES00511", status="incomplete", iso_sha256="old", active_build=None))
        (root/".import-old/disc").mkdir(parents=True)
        with patch("anyps3_manager.pipeline.profile_for", return_value=(SOURCE/"profiles/fixture.json", profile)):
            self.assertEqual(worker.adopt(source)["status"], "imported")
        self.assertFalse((root/".import-old").exists())
        self.assertEqual(tree_hashes(root/"userdata/hdd0"), tree_hashes(source/"hdd0"))
        with self.assertRaises(ManagerError) as error:
            worker.adopt(source)
        self.assertEqual(error.exception.code, "already-exists")

    def test_failed_adoption_can_be_replaced_by_import_without_published_userdata(self):
        root = self.lib.title_dir("BCES00511")
        self.lib.register(dict(id="BCES00511", status="incomplete", adopted_from="/synthetic/old", active_build=None))
        (root/".adopt-staging/disc").mkdir(parents=True)
        result = self.pipeline().import_iso(self.iso, no_build=True)
        self.assertEqual(result["status"], "imported")
        self.assertFalse((root/".adopt-staging").exists())


class JobRemovalTests(unittest.TestCase):
    setUp = fixtures.ManagerTests.setUp
    tearDown = fixtures.ManagerTests.tearDown
    cli = fixtures.ManagerTests.cli

    def job(self, name, state, **extra):
        job = jobs.Job(self.lib, name)
        job.path.mkdir()
        atomic_json(job.path/"job.json", dict(id=name, state=state, **extra))
        (job.path/"events.jsonl").touch()
        return job

    def test_multiremove_deletes_only_terminal_history_never_title(self):
        self.lib.register(dict(id="BCES00511", status="incomplete"))
        marker = self.lib.title_dir("BCES00511")/"userdata/progress"
        marker.parent.mkdir()
        marker.write_text("precious")
        for name, state in (("done", "succeeded"), ("failed", "failed"), ("cancelled", "cancelled")):
            self.job(name, state)
        code, result = self.cli("jobs", "remove", "done", "failed", "cancelled")
        self.assertEqual(code, 0)
        self.assertEqual(set(result["removed"]), {"done", "failed", "cancelled"})
        self.assertEqual(marker.read_text(), "precious")
        self.assertEqual(self.lib.title("BCES00511")["status"], "incomplete")

    def test_mixed_selection_is_atomic_and_rejects_running_or_queued(self):
        done = self.job("done", "failed")
        for name, state in (("queued", "queued"), ("running", "running")):
            self.job(name, state, process=identity(os.getpid()))
            code, result = self.cli("jobs", "remove", "done", name)
            self.assertEqual((code, result["code"]), (4, "job-running"))
            self.assertTrue(done.path.exists())

    def test_clear_retains_active_jobs_and_live_terminal_worker(self):
        self.job("done", "failed")
        self.job("running", "running", process=identity(os.getpid()))
        self.job("finishing", "succeeded", process=identity(os.getpid()))
        code, result = self.cli("jobs", "clear")
        self.assertEqual(code, 0)
        self.assertEqual(result["removed"], ["done"])
        self.assertTrue((self.lib.root/"jobs/running/job.json").is_file())
        self.assertTrue((self.lib.root/"jobs/finishing/job.json").is_file())

    def test_orphan_worker_is_failed_and_removable(self):
        self.job("orphan", "running", process=dict(pid=999999999, start_ticks="1", boot_id="dead"))
        self.assertEqual(self.cli("jobs", "remove", "orphan")[0], 0)

    def test_cleanup_obeys_worker_lifetime_lock(self):
        from anyps3_manager.storage import lock
        job = self.job("finishing", "succeeded")
        with lock(self.lib.root/"locks/job-finishing.lock"):
            self.assertEqual(self.cli("jobs", "remove", job.id)[0], 4)
            self.assertEqual(self.cli("jobs", "clear")[1]["removed"], [])
        self.assertEqual(self.cli("jobs", "remove", job.id)[0], 0)

    def test_job_command_arity_and_id_validation(self):
        for args in (("remove",), ("clear", "extra"), ("list", "extra"), ("show", "a", "b"), ("remove", "../title")):
            self.assertEqual(self.cli("jobs", *args)[0], 2)


if __name__ == "__main__":
    unittest.main()
