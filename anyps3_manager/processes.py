"""Scoped subprocesses and PID identity checks (never signal a reused PID)."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .settings import clean_environment
from .storage import ManagerError, read_json


def identity(pid):
    try:
        # comm may contain spaces or parentheses; fields after ')' start at #3.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return dict(pid=pid, start_ticks=fields[19], boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    except (OSError, IndexError):
        return None


def alive(record):
    return bool(record and record.get("pid") and identity(record["pid"]) == record)


def signal_process(record, sig):
    if not alive(record):
        return False
    # pidfd closes the PID-reuse race between verification and signaling.
    try:
        fd = os.pidfd_open(record["pid"])
    except ProcessLookupError:
        return False
    try:
        if identity(record["pid"]) != record:
            return False
        try:
            signal.pidfd_send_signal(fd, sig)
            return True
        except ProcessLookupError:
            return False
    finally:
        os.close(fd)


def stop_process(record, timeout=5):
    signal_process(record, signal.SIGTERM)
    end = time.monotonic() + timeout
    while alive(record) and time.monotonic() < end:
        time.sleep(.05)
    signal_process(record, signal.SIGKILL)


def descendants(pid):
    parents = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            parents[int(path.parent.name)] = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
    found = {pid}
    while True:
        expanded = found | {child for child, parent in parents.items() if parent in found}
        if expanded == found:
            return [record for child in found if (record := identity(child))]
        found = expanded


def stop_tree(pid, unit=None, timeout=1):
    """Stop each observed process by a checked PID, including compiler children."""
    targets = {r["pid"]: r for r in descendants(pid)}
    if unit:
        for child in scope_info(unit).get("pids", []):
            if record := identity(child):
                targets[child] = record
    for record in targets.values():
        signal_process(record, signal.SIGTERM)
    deadline = time.monotonic()+timeout
    while any(alive(r) for r in targets.values()) and time.monotonic() < deadline:
        time.sleep(.05)
    for record in targets.values():
        signal_process(record, signal.SIGKILL)
    deadline = time.monotonic()+3
    while any(alive(r) for r in targets.values()) and time.monotonic() < deadline:
        time.sleep(.05)
    if any(alive(r) for r in targets.values()):
        raise ManagerError("stop-timeout", "A child process has not exited; its inherited locks remain held.")


def scope_available():
    try:
        result = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=5, env=clean_environment())
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def scope_command(command, unit, memory, cpus, unscoped=False):
    if unscoped:
        return command
    if not scope_available():
        raise ManagerError("systemd-unavailable", "Systemd user scopes are unavailable. Use --unscoped only on another machine.", 6)
    result = ["systemd-run", "--user", "--scope", "--quiet", "--unit="+unit,
              "-p", "MemoryMax="+memory, "-p", "MemorySwapMax=0"]
    if cpus:
        result += ["-p", "AllowedCPUs="+cpus]
    return [*result, *command]


def scope_info(unit):
    try:
        result = subprocess.run(["systemctl", "--user", "show", unit+".scope",
                                 "-p", "ControlGroup", "-p", "MemoryPeak", "-p", "Result"],
                                capture_output=True, text=True, timeout=3, env=clean_environment())
        data = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        group = data.get("ControlGroup", "")
        if group.startswith("/") and ".." not in Path(group).parts:
            root = Path("/sys/fs/cgroup")/group.lstrip("/")
            data["pids"] = [int(p) for p in (root/"cgroup.procs").read_text().split()]
            data["memory_events"] = dict(line.split() for line in (root/"memory.events").read_text().splitlines())
        return data
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


class Toolchain:
    """Test overrides replace command prefixes, never the verification steps."""
    def __init__(self, source, override=None):
        self.source = Path(source)
        self.override_path = str(Path(override).resolve()) if override else None
        self.overrides = read_json(override) if override else {}
        for name, command in self.overrides.items():
            if name not in ("cli", "firmware", "translate", "compile") or not isinstance(command, list) or not command or any(not isinstance(a, str) for a in command):
                raise ManagerError("toolchain", "Toolchain must map tool names to nonempty argument arrays.", 2)

    def command(self, name):
        modules = dict(cli="anyps3", firmware="anyps3.firmware", translate="anyps3.build_game", compile="anyps3.native_build")
        return self.overrides.get(name, [sys.executable, "-P", "-m", modules[name]])

    def environment(self, source=None):
        env = clean_environment()
        env["PYTHONPATH"] = str(source or self.source)
        return env


def execute(command, log, *, env, unit, memory, cpus, unscoped=False, progress=None, lock_fds=()):
    command = scope_command(command, unit, memory, cpus, unscoped)
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    observed = {}
    with log.open("wb") as stream:
        process = None
        try:
            process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, env=env, pass_fds=lock_fds)
            while process.poll() is None:
                if progress:
                    progress(process, observed)
                if not unscoped:
                    info = scope_info(unit)
                    if info:
                        observed.update(info)
                time.sleep(.2)
        except BaseException:
            if process is not None:
                stop_tree(process.pid, None if unscoped else unit)
                process.wait()
            raise
    if process.returncode:
        tail = log.read_bytes()[-8192:].decode(errors="replace")
        oom = observed.get("memory_events", {}).get("oom_kill", "0") != "0"
        raise ManagerError("out-of-memory" if oom else "tool-failed",
                           f"Command exited with {process.returncode}.", returncode=process.returncode,
                           log=str(log), excerpt=tail, scope=observed)
    return observed
