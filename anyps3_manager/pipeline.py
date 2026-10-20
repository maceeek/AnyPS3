"""Import/build transactions. Persistent userdata is never an output of a rebuild."""

import hashlib
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import time

from .processes import execute
from .capabilities import environment_reads
from .settings import FIELDS, effective
from .storage import (ManagerError, atomic_json, identifier, lock, now, read_json,
                      readonly, remove_tree, safe_id, sha256, tree_hashes)


def sfo(data):
    from anyps3.sfo import parse_sfo
    try:
        return parse_sfo(data)
    except Exception as exc:
        raise ManagerError("invalid-sfo", str(exc)) from exc


def disc_identity(iso):
    from anyps3.disc import inventory
    iso = Path(iso).expanduser().resolve()
    if not iso.is_file():
        raise ManagerError("missing-input", f"ISO not found: {iso}", 6)
    try:
        info = inventory(iso)
    except Exception as exc:
        raise ManagerError("invalid-disc", str(exc)) from exc
    if not info["image_complete_by_size"] or info["available_file_count"] != info["file_count"]:
        raise ManagerError("partial-disc", "The ISO is incomplete; partial imports are not supported.", 6)
    entry = next((e for e in info["entries"] if e["path"].upper() == "PS3_GAME/PARAM.SFO"), None)
    if not entry or entry["size"] > 1024 * 1024:
        raise ManagerError("invalid-disc", "Disc PARAM.SFO is missing or too large.")
    with iso.open("rb") as stream:
        chunks = []
        for extent in entry["extents"]:
            stream.seek(extent["lba"]*2048)
            chunks.append(stream.read(extent["size"]))
    metadata = sfo(b"".join(chunks))
    if metadata.get("CATEGORY") != "DG":
        raise ManagerError("unsupported-category", "Only disc games (CATEGORY=DG) are supported.")
    title = metadata.get("TITLE_ID", "")
    if not re.fullmatch(r"[A-Z]{4}[0-9]{5}", title):
        raise ManagerError("invalid-title", "Disc has no valid title ID.")
    return dict(id=title, title=metadata.get("TITLE", title), version=metadata.get("APP_VER", metadata.get("VERSION", "")),
                iso_path=str(iso), disc_bytes=sum(e["size"] for e in info["entries"] if not e["directory"]),
                image_complete_by_size=True,
                disc_firmware=any(e["path"].upper() == "PS3_UPDATE/PS3UPDAT.PUP" for e in info["entries"]))


def preflight(lib, iso):
    result = disc_identity(iso)
    free = shutil.disk_usage(lib.root).free
    result.update(free_bytes=free, estimated_required_bytes=result["disc_bytes"]+4*1024**3,
                  keys={k: bool(v.get("path") and Path(v["path"]).is_file())
                        for k, v in read_json(lib.root/"keys.json", {}).items()},
                  firmware=[p.name for p in (lib.root/"firmware").iterdir() if (p/"extraction.json").is_file()],
                  compatibility="Compatibility is determined from the decrypted executable hash; unknown titles are expected to stop.")
    result["space_ok"] = free >= result["estimated_required_bytes"]
    return result


def key_path(lib, kind):
    record = read_json(lib.root/"keys.json", {}).get(kind)
    if not record or not Path(record["path"]).is_file():
        raise ManagerError("missing-key", f"Set the user-supplied {kind} key file first.", 6)
    if sha256(record["path"]) != record["sha256"]:
        raise ManagerError("key-changed", f"The {kind} key file changed; register it again.", 6)
    return record["path"]


def set_key(lib, kind, path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ManagerError("missing-input", f"Key file not found: {path}", 6)
    with lib.metadata():
        keys = read_json(lib.root/"keys.json", {})
        keys[kind] = dict(path=str(path), sha256=sha256(path))
        atomic_json(lib.root/"keys.json", keys)
    return dict(kind=kind, path=str(path), sha256=keys[kind]["sha256"])


def profile_for(lib, elf, title):
    digest = sha256(elf)
    for path in sorted((lib.source/"profiles").glob("*.json")):
        profile = read_json(path)
        if profile.get("executable_sha256") == digest:
            return path, profile
    return None, dict(schema_version=1, title=title["title"], title_id=title["id"],
                      executable_sha256=None, static_seed_entries=[], status="Untested; an explicit stop is expected.")


def verify_manifest(root, manifest, entries):
    report = read_json(root/manifest)
    for entry in report[entries]:
        path = root/entry["path"]
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file() or sha256(path) != entry["sha256"]:
            raise ManagerError("integrity", f"Manifest mismatch: {entry['path']}")
    return report


def load_segments(path):
    data = Path(path).read_bytes()
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x02":
        raise ManagerError("invalid-elf", "Expected a 64-bit big-endian ELF.")
    offset = struct.unpack_from(">Q", data, 32)[0]
    size, count = struct.unpack_from(">HH", data, 54)
    if size < 56 or offset + size*count > len(data):
        raise ManagerError("invalid-elf", "ELF program headers are truncated.")
    segments = []
    for i in range(count):
        kind, flags, at, address, _, length, memory, align = struct.unpack_from(">IIQQQQQQ", data, offset+i*size)
        if kind == 1 and length:
            if at+length > len(data):
                raise ManagerError("invalid-elf", "ELF load segment is truncated.")
            segments.append((address, data[at:at+length]))
    if not segments:
        raise ManagerError("invalid-elf", "ELF has no nonempty load segments.")
    return segments


def verify_build(original, build):
    generated = load_segments(build/"native/program.elf")
    segments = []
    for address, data in load_segments(original):
        match = any(base <= address and address+len(data) <= base+len(payload) and
                    payload[address-base:address-base+len(data)] == data for base, payload in generated)
        if not match:
            raise ManagerError("game-bytes-changed", f"Original PT_LOAD bytes differ at 0x{address:x}.")
        segments.append(dict(address=address, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
    actual = tree_hashes(build/"source/anyps3", "*.py")
    if actual != read_json(build/"bundle/build.json")["source_sha256"]:
        raise ManagerError("source-mismatch", "Translator source hashes do not match the immutable snapshot.")
    return dict(original_segments=segments, source_sha256=actual,
                program_sha256=sha256(build/"native/program"), elf_sha256=sha256(build/"native/program.elf"))


def cpu_identity():
    text = Path("/proc/cpuinfo").read_text()
    result = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip() in ("model name", "flags") and key.strip() not in result:
                result[key.strip()] = value.strip()
    return result


def build_records(lib, title):
    lib.title(title)
    return [dict(read_json(p), logs=str(p.parent/"logs"))
            for p in sorted((lib.title_dir(title)/"builds").glob("*/record.json"), reverse=True)]


def title_details(lib, title):
    result = lib.title(title)
    root = lib.title_dir(title)
    reasons = []
    active = result.get("active_build")
    if active:
        record = read_json(root/"builds"/safe_id(active)/"record.json")
        if record["source_sha256"] != tree_hashes(lib.source/"anyps3", "*.py"):
            reasons.append("Runtime sources changed")
        profile = Path(result["profile_path"]) if result.get("profile_path") else root/"profile.json"
        if not profile.is_file() or sha256(profile) != record["profile_sha256"]:
            reasons.append("Profile changed")
        if cpu_identity() != record["cpu"]:
            reasons.append("Host CPU changed")
        if current_disc_identity(result) != record.get("disc_identity"):
            reasons.append("Disc changed")
        settings = effective(lib, title)
        if (settings["firmware.id"] or result["firmware"]) != record["firmware"]:
            reasons.append("Firmware changed")
        if {k: settings[k] for k, v in FIELDS.items() if v["scope"] == "build"} != record["build_settings"]:
            reasons.append("Build settings changed")
    result.update(stale=bool(reasons), staleness_reasons=reasons,
                  icon=str(root/"disc/PS3_GAME/ICON0.PNG"), background=str(root/"disc/PS3_GAME/PIC1.PNG"))
    return result


def current_disc_identity(record):
    return record.get("iso_sha256") or record.get("disc_manifest_sha256")


def activate(lib, title, build_id):
    from .saves import backup
    with lib.title_lock(title), lib.run_lock(title):
        return activate_locked(lib, title, build_id, backup)


def activate_locked(lib, title, build_id, backup=None):
    from .saves import backup as make_backup
    backup = backup or make_backup
    root = lib.title_dir(title)
    record = read_json(root/"builds"/safe_id(build_id)/"record.json")
    if record["status"] not in ("built", "verified-boot", "verified-play"):
        raise ManagerError("invalid-build", "Only a successfully verified build can be activated.")
    current = lib.title(title)
    if current.get("active_build") and current["active_build"] != build_id:
        old = read_json(root/"builds"/current["active_build"]/"record.json")
        if old["source_sha256"] != record["source_sha256"]:
            backup(lib, title, reason="activate", automatic=True)
    return lib.update_title(title, active_build=build_id, status=record["status"])


def prune(lib, title, keep):
    if keep < 0:
        raise ManagerError("usage", "--keep cannot be negative.", 2)
    with lib.title_lock(title), lib.run_lock(title):
        current = lib.title(title)
        records = build_records(lib, title)
        protected = {current.get("active_build"), *current.get("pinned_builds", []),
                     *(r["id"] for r in records[:keep])}
        removed = []
        for record in records:
            if record["id"] not in protected:
                remove_tree(lib.title_dir(title)/"builds"/record["id"])
                removed.append(record["id"])
        return dict(removed=removed)


class Pipeline:
    def __init__(self, lib, tools, job, unscoped=False):
        self.lib, self.tools, self.job, self.unscoped = lib, tools, job, unscoped
        self.lock_fds = ()

    @contextmanager
    def locked(self, *contexts):
        previous = self.lock_fds
        with ExitStack() as stack:
            self.lock_fds = (*previous, *(stack.enter_context(context) for context in contexts))
            try:
                yield
            finally:
                self.lock_fds = previous

    def stage(self, name, index, count):
        self.job.emit("stage", stage=name, index=index, count=count)

    def command(self, name, arguments, log, settings=None, source=None, progress=None):
        settings = settings or effective(self.lib)
        memory = settings["build.compile_memory_max" if name == "compile" else "build.translate_memory_max"]
        env = self.tools.environment(source)
        env["ANYPS3_OBJECT_CACHE"] = str(self.lib.root/"object-cache")
        return execute([*self.tools.command(name), *map(str, arguments)], log, env=env,
                       unit=f"anyps3-{self.job.id}-{name}-{identifier()[-8:]}", memory=memory, cpus=settings["build.cpus"],
                       unscoped=self.unscoped, progress=progress, lock_fds=self.lock_fds)

    def firmware(self, path):
        path = Path(path).resolve()
        if not path.is_file():
            raise ManagerError("missing-input", f"Firmware PUP missing: {path}", 6)
        destination = self.lib.root/"firmware"/(".staging-"+self.job.id)
        self.command("firmware", [path, "--key-file", key_path(self.lib, "pup"), "-o", destination],
                     self.job.path/"firmware.log")
        report = verify_manifest(destination, "extraction.json", "files")
        firmware_id = safe_id(report["version"]+"-"+report["pup_sha256"][:8])
        final = self.lib.root/"firmware"/firmware_id
        with self.lib.metadata():
            if final.exists():
                verify_manifest(final, "extraction.json", "files")
                remove_tree(destination)
            else:
                destination.rename(final)
                readonly(final)
        return dict(firmware=firmware_id)

    def choose_firmware(self, disc, settings):
        if settings["firmware.id"]:
            final = self.lib.root/"firmware"/safe_id(settings["firmware.id"])
            if (final/"dev_flash/sys/external").is_dir():
                return final.name
            raise ManagerError("missing-firmware", "Selected firmware is unavailable.", 6)
        existing = sorted(p for p in (self.lib.root/"firmware").iterdir()
                          if not p.name.startswith(".") and (p/"dev_flash/sys/external").is_dir())
        if existing:
            return existing[0].name
        pup = disc/"PS3_UPDATE/PS3UPDAT.PUP"
        if pup.is_file():
            return self.firmware(pup)["firmware"]
        raise ManagerError("missing-firmware", "Add a firmware PUP; no library or disc firmware is available.", 6)

    def import_iso(self, iso, no_build=False, replace_disc=None, firmware=None):
        self.stage("identify", 1, 7)
        iso = Path(iso).resolve()
        info = preflight(self.lib, iso)
        digest = sha256(iso)
        title = info["id"]
        if replace_disc and replace_disc != title:
            raise ManagerError("wrong-title", "--replace-disc must match the ISO title ID.", 2)
        with self.locked(self.lib.title_lock(title), self.lib.run_lock(title)):
            root = self.lib.title_dir(title)
            if (root/"import-transaction.json").exists():
                self.commit_import(root)
            previous = read_json(root/"title.json", {})
            if previous.get("iso_sha256") == digest and previous.get("status") != "incomplete":
                return dict(title=title, already_imported=True)
            if previous and previous.get("iso_sha256") != digest and not replace_disc:
                raise ManagerError("disc-conflict", "A different disc is registered. Use --replace-disc TITLE.", 2)
            if not info["space_ok"]:
                raise ManagerError("disk-space", "Insufficient free space for extraction and build.", 6, **info)
            if not previous:
                self.lib.register(dict(id=title, title=info["title"], version=info["version"], status="incomplete",
                                       iso_sha256=digest, active_build=None, pinned_builds=[], play_seconds=0))
            stage = root/(".import-"+digest[:16])
            stage.mkdir(parents=True, exist_ok=True)
            settings = effective(self.lib, title)
            if firmware:
                settings["firmware.id"] = safe_id(firmware)
            self.stage("extract", 2, 7)
            if not (stage/"disc/anyps3-extraction.json").is_file():
                remove_tree(stage/"disc")
                self.command("cli", ["disc-extract", iso, "-o", stage/"disc"], self.job.path/"extract.log")
            manifest = verify_manifest(stage/"disc", "anyps3-extraction.json", "extracted")
            if not manifest["image_complete_by_size"] or manifest["missing_count"]:
                raise ManagerError("partial-disc", "Extraction was incomplete.")
            self.stage("firmware", 3, 7)
            firmware_id = self.choose_firmware(stage/"disc", settings)
            self.stage("decrypt", 4, 7)
            (stage/"eboot").mkdir(exist_ok=True)
            elf = stage/"eboot/EBOOT.analysis.elf"
            if not (stage/"eboot/verified.json").is_file():
                elf.unlink(missing_ok=True)
                self.command("cli", ["unself", stage/"disc/PS3_GAME/USRDIR/EBOOT.BIN",
                             "--key-file", key_path(self.lib, "app"), "-o", elf], stage/"eboot/unself.json")
                atomic_json(stage/"eboot/verified.json", dict(sha256=sha256(elf)))
            if sha256(elf) != read_json(stage/"eboot/verified.json")["sha256"]:
                raise ManagerError("integrity", "Resumed decrypted executable changed.")
            self.stage("profile", 5, 7)
            profile_path, profile = profile_for(self.lib, elf, info)
            atomic_json(stage/"profile.json", profile)
            # Record a journal before swapping paths. A retry recovers an interrupted
            # transaction before replacing anything else; userdata never participates.
            metadata = dict(title=info["title"], version=info["version"],
                            iso_sha256=digest, iso_path=str(iso), firmware=firmware_id,
                            self_sha256=sha256(stage/"disc/PS3_GAME/USRDIR/EBOOT.BIN"),
                            elf_sha256=sha256(elf), profile_path=str(profile_path) if profile_path else None,
                            compatibility=profile.get("status", "Hash matched; compatibility is not established."),
                            status="stale" if previous.get("active_build") else "imported")
            atomic_json(root/"import-transaction.json", dict(stage=stage.name, old=".previous-"+self.job.id,
                        metadata=metadata, seed_username=settings["user.name"]))
            self.commit_import(root)
            readonly(root/"disc")
            self.stage("userdata", 6, 7)
            if not (root/"userdata").exists():
                self.seed_userdata(root, settings["user.name"])
            self.lib.update_title(title, **metadata)
        if no_build:
            return dict(title=title, status="imported")
        self.stage("build", 7, 7)
        return self.build(title, reason="Import")

    def commit_import(self, root):
        journal = read_json(root/"import-transaction.json")
        old = root/journal["old"]
        # Staging names are internal; reject any journal path escaping the title.
        stage = root/journal["stage"]
        if stage.parent != root or old.parent != root:
            raise ManagerError("invalid-journal", "Invalid import recovery paths.")
        old.mkdir(exist_ok=True)
        for name in ("disc", "eboot", "profile.json"):
            if (stage/name).exists():
                if (root/name).exists():
                    if (root/name).is_dir():
                        (root/name).chmod(0o700)
                    (root/name).rename(old/name)
                if (stage/name).is_dir():
                    (stage/name).chmod(0o700)
                (stage/name).rename(root/name)
        readonly(root/"disc")
        if not (root/"userdata").exists():
            self.seed_userdata(root, journal["seed_username"])
        # Publish identity before deleting recovery data. Repeating this step is
        # safe if the worker is interrupted between rename and metadata update.
        if journal.get("metadata"):
            self.lib.update_title(root.name, **journal["metadata"])
        remove_tree(old)
        remove_tree(stage)
        (root/"import-transaction.json").unlink()

    @staticmethod
    def seed_userdata(root, name):
        home = root/"userdata/hdd0/home/00000001"
        (home/"savedata").mkdir(parents=True)
        (home/"trophy").mkdir()
        (home/"localusername").write_text(name)

    def build(self, title, reason="", no_activate=False):
        # The machine lock is shared across all libraries for this Unix user.
        machine_lock = Path(tempfile.gettempdir())/f"anyps3-{os.getuid()}-compile.lock"
        with self.locked(lock(machine_lock), self.lib.title_lock(title), self.lib.run_lock(title)):
            current = self.lib.title(title)
            root = self.lib.title_dir(title)
            settings = effective(self.lib, title)
            build_id = f"{len(list((root/'builds').glob('*')))+1:04d}-"+identifier()
            build = root/"builds"/build_id
            build.mkdir(parents=True)
            record = dict(id=build_id, title=title, created=now(), status="building", reason=reason)
            atomic_json(build/"record.json", record)
            self.lib.update_title(title, status="building")
            start = time.monotonic()
            try:
                self.stage("snapshot", 1, 4)
                shutil.copytree(self.lib.source/"anyps3", build/"source/anyps3",
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                profile = Path(current["profile_path"]) if current.get("profile_path") else root/"profile.json"
                shutil.copyfile(profile, build/"profile.json")
                source_hashes = tree_hashes(build/"source/anyps3", "*.py")
                firmware_id = settings["firmware.id"] or current["firmware"]
                firmware = self.lib.root/"firmware"/safe_id(firmware_id)
                if not (firmware/"dev_flash/sys/external").is_dir():
                    raise ManagerError("missing-firmware", "Selected firmware is unavailable.", 6)
                revision = subprocess.run(["git", "-C", str(self.lib.source), "rev-parse", "HEAD"], capture_output=True, text=True)
                dirty = subprocess.run(["git", "-C", str(self.lib.source), "status", "--porcelain"], capture_output=True, text=True)
                record.update(source_sha256=source_hashes, profile_sha256=sha256(build/"profile.json"),
                              disc_identity=current_disc_identity(current),
                              firmware=firmware_id, cpu=cpu_identity(), git_revision=revision.stdout.strip() or None,
                              git_dirty=bool(dirty.stdout.strip()), build_settings={k: settings[k] for k, v in FIELDS.items() if v["scope"] == "build"},
                              available_env=environment_reads(build/"source/anyps3"))
                self.stage("translate", 2, 4)
                self.command("translate", [root/"eboot/EBOOT.analysis.elf", "--firmware", firmware/"dev_flash/sys/external",
                    "--key-file", key_path(self.lib, "app"), "--profile", build/"profile.json",
                    "--max-steps", "1000000000000", "-o", build/"bundle"], build/"logs/translate.log", settings, build/"source")
                self.stage("compile", 3, 4)
                (build/"native").mkdir()
                total = len([p for p in read_json(build/"bundle/units.json")["units"] if p.endswith(".cpp")])
                last = [-1]

                def progress(process, observed):
                    done = len(list((build/"native/objects").glob("*.o")))
                    if done != last[0]:
                        self.job.emit("progress", stage="compile", done=done, total=total, unit="objects")
                        last[0] = done

                scope = self.command("compile", ["--vulkan", build/"bundle", "-o", build/"native/program",
                          "-j", settings["build.jobs"], "--extra-flags="+settings["build.extra_flags"]],
                          build/"logs/compile.log", settings, build/"source", progress)
                shutil.copyfile(build/"bundle/program.elf", build/"native/program.elf")
                self.stage("verify", 4, 4)
                verification = verify_build(root/"eboot/EBOOT.analysis.elf", build)
                atomic_json(build/"verification.json", verification)
                compile_report = read_json(build/"bundle/compile.json")
                shutil.copyfile(build/"bundle/compile.json", build/"compile.json")
                shutil.copyfile(build/"bundle/build.json", build/"build.json")
                compiler = subprocess.run(["c++", "--version"], capture_output=True, text=True)
                record.update(status="built", duration_seconds=time.monotonic()-start,
                              peak_memory=scope.get("MemoryPeak"), compiler_version=compiler.stdout.splitlines(),
                              compiler_flags=compile_report["compiler_flags"], result="verified")
                if not settings["build.keep_intermediates"]:
                    remove_tree(build/"native/objects")
                    for p in (build/"bundle").iterdir():
                        if p.suffix in (".cpp", ".hpp"):
                            p.unlink()
                atomic_json(build/"record.json", record)
                if not no_activate:
                    activate_locked(self.lib, title, build_id)
                else:
                    self.lib.update_title(title, status=current["status"])
                self.trim_cache(settings["build.object_cache_gib"])
                readonly(build)
                return dict(title=title, build=build_id, status="built")
            except BaseException as exc:
                record.update(status="failed", error=str(exc), duration_seconds=time.monotonic()-start)
                atomic_json(build/"record.json", record)
                self.lib.update_title(title, status="failed")
                raise

    def trim_cache(self, gib):
        files = sorted((self.lib.root/"object-cache").glob("*.o"), key=lambda p: p.stat().st_mtime, reverse=True)
        retained = 0
        for path in files:
            retained += path.stat().st_size
            if retained > gib*1024**3:
                path.unlink()

    def adopt(self, path):
        source = Path(path).resolve()
        disc = source/"game-complete"
        info = sfo((disc/"PS3_GAME/PARAM.SFO").read_bytes())
        title = info["TITLE_ID"]
        if title != "BCES00511":
            raise ManagerError("wrong-title", "adopt-ratchet requires BCES00511.")
        root = self.lib.title_dir(title)
        with self.lib.title_lock(title), self.lib.run_lock(title):
            if (root/"adopt-transaction.json").exists():
                return self.commit_adoption(root)
            previous = read_json(root/"title.json", {})
            if previous and (previous.get("status") != "incomplete" or previous.get("adopted_from") != str(source)):
                raise ManagerError("already-exists", "Title already exists; adoption never overwrites it.")
            if (root/"userdata").exists():
                raise ManagerError("retained-userdata", "This title has retained userdata. Adoption never overwrites it; import the disc instead.")
            self.stage("verify-source", 1, 3)
            # Validate all prerequisites before publishing any title directory.
            for kind, filename in (("app", "app-key.json"), ("pup", "firmware-keys.json")):
                if not (source/filename).is_file():
                    raise ManagerError("missing-key", f"Key file not found: {source/filename}", 6)
            username = source/"hdd0/home/00000001/localusername"
            if not username.is_file() or not 1 <= username.stat().st_size <= 63:
                raise ManagerError("invalid-user", "Source localusername must contain 1–63 bytes.")
            verify_manifest(disc, "anyps3-extraction.json", "extracted")
            fw = verify_manifest(source/"firmware-verified", "extraction.json", "files")
            profile_path, profile = profile_for(self.lib, source/"EBOOT.analysis.elf", {"id": title, "title": info["TITLE"]})
            if not profile_path:
                raise ManagerError("identity", "Existing EBOOT does not match a known profile.")
            if shutil.disk_usage(self.lib.root).free < sum(p.stat().st_size for part in ("game-complete", "firmware-verified", "hdd0") for p in (source/part).rglob("*") if p.is_file())+4*1024**3:
                raise ManagerError("disk-space", "Insufficient free space for copy-only adoption.", 6)
            firmware_id = safe_id(fw["version"]+"-"+fw["pup_sha256"][:8])
            record = dict(id=title, title=info["TITLE"], version=info.get("APP_VER", ""),
                status="incomplete", active_build=None, pinned_builds=[], play_seconds=0, firmware=firmware_id,
                profile_path=str(profile_path), elf_sha256=profile["executable_sha256"],
                disc_manifest_sha256=sha256(disc/"anyps3-extraction.json"),
                compatibility=profile.get("status", "Hash matched; compatibility is not established."),
                adopted_from=str(source), adopted_at=now())
            set_key(self.lib, "app", source/"app-key.json")
            set_key(self.lib, "pup", source/"firmware-keys.json")
            self.lib.register(record)
            self.stage("copy", 2, 3)
            stage = root/".adopt-staging"
            remove_tree(stage)
            stage.mkdir()
            original_userdata = tree_hashes(source/"hdd0")
            shutil.copytree(disc, stage/"disc")
            (stage/"eboot").mkdir()
            shutil.copyfile(source/"EBOOT.analysis.elf", stage/"eboot/EBOOT.analysis.elf")
            shutil.copytree(source/"hdd0", stage/"userdata/hdd0")
            if tree_hashes(stage/"userdata/hdd0") != original_userdata:
                raise ManagerError("integrity", "Copied userdata differs from its source.")
            verify_manifest(stage/"disc", "anyps3-extraction.json", "extracted")
            if sha256(stage/"eboot/EBOOT.analysis.elf") != profile["executable_sha256"]:
                raise ManagerError("integrity", "Copied EBOOT hash mismatch.")
            destination = self.lib.root/"firmware"/firmware_id
            if not destination.exists():
                fw_stage = self.lib.root/"firmware"/(".adopt-"+self.job.id)
                remove_tree(fw_stage)
                shutil.copytree(source/"firmware-verified", fw_stage)
                verify_manifest(fw_stage, "extraction.json", "files")
                with self.lib.metadata():
                    if not destination.exists():
                        fw_stage.chmod(0o700)
                        fw_stage.rename(destination)
                        readonly(destination)
                    else:
                        remove_tree(fw_stage)
            verify_manifest(destination, "extraction.json", "files")
            atomic_json(stage/"profile.json", profile)
            record["status"] = "imported"
            atomic_json(root/"adopt-transaction.json", dict(stage=stage.name, record=record, userdata_sha256=original_userdata))
            result = self.commit_adoption(root)
            self.stage("complete", 3, 3)
            return result

    def commit_adoption(self, root):
        journal = read_json(root/"adopt-transaction.json")
        stage = root/journal["stage"]
        if stage.parent != root:
            raise ManagerError("invalid-journal", "Invalid adoption recovery path.")
        for name in ("disc", "eboot", "userdata", "profile.json"):
            if (stage/name).exists():
                if (root/name).exists():
                    raise ManagerError("adoption-conflict", f"Adoption cannot overwrite existing {name}.")
                if (stage/name).is_dir():
                    (stage/name).chmod(0o700)
                (stage/name).rename(root/name)
        verify_manifest(root/"disc", "anyps3-extraction.json", "extracted")
        if sha256(root/"eboot/EBOOT.analysis.elf") != journal["record"]["elf_sha256"]:
            raise ManagerError("integrity", "Adopted executable differs from the profile.")
        if tree_hashes(root/"userdata/hdd0") != journal["userdata_sha256"]:
            raise ManagerError("integrity", "Adopted userdata differs from its verified copy.")
        readonly(root/"disc")
        self.lib.register(journal["record"])
        remove_tree(stage)
        (root/"adopt-transaction.json").unlink()
        return dict(title=root.name, status="imported", next="build")
