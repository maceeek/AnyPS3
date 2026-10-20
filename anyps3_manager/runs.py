"""Player launches, persistent run evidence and conservative STOP reporting."""

import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import time

from .processes import alive, identity, scope_command, scope_info, stop_tree
from .saves import backup
from .settings import effective, run_environment
from .storage import ManagerError, atomic_json, identifier, now, read_json, safe_id, sha256


def records(lib, title):
    lib.title(title)
    return [read_json(p) for p in sorted((lib.title_dir(title)/"runs").glob("*/run.json"), reverse=True)]


def stop(lib, title):
    running = [r for r in records(lib, title) if r["state"] == "running"]
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
        stderr = lib.title_dir(title)/"runs"/record["id"]/"stderr.log"
        if stderr.exists():
            record.update(summarize(stderr))
        atomic_json(lib.title_dir(title)/"runs"/record["id"]/"run.json", record)
    return dict(title=title, stop_requested=True, run=record["id"])


def summarize(stderr):
    rates, stop_line, error = [], None, None
    with Path(stderr).open(errors="replace") as stream:
        for line in stream:
            if "STOP pc=" in line:
                stop_line = line.rstrip()
            if "error" in line.lower() or "terminate called" in line:
                error = line.rstrip()
            if "PERF interval" in line:
                match = re.search(r"flips_per_s=([0-9.]+)", line)
                if match:
                    rates.append(float(match[1]))
    return dict(stop=stop_line, last_error=error, fps_mean=statistics.mean(rates) if rates else None,
                fps_median=statistics.median(rates) if rates else None, fps_samples=len(rates))


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
        atomic_json(run_dir/"run.json", record)
        if job:
            job.emit("stage", stage="run", index=1, count=1)
            job.update(run=run_id, title=title)
        started = time.monotonic()
        info = {}
        process = None
        try:
            with (run_dir/"stdout.log").open("wb") as stdout, (run_dir/"stderr.log").open("wb") as stderr:
                if (run_dir/"stop-request.json").exists():
                    raise ManagerError("cancelled", "Launch cancelled before starting.", 5)
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env, pass_fds=(title_fd, run_fd))
                record["launcher_process"] = identity(process.pid)
                if unscoped:
                    record["program_process"] = identity(process.pid)
                atomic_json(run_dir/"run.json", record)
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
                        atomic_json(run_dir/"run.json", record)
                    time.sleep(.25)
        except BaseException:
            if process is not None and process.poll() is None:
                stop_tree(process.pid, None if unscoped else unit, settings["run.stop_timeout"])
                process.wait()
            raise
        finally:
            exited = process is None or process.poll() is not None
            record.update(ended=now() if exited else None, state="finished" if exited else "running",
                          exit_code=process.poll() if process else None,
                          play_seconds=time.monotonic()-started, scope_result=info,
                          oom_killed=info.get("memory_events", {}).get("oom_kill", "0") != "0",
                          stopped_by_user=(run_dir/"stop-request.json").exists())
            if (run_dir/"stderr.log").exists():
                record.update(summarize(run_dir/"stderr.log"))
            atomic_json(run_dir/"run.json", record)
            lib.update_title(title, last_played=record["started"],
                             play_seconds=current.get("play_seconds", 0)+record["play_seconds"])
        # A flip is boot evidence only. Never infer menu/play from frame counts.
        if record["fps_samples"] and record["fps_mean"] > 0:
            record["verification_status"] = "verified-boot"
            atomic_json(run_dir/"run.json", record)
            if current.get("active_build") == build_id:
                lib.update_title(title, status="verified-boot")
        for old in records(lib, title)[settings["run.keep"]:]:
            if old["state"] == "finished":
                shutil.rmtree(root/"runs"/old["id"])
        if record["stop"]:
            raise ManagerError("guest-stop", record["stop"], 3, run=run_id, log=str(run_dir/"stderr.log"))
        if record["exit_code"]:
            if record["stopped_by_user"]:
                raise ManagerError("cancelled", "Run stopped by the user.", 5, run=run_id)
            raise ManagerError("out-of-memory" if record["oom_killed"] else "run-ended",
                               f"Game exited with {record['exit_code']}.", run=run_id, log=str(run_dir/"stderr.log"))
        return record
