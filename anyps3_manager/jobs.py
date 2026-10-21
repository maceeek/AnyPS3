"""Detached workers and replayable, append-only JSON Lines events."""

import json
from contextlib import ExitStack
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from . import SCHEMA
from .processes import alive, identity, scope_info, signal_process, stop_tree
from .settings import clean_environment
from .storage import ManagerError, atomic_json, identifier, lock, now, read_json, remove_tree, safe_id

TERMINAL = {"succeeded", "failed", "cancelled"}


def records(lib):
    result = []
    for path in sorted((lib.root/"jobs").glob("*/job.json"), reverse=True):
        try:
            result.append(Job(lib, path.parent.name).record())
        except ManagerError as exc:
            # A concurrent history cleanup may remove a record after globbing.
            if exc.code != "missing-input":
                raise
    return result


def remove(lib, ids=None):
    """Remove history only, after the worker has released its lifetime lock."""
    with lock(lib.root/"locks/jobs-history.lock"), ExitStack() as stack:
        candidates = [Job(lib, value) for value in dict.fromkeys(ids)] if ids is not None else [
            Job(lib, record["id"]) for record in records(lib) if record["state"] in TERMINAL]
        removable, skipped = [], []
        for job in candidates:
            try:
                stack.enter_context(lock(lib.root/"locks"/("job-"+job.id+".lock")))
                record = job.record(check_lock=False)
                process = record.get("process") or read_json(job.path/"spawn.json", {})
                if record["state"] not in TERMINAL or alive(process):
                    raise ManagerError("job-running", "Active jobs cannot be removed; cancel the job first.", 4, job=job.id)
            except ManagerError as exc:
                if exc.code not in ("lock-busy", "job-running") or ids is not None:
                    raise
                skipped.append(job.id)
                continue
            removable.append(job)
        # Validate the entire explicit selection before removing anything.
        for job in removable:
            remove_tree(job.path)
        return dict(removed=[job.id for job in removable], skipped=skipped)


class Job:
    def __init__(self, lib, job_id):
        self.lib = lib
        self.path = lib.root/"jobs"/safe_id(job_id)
        self.id = job_id

    def lifetime_lock(self):
        return lock(self.lib.root/"locks"/("job-"+self.id+".lock"))

    def lifetime_busy(self):
        try:
            with self.lifetime_lock():
                return False
        except ManagerError as exc:
            if exc.code != "lock-busy":
                raise
            return True

    def record(self, check_lock=True):
        record = read_json(self.path/"job.json")
        record["id"] = self.id
        process = record.get("process") or read_json(self.path/"spawn.json", {})
        submission_lost = bool(record["state"] == "queued" and not process and record.get("submitter")
                               and not alive(record["submitter"]))
        if not alive(process) and (record["state"] != "queued" or process or submission_lost):
            children = alive(record.get("tool_process"))
            if record.get("tool_scope"):
                children |= bool(scope_info(record["tool_scope"]).get("pids"))
            if children or (check_lock and self.lifetime_busy()):
                return dict(record, state="running", supervisor_lost=True,
                            recovery_hint="Supervisor exited while a tool or game is still active. Cancel before retrying.")
        if record["state"] not in TERMINAL and ((process and not alive(process)) or
                                               (record["state"] == "running" and not process) or submission_lost):
            # Observational only: an absent PID is not a successful operation.
            record = dict(record, state="failed", error="Worker no longer exists; inspect log and retry.")
        return record

    def update(self, **changes):
        record = read_json(self.path/"job.json")
        record.update(changes, updated=now())
        atomic_json(self.path/"job.json", record)

    def emit(self, event, **data):
        value = dict(schema=SCHEMA, event=event, job=self.id, time=now(), **data)
        with (self.path/"events.jsonl").open("a") as stream:
            stream.write(json.dumps(value, ensure_ascii=False)+"\n")
            stream.flush()
        if event == "stage":
            self.update(stage=data["stage"])

    def cancel(self):
        record = self.record()
        if record["state"] in TERMINAL:
            return record
        atomic_json(self.path/"cancel.json", dict(time=now()))
        process = record.get("process") or read_json(self.path/"spawn.json", {})
        if record["state"] == "queued" and not process:
            # The submitter may still be inside Popen/publication. Its worker
            # consumes this marker before doing any title/tool work.
            return dict(id=self.id, cancellation_requested=True)
        if not signal_process(process, signal.SIGTERM):
            # A killed supervisor does not make its inherited tools cancellable
            # by its old PID. Use the recorded launcher/scope instead.
            if record.get("operation") == "run" and record.get("title") and record.get("run"):
                from .runs import stop
                stop(self.lib, record["title"], run_id=record["run"])
            target, unit = record.get("tool_process"), record.get("tool_scope")
            if alive(target):
                stop_tree(target["pid"], unit)
            elif unit:
                for pid in scope_info(unit).get("pids", []):
                    if identity(pid):
                        stop_tree(pid, unit)
                        break
            try:
                with self.lifetime_lock():
                    self.emit("error", code="cancelled", message="Orphaned job cancelled.", detail={})
                    self.update(state="cancelled", ended=now(), exit_code=5, tool_process=None, tool_scope=None)
            except ManagerError as exc:
                if exc.code != "lock-busy":
                    raise
                raise ManagerError("orphan-active", "Supervisor is gone but an unidentified child still holds the job lock; its history is preserved.", 4) from None
        return dict(id=self.id, cancellation_requested=True)

    def follow(self):
        terminal_event = False
        with (self.path/"events.jsonl").open() as stream:
            while True:
                position = stream.tell()
                line = stream.readline()
                if line:
                    # A write interrupted halfway through is not a complete event.
                    if not line.endswith("\n"):
                        if self.record()["state"] in TERMINAL:
                            raise ManagerError("truncated-events", "Job ended with an incomplete event; inspect its log.")
                        stream.seek(position)
                        time.sleep(.1)
                        continue
                    event = json.loads(line)
                    terminal_event |= event.get("event") in ("result", "error")
                    yield event
                    continue
                record = self.record()
                if record["state"] in TERMINAL:
                    if not terminal_event:
                        yield dict(schema=SCHEMA, event="error", code="worker-interrupted",
                                   message=str(record.get("error", "Worker ended without a result.")), detail={})
                    return
                time.sleep(.1)


def start(lib, operation, arguments, toolchain=None, unscoped=False):
    job = Job(lib, identifier())
    job.path.mkdir(parents=True)
    (job.path/"events.jsonl").touch()
    atomic_json(job.path/"job.json", dict(id=job.id, operation=operation, arguments=arguments,
                state="queued", stage="queued", created=now(), updated=now(), source=str(lib.source),
                toolchain=str(Path(toolchain).resolve()) if toolchain else None, unscoped=unscoped, process=None,
                submitter=identity(os.getpid())))
    env = clean_environment()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    # The same open-file description survives even if the submitter dies after
    # Popen but before spawn.json, or while Python is still importing the worker.
    with job.lifetime_lock() as job_fd:
        try:
            with (job.path/"log.txt").open("ab") as log:
                process = subprocess.Popen([sys.executable, "-P", "-m", "anyps3_manager", "--library",
                            str(lib.root), "--source", str(lib.source), "_worker", job.id, "--lock-fd", str(job_fd)], stdin=subprocess.DEVNULL,
                            stdout=log, stderr=log, env=env, start_new_session=True, pass_fds=(job_fd,))
        except OSError as exc:
            error = ManagerError("worker-start", "Could not start the job worker; retry the operation.", 6)
            job.emit("error", code=error.code, message=str(error), detail={})
            job.update(state="failed", ended=now(), exit_code=error.exit_code, error=error.record())
            raise error from exc
        # The worker owns job.json; this separate token avoids competing writes.
        atomic_json(job.path/"spawn.json", identity(process.pid))
    # Library callers may outlive the CLI invocation. Reap our worker without
    # keeping a short-lived submitting client alive until the job finishes.
    threading.Thread(target=process.wait, daemon=True).start()
    return dict(job=job.id, state="queued")


def work(lib, job_id, lock_fd=None):
    path = lib.root/"locks"/("job-"+safe_id(job_id)+".lock")
    if lock_fd is None:
        with lock(path) as job_fd:
            return _work(lib, job_id, job_fd)
    # Never accept an unrelated inherited descriptor as a lifecycle lock.
    expected, actual = path.stat(), os.fstat(lock_fd)
    if lock_fd <= 2 or (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
        raise ManagerError("invalid-job-lock", "The inherited job lock does not match this job.")
    try:
        return _work(lib, job_id, lock_fd)
    finally:
        os.close(lock_fd)


def _work(lib, job_id, job_fd):
    from .pipeline import Pipeline
    from .runs import run
    from .processes import Toolchain

    job = Job(lib, job_id)
    job.lock_fd = job_fd
    record = read_json(job.path/"job.json")

    def cancelled(signum, frame):
        # Repeated cancel requests must not interrupt child cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise ManagerError("cancelled", "Job cancelled.", 5)

    signal.signal(signal.SIGTERM, cancelled)
    job.update(state="running", process=identity(os.getpid()))
    try:
        if (job.path/"cancel.json").exists():
            cancelled(None, None)
        pipeline = Pipeline(lib, Toolchain(lib.source, record["toolchain"]), job, record["unscoped"])
        args = record["arguments"]
        if record["operation"] == "build":
            result = pipeline.build(**args)
        elif record["operation"] == "import":
            result = pipeline.import_iso(**args)
        elif record["operation"] == "adopt":
            result = pipeline.adopt(**args)
        elif record["operation"] == "run":
            result = run(lib, job=job, unscoped=record["unscoped"], **args)
        elif record["operation"] == "firmware":
            result = pipeline.firmware(Path(args["path"]))
        else:
            raise ManagerError("unknown-job", "Unknown job operation.")
        job.emit("result", **result)
        job.update(state="succeeded", result=result, ended=now())
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        error = exc if isinstance(exc, ManagerError) else ManagerError("internal-error", str(exc))
        job.emit("error", code=error.code, message=str(error), detail=error.detail)
        job.update(state="cancelled" if error.exit_code == 5 else "failed",
                   error=error.record(), exit_code=error.exit_code, ended=now())
        return error.exit_code
