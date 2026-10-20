"""Detached workers and replayable, append-only JSON Lines events."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from . import SCHEMA
from .processes import alive, identity, signal_process, stop_process
from .settings import clean_environment
from .storage import ManagerError, atomic_json, identifier, now, read_json, safe_id

TERMINAL = {"succeeded", "failed", "cancelled"}


class Job:
    def __init__(self, lib, job_id):
        self.lib = lib
        self.path = lib.root/"jobs"/safe_id(job_id)
        self.id = job_id

    def record(self):
        record = read_json(self.path/"job.json")
        process = record.get("process") or read_json(self.path/"spawn.json", {})
        if record["state"] not in TERMINAL and process and not alive(process):
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
        signal_process(record.get("process") or read_json(self.path/"spawn.json", {}), signal.SIGTERM)
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
                toolchain=str(Path(toolchain).resolve()) if toolchain else None, unscoped=unscoped, process=None))
    env = clean_environment()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    with (job.path/"log.txt").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-P", "-m", "anyps3_manager", "--library",
                    str(lib.root), "--source", str(lib.source), "_worker", job.id], stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, env=env, start_new_session=True)
    # The worker owns job.json once spawned. A bootstrap token avoids parent/worker
    # writes racing and lets cancellation identify queued workers.
    atomic_json(job.path/"spawn.json", identity(process.pid))
    # Library callers may outlive the CLI invocation. Reap our worker without
    # keeping a short-lived submitting client alive until the job finishes.
    threading.Thread(target=process.wait, daemon=True).start()
    return dict(job=job.id, state="queued")


def work(lib, job_id):
    from .pipeline import Pipeline
    from .runs import run
    from .processes import Toolchain

    job = Job(lib, job_id)
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
