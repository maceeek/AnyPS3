"""Player launches, persistent run evidence and conservative STOP reporting."""

import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import time

from .intro_skip import IntroSkip, recipe
from .processes import alive, identity, scope_command, scope_info, stop_tree
from .saves import backup
from .settings import effective, run_environment
from .storage import ManagerError, atomic_json, identifier, now, read_json, safe_id, sha256


def records(lib, title):
    lib.title(title)
    return [read_json(p) for p in sorted((lib.title_dir(title)/"runs").glob("*/run.json"), reverse=True)]


def stop(lib, title, run_id=None):
    running = [r for r in records(lib, title) if r["state"] == "running" and (run_id is None or r["id"] == run_id)]
    if not running:
        return dict(title=title, stopped=False)
    record = running[0]
    atomic_json(lib.title_dir(title)/"runs"/record["id"]/"stop-request.json", dict(time=now()))
    # The worker observes this even if Stop arrived before Popen. If its
    # supervisor died, stop the surviving recorded tree/scope ourselves.
    target = record.get("program_process") or record.get("launcher_process")
    if alive(target):
        stop_tree(target["pid"], record.get("scope"), effective(lib, title)["run.stop_timeout"])
    elif record.get("scope"):
        for pid in scope_info(record["scope"]).get("pids", []):
            if identity(pid):
                stop_tree(pid, record["scope"], effective(lib, title)["run.stop_timeout"])
                break
    if not alive(record.get("manager_process")):
        record.update(state="finished", ended=now(), stopped_by_user=True, exit_code=None,
                      supervisor_lost=True)
        record_evidence(lib, title, record)
        prune_records(lib, title, effective(lib, title)["run.keep"])
    return dict(title=title, stop_requested=True, run=record["id"])


class RunSummary:
    """Shared line parser for incremental live evidence and the final summary."""
    def __init__(self):
        self.rates = []
        self.stop_line = self.error = None
        self.flips = self.captured_frames = 0
        self.pending = b""
        self.listeners = []

    def line(self, line):
        for listener in self.listeners:
            listener(line)
        if "STOP pc=" in line:
            self.stop_line = line.rstrip()
        if "error" in line.lower() or "terminate called" in line:
            self.error = line.rstrip()
        if "PERF interval" in line:
            match = re.search(r"\bflips_per_s=([0-9]+(?:\.[0-9]+)?)(?=\s|$)", line)
            if match:
                self.rates.append(float(match[1]))
        if "PERF interval" in line or "RSX captured_frames=" in line:
            match = re.search(r"\bflips=(\d+)(?=\s|$)", line)
            if match:
                self.flips = max(self.flips, int(match[1]))
        match = re.search(r"\bRSX captured(?: frame|_frames)=(\d+)(?=\s|$)", line)
        if match:
            self.captured_frames = max(self.captured_frames, int(match[1]))

    def read_new(self, stream):
        # Keep the file offset and incomplete line across polls; consume each
        # complete line once, including lines split across writes/UTF-8 bytes.
        while chunk := stream.read(65536):
            lines = (self.pending+chunk).split(b"\n")
            self.pending = lines.pop()
            for line in lines:
                self.line(line.decode(errors="replace"))

    def result(self):
        return dict(stop=self.stop_line, last_error=self.error,
                    fps_mean=statistics.mean(self.rates) if self.rates else None,
                    fps_median=statistics.median(self.rates) if self.rates else None,
                    fps_samples=len(self.rates), flips=self.flips, captured_frames=self.captured_frames)


def summarize(stderr):
    summary = RunSummary()
    with Path(stderr).open(errors="replace") as stream:
        for line in stream:
            summary.line(line)
    return summary.result()


def record_evidence(lib, title, record):
    """Publish evidence for both supervised and recovered run finalization."""
    directory = lib.title_dir(title)/"runs"/safe_id(record["id"])
    stderr = directory/"stderr.log"
    if stderr.exists():
        record.update(summarize(stderr))
    # Rates alone do not prove a flip, menu or play.
    booted = record.get("flips", 0) > 0
    if booted:
        record["verification_status"] = "verified-boot"
    atomic_json(directory/"run.json", record)
    current = lib.title(title)
    if booted and current.get("active_build") == record.get("build") and current.get("status") != "verified-play":
        lib.update_title(title, status="verified-boot")


def prune_records(lib, title, keep):
    """Retain N completed runs without counting or removing active records."""
    completed = [record for record in records(lib, title) if record["state"] == "finished"]
    for old in completed[keep:]:
        shutil.rmtree(lib.title_dir(title)/"runs"/safe_id(old["id"]))


def run(lib, title, build=None, overrides=None, unscoped=False, job=None):
    with lib.title_lock(title) as title_fd, lib.run_lock(title) as run_fd:
        current = lib.title(title)
        build_id = safe_id(build or current.get("active_build") or "")
        root = lib.title_dir(title)
        build_path = root/"builds"/build_id
        build_record = read_json(build_path/"record.json")
        if build_record["status"] not in ("built", "verified-boot", "verified-play"):
            raise ManagerError("invalid-build", "The selected build has not passed verification.")
        verified = read_json(build_path/"verification.json")
        for filename, key in (("program", "program_sha256"), ("program.elf", "elf_sha256")):
            if sha256(build_path/"native"/filename) != verified[key]:
                raise ManagerError("build-changed", f"Verified artifact changed: {filename}")
        settings = effective(lib, title, overrides)
        run_id = identifier()
        run_dir = root/"runs"/run_id
        unit = "anyps3-"+title.lower()+"-"+run_id.lower()
        command = scope_command([str(build_path/"native/program"), str(build_path/"native/program.elf")],
                                 unit, settings["run.memory_max"], settings["run.cpus"], unscoped)
        username = root/"userdata/hdd0/home/00000001/localusername"
        if not username.is_file() or not 1 <= username.stat().st_size <= 63:
            raise ManagerError("invalid-user", "The persistent localusername must contain 1–63 bytes.")
        if settings["saves.backup_before_run"]:
            backup(lib, title, reason="run", automatic=True)
        if (root/"cache").exists():
            shutil.rmtree(root/"cache")
        (root/"cache").mkdir()
        run_dir.mkdir(parents=True)
        env = run_environment(lib, title, settings, build_record["available_env"], run_dir)
        record = dict(id=run_id, title=title, build=build_id, started=now(), state="running",
                      scope=unit if not unscoped else None, settings=settings, manager_process=identity(os.getpid()),
                      program_process=None, launcher_process=None, command=command)
        live_summary = RunSummary()
        record.update(live_summary.result())
        skip = None
        if "ANYPS3_PAD_INJECT" in env:
            skip = IntroSkip(env["ANYPS3_PAD_INJECT"], recipe(title))
            live_summary.listeners.append(skip.line)
            record["intro_skip"] = skip.summary()
        atomic_json(run_dir/"run.json", record)
        if job:
            job.emit("stage", stage="run", index=1, count=1)
            job.update(run=run_id, title=title, tool_scope=unit if not unscoped else None)
        started = time.monotonic()
        info = {}
        process = None
        try:
            with (run_dir/"stdout.log").open("wb") as stdout, (run_dir/"stderr.log").open("wb") as stderr, \
                 (run_dir/"stderr.log").open("rb") as evidence:
                if (run_dir/"stop-request.json").exists():
                    raise ManagerError("cancelled", "Launch cancelled before starting.", 5)
                job_fds = (job.lock_fd,) if job and hasattr(job, "lock_fd") else ()
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env, pass_fds=(title_fd, run_fd, *job_fds))
                record["launcher_process"] = identity(process.pid)
                if job:
                    job.update(tool_process=record["launcher_process"])
                if unscoped:
                    record["program_process"] = identity(process.pid)
                atomic_json(run_dir/"run.json", record)
                next_evidence = time.monotonic()
                while process.poll() is None:
                    if (run_dir/"stop-request.json").exists():
                        raise ManagerError("cancelled", "Run stopped by the user.", 5)
                    if not unscoped:
                        info.update(scope_info(unit))
                        for pid in info.get("pids", []):
                            try:
                                executable = Path(f"/proc/{pid}/exe").resolve()
                                if executable == (build_path/"native/program").resolve():
                                    record["program_process"] = identity(pid)
                            except OSError:
                                pass
                    # The intro-skip driver reacts to each fresh report promptly.
                    if skip and skip.active:
                        live_summary.read_new(evidence)
                        skip.tick()
                    if time.monotonic() >= next_evidence:
                        live_summary.read_new(evidence)
                        record.update(live_summary.result())
                        if skip:
                            record["intro_skip"] = skip.summary()
                        atomic_json(run_dir/"run.json", record)
                        next_evidence = time.monotonic()+1
                    time.sleep(.25)
        except BaseException:
            if process is not None and process.poll() is None:
                stop_tree(process.pid, None if unscoped else unit, settings["run.stop_timeout"])
                process.wait()
            raise
        finally:
            exited = process is None or process.poll() is not None
            if skip:
                skip.finish("run-ended")
                record["intro_skip"] = skip.summary()
            record.update(ended=now() if exited else None, state="finished" if exited else "running",
                          exit_code=process.poll() if process else None,
                          play_seconds=time.monotonic()-started, scope_result=info,
                          oom_killed=info.get("memory_events", {}).get("oom_kill", "0") != "0",
                          stopped_by_user=(run_dir/"stop-request.json").exists())
            record_evidence(lib, title, record)
            lib.update_title(title, last_played=record["started"],
                             play_seconds=current.get("play_seconds", 0)+record["play_seconds"])
            prune_records(lib, title, settings["run.keep"])
        if record["stop"]:
            raise ManagerError("guest-stop", record["stop"], 3, run=run_id, log=str(run_dir/"stderr.log"))
        if record["exit_code"]:
            if record["stopped_by_user"]:
                raise ManagerError("cancelled", "Run stopped by the user.", 5, run=run_id)
            raise ManagerError("out-of-memory" if record["oom_killed"] else "run-ended",
                               f"Game exited with {record['exit_code']}.", run=run_id, log=str(run_dir/"stderr.log"))
        return record
