"""Atomic records, bounded identifiers and advisory process locks."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from . import SCHEMA


class ManagerError(Exception):
    def __init__(self, code, message, exit_code=1, **detail):
        super().__init__(message)
        self.code, self.exit_code, self.detail = code, exit_code, detail

    def record(self):
        return dict(schema=SCHEMA, event="error", code=self.code,
                    message=str(self), detail=self.detail)


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ManagerError("invalid-id", "Invalid library identifier.", 2)
    return value


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        if default is not None:
            return default
        raise ManagerError("missing-input", f"Missing file: {path}", 6) from None
    except (ValueError, UnicodeError) as exc:
        raise ManagerError("invalid-json", f"Invalid JSON: {path}: {exc}") from exc


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_hashes(root, pattern="*"):
    return {str(p.relative_to(root)): sha256(p) for p in sorted(Path(root).rglob(pattern)) if p.is_file()}


@contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ManagerError("lock-busy", f"Busy: {path.name}", 4) from None
        yield fd
    finally:
        os.close(fd)


def remove_tree(path):
    """Remove our own read-only disc/snapshot; never follow a directory symlink."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        for directory, dirs, files in os.walk(path, followlinks=False):
            os.chmod(directory, 0o700)
        shutil.rmtree(path)


def readonly(path):
    for item in sorted(Path(path).rglob("*"), reverse=True):
        if item.is_symlink():
            raise ManagerError("unsafe-file", f"Symlink in immutable data: {item}")
        item.chmod(0o555 if item.is_dir() or item.stat().st_mode & 0o111 else 0o444)
    Path(path).chmod(0o555)


class Library:
    def __init__(self, root=None, source=None):
        self.source = Path(source or Path(__file__).resolve().parent.parent).resolve()
        self.root = Path(root or Path(os.environ.get("XDG_DATA_HOME", Path.home()/".local/share"))/"anyps3").expanduser().resolve()
        repository_roots = [self.source]
        # A linked worktree can itself be under the primary checkout's ignored
        # local/ directory. Neither checkout is a suitable persistent library.
        repository_roots.extend(parent for parent in self.source.parents if (parent/".git").exists())
        if any(self.root == repo or self.root.is_relative_to(repo) for repo in repository_roots):
            raise ManagerError("library-location", "The library must be outside the source checkout.", 2)

    def init(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.metadata():
            for name in ("locks", "jobs", "object-cache", "firmware", "titles"):
                (self.root/name).mkdir(exist_ok=True)
            if not (self.root/"library.json").exists():
                atomic_json(self.root/"library.json", dict(schema=SCHEMA, created=now(), titles=[]))
        return dict(library=str(self.root))

    def metadata(self):
        return lock(self.root/"locks/metadata.lock")

    def title_dir(self, title):
        return self.root/"titles"/safe_id(title)

    def title(self, title):
        return read_json(self.title_dir(title)/"title.json")

    def title_lock(self, title):
        return lock(self.root/"locks"/(safe_id(title)+".operation.lock"))

    @contextmanager
    def run_lock(self, title):
        from .processes import alive, scope_info
        with lock(self.root/"locks"/(safe_id(title)+".run.lock")) as fd:
            for path in (self.title_dir(title)/"runs").glob("*/run.json"):
                record = read_json(path)
                if record.get("state") != "running":
                    continue
                live = any(alive(record.get(key)) for key in ("program_process", "launcher_process"))
                if record.get("scope"):
                    live |= bool(scope_info(record["scope"]).get("pids"))
                if live:
                    raise ManagerError("lock-busy", "A recorded game process is still alive.", 4)
            yield fd

    def update_title(self, title_id, **fields):
        with self.metadata():
            record = self.title(title_id)
            record.update(fields)
            atomic_json(self.title_dir(title_id)/"title.json", record)
        return record

    def register(self, record):
        title = safe_id(record["id"])
        with self.metadata():
            atomic_json(self.title_dir(title)/"title.json", record)
            index = read_json(self.root/"library.json")
            if title not in index["titles"]:
                index["titles"].append(title)
            atomic_json(self.root/"library.json", index)

    def titles(self):
        index = read_json(self.root/"library.json", {"titles": []})
        return [self.title(t) for t in index["titles"]]
