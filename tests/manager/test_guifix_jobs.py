"""Orphan worker lifetime and failed submission regressions, without a game build."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from anyps3_manager import jobs, pipeline
from anyps3_manager.processes import alive, identity, signal_process, stop_tree
from anyps3_manager.storage import Library, ManagerError, atomic_json, read_json, remove_tree


class JobLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="anyps3-guifix-jobs-")
        self.root = Path(self.temp.name)
        self.lib = Library(self.root/"library")
        self.lib.init()
        self.addCleanup(self.temp.cleanup)

    def wait(self, condition):
        end = time.monotonic()+10
        while not condition() and time.monotonic() < end:
            time.sleep(.02)
        self.assertTrue(condition())

    def test_submission_failure_is_terminal_removable_and_does_not_block_title(self):
        self.lib.register(dict(id="BCES00511", status="imported", active_build=None))
        with patch("anyps3_manager.jobs.subprocess.Popen", side_effect=OSError("synthetic process limit")):
            with self.assertRaises(ManagerError) as error:
                jobs.start(self.lib, "build", dict(title="BCES00511"))
        self.assertEqual(error.exception.code, "worker-start")
        record, = jobs.records(self.lib)
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["exit_code"], 6)
        self.assertFalse(pipeline.title_details(self.lib, "BCES00511")["busy"])
        self.assertEqual(jobs.remove(self.lib, [record["id"]])["removed"], [record["id"]])

    def test_orphan_run_cancellation_is_bound_to_original_run(self):
        self.lib.register(dict(id="BCES00511", status="built", active_build=None))
        old = self.lib.title_dir("BCES00511")/"runs/old/run.json"
        new = self.lib.title_dir("BCES00511")/"runs/new/run.json"
        atomic_json(old, dict(id="old", state="finished"))
        atomic_json(new, dict(id="new", state="running", manager_process=identity(os.getpid())))
        job = jobs.Job(self.lib, "old-run-job")
        job.path.mkdir()
        stale = dict(id=job.id, operation="run", title="BCES00511", run="old", state="running", process=None)
        atomic_json(job.path/"job.json", stale)
        (job.path/"events.jsonl").touch()
        # The old game exits after observation; another game now owns the title.
        with patch.object(job, "record", return_value=stale):
            self.assertTrue(job.cancel()["cancellation_requested"])
        self.assertFalse((new.parent/"stop-request.json").exists())
        self.assertEqual(read_json(new)["state"], "running")

    def test_history_cleanup_does_not_lock_unrelated_title_metadata(self):
        self.lib.register(dict(id="BCES00511", status="imported"))
        job = jobs.Job(self.lib, "finished-history")
        job.path.mkdir()
        atomic_json(job.path/"job.json", dict(id=job.id, state="failed"))
        def remove_during_update(path):
            self.lib.update_title("BCES00511", status="built")
            remove_tree(path)
        with patch("anyps3_manager.jobs.remove_tree", side_effect=remove_during_update):
            self.assertEqual(jobs.remove(self.lib, [job.id])["removed"], [job.id])
        self.assertEqual(self.lib.title("BCES00511")["status"], "built")

    def test_worker_holds_lock_before_spawn_publication_and_python_worker_entry(self):
        marker = self.root/"bootstrap-child.pid"
        submitter = self.root/"interrupted-submitter.py"
        submitter.write_text("import os,sys,time,subprocess\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            "from anyps3_manager import jobs\n"
            "from anyps3_manager.storage import Library\n"
            "real_popen=subprocess.Popen\n"
            "real_atomic=jobs.atomic_json\n"
            "def delayed_worker(args,**kwargs):\n"
            " p=real_popen([sys.executable,'-c','import time;time.sleep(60)'],**kwargs)\n"
            " Path(sys.argv[2]).write_text(str(p.pid))\n"
            " return p\n"
            "def interrupted(path,data):\n"
            " if Path(path).name=='spawn.json': os._exit(42)\n"
            " return real_atomic(path,data)\n"
            "with patch.object(jobs.subprocess,'Popen',delayed_worker),patch.object(jobs,'atomic_json',interrupted):\n"
            " jobs.start(Library(sys.argv[1]),'build',dict(title='BCES00511'),unscoped=True)\n")
        process = subprocess.Popen([sys.executable, str(submitter), str(self.lib.root), str(marker)])
        child = None
        try:
            self.assertEqual(process.wait(timeout=10), 42)
            self.assertTrue(marker.exists())
            child = identity(int(marker.read_text()))
            self.assertTrue(alive(child))
            record, = jobs.records(self.lib)
            self.assertEqual(record["state"], "running")
            self.assertTrue(record["supervisor_lost"])
            job = jobs.Job(self.lib, record["id"])
            self.assertFalse((job.path/"spawn.json").exists())
            with self.assertRaises(ManagerError):
                jobs.remove(self.lib, [job.id])
            self.assertEqual(jobs.remove(self.lib)["removed"], [])
            signal_process(child, signal.SIGKILL)
            self.wait(lambda: not alive(child))
            self.assertEqual(job.record()["state"], "failed")
            self.assertEqual(jobs.remove(self.lib, [job.id])["removed"], [job.id])
        finally:
            if alive(child):
                stop_tree(child["pid"])
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_cancel_during_bootstrap_records_request_without_terminalizing(self):
        job = jobs.Job(self.lib, "bootstrapping")
        job.path.mkdir()
        atomic_json(job.path/"job.json", dict(id=job.id, state="queued", process=None, submitter=identity(os.getpid())))
        (job.path/"events.jsonl").touch()
        self.assertTrue(job.cancel()["cancellation_requested"])
        self.assertTrue((job.path/"cancel.json").exists())
        self.assertEqual(job.record()["state"], "queued")
        with self.assertRaises(ManagerError):
            jobs.remove(self.lib, [job.id])

    def test_dead_submitter_before_spawn_is_failed_and_removable(self):
        job = jobs.Job(self.lib, "submission-interrupted")
        job.path.mkdir()
        atomic_json(job.path/"job.json", dict(id=job.id, state="queued", process=None,
                    submitter=dict(pid=999999999, start_ticks="1", boot_id="dead")))
        (job.path/"events.jsonl").touch()
        self.assertEqual(job.record()["state"], "failed")
        self.assertEqual(jobs.remove(self.lib, [job.id])["removed"], [job.id])

    def test_unknown_inherited_child_keeps_history_and_cannot_claim_cancellation(self):
        job = jobs.Job(self.lib, "unknown-child")
        job.path.mkdir()
        atomic_json(job.path/"job.json", dict(id=job.id, state="running", process=None))
        (job.path/"events.jsonl").touch()
        with job.lifetime_lock():
            self.assertEqual(job.record()["state"], "running")
            self.assertTrue(job.record()["supervisor_lost"])
            self.assertEqual(jobs.remove(self.lib)["removed"], [])
            with self.assertRaises(ManagerError) as error:
                job.cancel()
            self.assertEqual(error.exception.code, "orphan-active")
            self.assertTrue((job.path/"job.json").exists())
        self.assertEqual(job.record()["state"], "failed")
        self.assertEqual(jobs.remove(self.lib, [job.id])["removed"], [job.id])

    def test_killed_supervisor_keeps_live_tool_and_cancel_stops_descendants(self):
        self.orphan_tool(unscoped=True)

    def test_scoped_orphan_tool_is_retained_and_cancelled(self):
        self.orphan_tool(unscoped=False)

    def orphan_tool(self, unscoped):
        from anyps3_manager.keys import set_key
        pup = self.root/"update.pup"
        pup.write_bytes(b"synthetic firmware")
        key = self.root/"private-fixture.json"
        atomic_json(key, dict(erk=bytes(range(32)).hex(), riv=bytes(range(16)).hex(), pup_hmac=bytes(range(64)).hex()))
        set_key(self.lib, "pup", key)
        child_pid = self.root/"child.pid"
        script = self.root/"tool.py"
        script.write_text("import os,subprocess,sys,time\n"
                          "assert os.environ['SDL_AUDIODRIVER']=='dummy'\n"
                          "assert os.environ['ANYPS3_AUDIO_SINK']=='null'\n"
                          "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                          "open(sys.argv[1],'w').write(str(p.pid))\n"
                          "time.sleep(60)\n")
        tools = self.root/"toolchain.json"
        atomic_json(tools, dict(firmware=[sys.executable, str(script), str(child_pid)]))
        submitted = jobs.start(self.lib, "firmware", dict(path=str(pup)), tools, unscoped=unscoped)
        job = jobs.Job(self.lib, submitted["job"])
        try:
            self.wait(child_pid.exists)
            self.wait(lambda: bool(read_json(job.path/"job.json").get("tool_process")))
            initial = read_json(job.path/"job.json")
            worker, tool = initial["process"], initial["tool_process"]
            child = identity(int(child_pid.read_text()))
            self.assertTrue(alive(child))
            signal_process(worker, signal.SIGKILL)
            self.wait(lambda: not alive(worker))
            orphan = job.record()
            self.assertEqual(orphan["state"], "running")
            self.assertTrue(orphan["supervisor_lost"])
            self.assertTrue(alive(tool))
            with self.assertRaises(ManagerError):
                jobs.remove(self.lib, [job.id])
            self.assertEqual(jobs.remove(self.lib)["removed"], [])
            self.assertTrue(job.cancel()["cancellation_requested"])
            self.wait(lambda: not alive(tool) and not alive(child))
            self.assertEqual(job.record()["state"], "cancelled")
            self.assertEqual(jobs.remove(self.lib, [job.id])["removed"], [job.id])
        finally:
            if (job.path/"job.json").exists():
                record = read_json(job.path/"job.json")
                for name in ("process", "tool_process"):
                    if alive(record.get(name)):
                        stop_tree(record[name]["pid"])
                remove_tree(job.path)


if __name__ == "__main__":
    unittest.main()
