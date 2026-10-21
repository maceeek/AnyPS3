"""Save backups and portable exports. Restore never runs alongside the game."""

import errno
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import zipfile

from .pipeline import sfo
from .settings import effective
from .storage import ManagerError, identifier, read_json, safe_id

EXPORT_SCHEMA = "anyps3.save/1"
try:
    from compression import zstd  # Python 3.14+, optional stdlib backend.
    BACKUP_SUFFIX, WRITE_MODE = ".tar.zst", "w:zst"
except ImportError:
    BACKUP_SUFFIX, WRITE_MODE = ".tar.gz", "w:gz"


def checked_path(root, relative):
    """Reject links in save paths, including ancestors outside the leaf tree."""
    path = root
    for part in Path(relative).parts:
        path = path/part
        if path.is_symlink():
            raise ManagerError("unsafe-save", "Save paths cannot contain symbolic links.")
    return path


def save_root(lib, title):
    lib.title(title)
    return lib.title_dir(title)/"userdata/hdd0/home/00000001/savedata"


def list_saves(lib, title):
    result = []
    for path in sorted(save_root(lib, title).glob("*")):
        if path.is_dir():
            metadata = {}
            if (path/"PARAM.SFO").is_file():
                try:
                    metadata = sfo((path/"PARAM.SFO").read_bytes())
                except ManagerError as exc:
                    metadata = {"error": str(exc)}
            result.append(dict(id=path.name, title=metadata.get("TITLE", path.name),
                subtitle=metadata.get("SUB_TITLE", ""), detail=metadata.get("DETAIL", ""),
                metadata=metadata, icon=str(path/"ICON0.PNG"),
                size=sum(p.stat().st_size for p in path.rglob("*") if p.is_file()),
                modified=path.stat().st_mtime))
    return result


def backup(lib, title, reason="manual", automatic=False):
    """Caller holds the run lock when data must not change during a snapshot."""
    settings = effective(lib, title)
    root = lib.title_dir(title)
    directory = root/"backups"
    directory.mkdir(exist_ok=True)
    name = identifier()+("-auto-" if automatic else "-manual-")+safe_id(reason)+BACKUP_SUFFIX
    target = directory/name
    temporary = directory/("."+name)
    paths = ["hdd0/home/00000001/savedata", "hdd0/home/00000001/trophy"]
    if settings["saves.backup_game_data"]:
        paths.append("hdd0/game/"+title)
    try:
        with tarfile.open(temporary, WRITE_MODE) as archive:
            for path in paths:
                origin = checked_path(root, "userdata/"+path)
                if origin.exists():
                    if any(p.is_symlink() for p in [origin, *origin.rglob("*")]):
                        raise ManagerError("unsafe-save", "Save backups cannot contain symbolic links.")
                    archive.add(origin, arcname=path)
                elif path != "hdd0/game/"+title:
                    # Absence is an empty snapshot too: restoring it must clear
                    # saves/trophies created after this backup.
                    entry = tarfile.TarInfo(path)
                    entry.type, entry.mode = tarfile.DIRTYPE, 0o700
                    archive.addfile(entry)
        temporary.rename(target)
    finally:
        temporary.unlink(missing_ok=True)
    if automatic:
        for old in sorted(directory.glob("*-auto-*.tar.*"), reverse=True)[settings["saves.keep"]:]:
            old.unlink()
    return dict(backup=name, path=str(target), compression="zstd" if BACKUP_SUFFIX == ".tar.zst" else "gzip")


def replace_directory(stage, target):
    """Same-filesystem replacement with rollback on a failed publish."""
    previous = target.parent/("."+target.name+"-previous-"+identifier())
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.rename(previous)
    try:
        stage.rename(target)
    except BaseException:
        if previous.exists():
            previous.rename(target)
        raise
    if previous.exists():
        shutil.rmtree(previous)


def safe_member(name):
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts or "\0" in name:
        raise ManagerError("unsafe-archive", f"Unsafe archive path: {name}")
    return path


def restore(lib, title, backup_id):
    with lib.run_lock(title):
        root = lib.title_dir(title)
        path = root/"backups"/safe_id(backup_id)
        if not path.is_file():
            raise ManagerError("missing-backup", "Backup not found.", 6)
        allowed = ["hdd0/home/00000001/savedata", "hdd0/home/00000001/trophy", "hdd0/game/"+title]
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=root) as temporary:
            stage = Path(temporary)
            with tarfile.open(path, "r:*") as archive:
                members = archive.getmembers()
                total = 0
                for member in members:
                    name = str(safe_member(member.name))
                    if not any(name == p or name.startswith(p+"/") for p in allowed) or not (member.isfile() or member.isdir()):
                        raise ManagerError("unsafe-archive", "Backup contains unsupported paths or links.")
                    total += member.size
                    if total > shutil.disk_usage(root).free:
                        raise ManagerError("disk-space", "Insufficient space to restore backup.", 6)
                archive.extractall(stage, filter="data")
            backup(lib, title, reason="before-restore")
            replace = [name for name in allowed if (stage/name).exists()]
            for name in replace:
                checked_path(root, "userdata/"+name)
            for name in replace:
                target = root/"userdata"/name
                replace_directory(stage/name, target)
        return dict(restored=backup_id)


def export_save(lib, title, save, output):
    with lib.run_lock(title):
        lib.title(title)
        source = checked_path(lib.title_dir(title), "userdata/hdd0/home/00000001/savedata/"+safe_id(save))
        if not source.is_dir():
            raise ManagerError("missing-save", "Save not found.", 6)
        output = Path(output).expanduser().resolve()
        if output.exists() or output.is_relative_to(lib.title_dir(title)):
            raise ManagerError("export-path", "Export must be a new file outside this title.")
        fd, temporary = tempfile.mkstemp(prefix=".save-export-", dir=output.parent)
        os.close(fd)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("anyps3-save.json", json.dumps(dict(schema=EXPORT_SCHEMA, title=title, save=save)))
                for path in sorted(source.rglob("*")):
                    if path.is_symlink():
                        raise ManagerError("unsafe-save", "Export cannot contain symbolic links.")
                    if path.is_file() or path.is_dir():
                        archive.write(path, "save/"+path.relative_to(source).as_posix())
            # Publish only a complete archive and never replace a raced-in file.
            try:
                os.link(temporary, output)
            except OSError as exc:
                if exc.errno not in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.EPERM, errno.EXDEV):
                    raise
                # FAT/exFAT destinations lack hardlinks. Exclusive creation
                # still prevents overwrite; the copy is visible until complete.
                created = False
                try:
                    with output.open("xb") as destination:
                        created = True
                        with Path(temporary).open("rb") as source_archive:
                            shutil.copyfileobj(source_archive, destination)
                except BaseException:
                    if created:
                        output.unlink(missing_ok=True)
                    raise
        finally:
            Path(temporary).unlink(missing_ok=True)
        return dict(path=str(output))


def import_save(lib, title, path):
    with lib.run_lock(title):
        root = lib.title_dir(title)
        with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory(prefix=".save-import-", dir=root) as temporary:
            try:
                metadata = json.loads(archive.read("anyps3-save.json"))
            except (KeyError, ValueError) as exc:
                raise ManagerError("unsupported-save", "Only AnyPS3 save exports are accepted.") from exc
            if not isinstance(metadata, dict):
                raise ManagerError("unsupported-save", "Save export metadata must be a JSON object.")
            if metadata.get("schema") != EXPORT_SCHEMA or metadata.get("title") != title:
                raise ManagerError("wrong-save", "Save export schema or title does not match.")
            save = safe_id(metadata.get("save"))
            target = checked_path(root, "userdata/hdd0/home/00000001/savedata/"+save)
            stage = Path(temporary)/"save"
            stage.mkdir()
            total, names = 0, set()
            for member in archive.infolist():
                name = safe_member(member.filename)
                if member.filename == "anyps3-save.json":
                    continue
                if not name.parts or name.parts[0] != "save" or len(name.parts) < 2 or member.filename in names:
                    raise ManagerError("unsafe-archive", "Unexpected or duplicate export member.")
                names.add(member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ManagerError("unsafe-archive", "Save export cannot contain links.")
                total += member.file_size
                if total > shutil.disk_usage(root).free or total > 4*1024**3:
                    raise ManagerError("save-too-large", "Save export exceeds available space or 4 GiB.")
                destination = Path(temporary)/str(name)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as src, destination.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
            if not (stage/"PARAM.SFO").is_file():
                raise ManagerError("invalid-save", "Save export has no PARAM.SFO.")
            sfo((stage/"PARAM.SFO").read_bytes())
            backup(lib, title, reason="before-import")
            replace_directory(stage, target)
        return dict(imported=target.name)
