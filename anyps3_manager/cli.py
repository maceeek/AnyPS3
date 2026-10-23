"""Stable machine-readable CLI; long operations belong to detached workers."""

import argparse
import ctypes.util
import json
import os
from pathlib import Path
import shutil
import sys
import time

from . import SCHEMA
from . import jobs, keys as key_setup, pipeline, runs, saves, settings
from .processes import scope_available
from .storage import Library, ManagerError, atomic_json, read_json, remove_tree, safe_id


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ManagerError("usage", message, 2)


def parser():
    common = Parser(add_help=False)
    common.add_argument("--json", action="store_true")
    common.add_argument("--events", action="store_true")
    common.add_argument("--library")
    common.add_argument("--source")
    common.add_argument("--toolchain")
    common.add_argument("--unscoped", action="store_true")
    root = Parser(description=__doc__, parents=[common])
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor"):
        commands.add_parser(name)
    keys = commands.add_parser("keys")
    keys.add_argument("action", choices=["set", "fetch-rpcs3"])
    keys.add_argument("kind", choices=["app", "pup"], nargs="?")
    keys.add_argument("path", nargs="?")
    keys.add_argument("--stdin", action="store_true", help="Read pasted key JSON from stdin; store privately (never pass key values as arguments)")
    keys.add_argument("--iso", help="Validate against EBOOT.BIN and firmware in this ISO")
    keys.add_argument("--pup", help="Validate against this PUP (overrides ISO firmware)")
    keys.add_argument("--self", dest="self_path", help="Validate against this EBOOT.BIN SELF")
    firmware = commands.add_parser("firmware")
    firmware.add_argument("action", choices=["list", "add", "add-from-disc"])
    firmware.add_argument("value", nargs="?")
    imp = commands.add_parser("import")
    imp.add_argument("iso")
    imp.add_argument("--no-build", action="store_true")
    imp.add_argument("--replace-disc")
    imp.add_argument("--firmware", help="Firmware ID from firmware list")
    scan = commands.add_parser("inspect-iso")
    scan.add_argument("iso")
    titles = commands.add_parser("titles")
    titles.add_argument("action", choices=["list", "show", "remove", "resume"], nargs="?", default="list")
    titles.add_argument("title", nargs="?")
    titles.add_argument("--delete-userdata", action="store_true")
    titles.add_argument("--no-backup", action="store_true")
    build = commands.add_parser("build")
    build.add_argument("title")
    build.add_argument("--reason", default="")
    build.add_argument("--no-activate", action="store_true")
    commands.add_parser("builds").add_argument("title")
    for name in ("activate", "pin", "unpin"):
        cmd = commands.add_parser(name)
        cmd.add_argument("title")
        cmd.add_argument("build")
    prune = commands.add_parser("prune")
    prune.add_argument("title")
    prune.add_argument("--keep", type=int, required=True)
    run = commands.add_parser("run")
    run.add_argument("title")
    run.add_argument("--build")
    run.add_argument("--set", action="append", default=[])
    run.add_argument("--detach", action="store_true")
    commands.add_parser("stop").add_argument("title")
    history = commands.add_parser("runs")
    history.add_argument("values", nargs="+")
    save = commands.add_parser("saves")
    save.add_argument("action", choices=["list", "backups", "backup", "restore", "export", "import"])
    save.add_argument("title")
    save.add_argument("values", nargs="*")
    conf = commands.add_parser("settings")
    conf.add_argument("action", choices=["schema", "get", "set", "reset"])
    conf.add_argument("values", nargs="*")
    group = conf.add_mutually_exclusive_group()
    group.add_argument("--title")
    group.add_argument("--global", dest="global_scope", action="store_true")
    job = commands.add_parser("jobs")
    job.add_argument("action", choices=["list", "show", "follow", "cancel", "remove", "clear"])
    job.add_argument("ids", nargs="*")
    pads = commands.add_parser("pads")
    pads.add_argument("action", choices=["list", "monitor", "rumble-test"])
    pads.add_argument("pad", nargs="?")
    commands.add_parser("adopt-ratchet").add_argument("path")
    worker = commands.add_parser("_worker")
    worker.add_argument("id")
    worker.add_argument("--lock-fd", type=int)
    return root


def normalize_options(argv):
    """Common flags may appear before or after any command."""
    before, after = [], []
    valued = {"--library", "--source", "--toolchain"}
    switches = {"--json", "--events", "--unscoped"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        key = arg.split("=", 1)[0]
        if key in valued:
            before.append(arg)
            if "=" not in arg:
                i += 1
                if i == len(argv):
                    raise ManagerError("usage", f"{key} requires a value.", 2)
                before.append(argv[i])
        elif key in switches:
            before.append(arg)
        else:
            after.append(arg)
        i += 1
    # argparse's '*' positional cannot resume after an option. Move settings
    # scope flags before its action so both documented/GUI spellings work.
    if after and after[0] == "settings":
        options, positionals = [], ["settings"]
        i = 1
        while i < len(after):
            arg = after[i]
            if arg == "--title":
                if i+1 == len(after):
                    raise ManagerError("usage", "--title requires an ID.", 2)
                options.extend(after[i:i+2])
                i += 2
                continue
            if arg == "--global" or arg.startswith("--title="):
                options.append(arg)
            else:
                positionals.append(arg)
            i += 1
        after = ["settings", *options, *positionals[1:]]
    return before+after


def emit(value):
    print(json.dumps({"schema": SCHEMA, **value}, ensure_ascii=False), flush=True)


def require_values(values, count):
    if len(values) != count:
        raise ManagerError("usage", f"Expected {count} argument(s), received {len(values)}.", 2)
    return values


def available(lib, title):
    if title and lib.title(title).get("active_build"):
        return read_json(lib.title_dir(title)/"builds"/lib.title(title)["active_build"]/"record.json")["available_env"]
    # No build: expose capabilities of the selected source, not imagined features.
    from .capabilities import environment_reads
    return environment_reads(lib.source/"anyps3")


def dispatch(args, lib):
    cmd = args.command
    if cmd == "init":
        return lib.init()
    if cmd == "_worker":
        return jobs.work(lib, args.id, args.lock_fd)
    if not (lib.root/"library.json").exists():
        lib.init()
    if cmd == "doctor":
        checks = dict(compiler=bool(shutil.which("c++")), sdl2=bool(ctypes.util.find_library("SDL2-2.0")),
                      vulkan=bool(ctypes.util.find_library("vulkan")),
                      vulkan_icds=[str(p) for directory in ("/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d")
                                   for p in Path(directory).glob("*.json")],
                      glslang=bool(ctypes.util.find_library("glslang")),
                      systemd_user=scope_available(), keys=read_json(lib.root/"keys.json", {}),
                      free_bytes=shutil.disk_usage(lib.root).free,
                      ram_bytes=os.sysconf("SC_PAGE_SIZE")*os.sysconf("SC_PHYS_PAGES"))
        checks["keys"] = {kind: dict(record, exists=Path(record["path"]).is_file())
                          for kind, record in checks["keys"].items()}
        return dict(checks=checks, library=str(lib.root), audio_enabled=settings.effective(lib)["audio.enabled"])
    if cmd == "keys":
        if args.action == "fetch-rpcs3" and (args.kind or args.path or args.stdin):
            raise ManagerError("usage", "keys fetch-rpcs3 accepts only --iso, --pup and --self targets.", 2)
        if args.action == "set" and (not args.kind or bool(args.path) == bool(args.stdin)):
            raise ManagerError("usage", "Use keys set app|pup /path/to/key.json or --stdin. " + key_setup.guidance(args.kind or "pup"), 2)
        selected = key_setup.targets(iso=args.iso, pup=args.pup, self_path=args.self_path)
        if args.action == "fetch-rpcs3":
            return key_setup.fetch_rpcs3(lib, selected)
        if args.stdin:
            try:
                data = sys.stdin.read(16385)
                if len(data) > 16384:
                    raise ValueError()
                spec = json.loads(data)
            except (ValueError, UnicodeError):
                raise key_setup.invalid(args.kind, "Invalid pasted JSON; enter the required hex fields again.") from None
            return key_setup.enter_key(lib, args.kind, spec, selected)
        return pipeline.set_key(lib, args.kind, args.path, selected=selected)
    if cmd == "inspect-iso":
        return pipeline.preflight(lib, args.iso)
    if cmd == "firmware":
        if args.action == "list":
            return dict(firmware=[dict(id=p.parent.name, **read_json(p)) for p in (lib.root/"firmware").glob("*/extraction.json")])
        if not args.value:
            raise ManagerError("usage", "Specify a PUP path or title.", 2)
        path = lib.title_dir(args.value)/"disc/PS3_UPDATE/PS3UPDAT.PUP" if args.action == "add-from-disc" else Path(args.value).resolve()
        return jobs.start(lib, "firmware", dict(path=str(path)), args.toolchain, args.unscoped)
    if cmd in ("import", "build", "adopt-ratchet"):
        operation = {"import": "import", "build": "build", "adopt-ratchet": "adopt"}[cmd]
        values = dict(iso=str(Path(args.iso).resolve()), no_build=args.no_build, replace_disc=args.replace_disc, firmware=args.firmware) if cmd == "import" else (
            dict(title=args.title, reason=args.reason, no_activate=args.no_activate) if cmd == "build" else dict(path=str(Path(args.path).resolve())))
        return jobs.start(lib, operation, values, args.toolchain, args.unscoped)
    if cmd == "titles":
        if args.action == "list":
            return dict(titles=[pipeline.title_details(lib, t["id"]) for t in lib.titles()])
        if not args.title:
            raise ManagerError("usage", "Specify a title ID.", 2)
        if args.action == "show":
            return dict(title=pipeline.title_details(lib, args.title))
        if args.action == "resume":
            detail = pipeline.title_details(lib, args.title)
            if detail["busy"]:
                raise ManagerError("title-busy", "A job or game is active for this title; wait or cancel it first.", 4)
            recovery = detail.get("recovery")
            if detail["status"] != "incomplete" or not recovery:
                raise ManagerError("no-recovery", "Choose the original ISO in Import ISO, or repeat Adopt Ratchet with its source directory.", 6)
            return jobs.start(lib, recovery["operation"], recovery["arguments"], args.toolchain, args.unscoped)
        with lib.title_lock(args.title), lib.run_lock(args.title):
            lib.title(args.title)
            if pipeline.title_jobs(lib, args.title):
                raise ManagerError("title-busy", "A job is active for this title; wait or cancel it before removal.", 4)
            root = lib.title_dir(args.title)
            if args.delete_userdata and not args.no_backup:
                saves.backup(lib, args.title, reason="before-remove")
            for path in root.iterdir():
                if path.name in ("userdata", "backups") and not (path.name == "userdata" and args.delete_userdata):
                    continue
                remove_tree(path) if path.is_dir() else path.unlink()
            with lib.metadata():
                index = read_json(lib.root/"library.json")
                index["titles"].remove(args.title)
                atomic_json(lib.root/"library.json", index)
        return dict(removed=args.title, userdata_preserved=not args.delete_userdata)
    if cmd == "builds":
        return dict(builds=pipeline.build_records(lib, args.title))
    if cmd == "activate":
        return dict(title=pipeline.activate(lib, args.title, args.build))
    if cmd in ("pin", "unpin"):
        safe_id(args.build)
        read_json(lib.title_dir(args.title)/"builds"/args.build/"record.json")
        with lib.title_lock(args.title):
            current = lib.title(args.title)
            pinned = set(current.get("pinned_builds", []))
            pinned.add(args.build) if cmd == "pin" else pinned.discard(args.build)
            lib.update_title(args.title, pinned_builds=sorted(pinned))
        return dict(pinned_builds=sorted(pinned))
    if cmd == "prune":
        return pipeline.prune(lib, args.title, args.keep)
    if cmd == "run":
        overrides = {}
        for value in args.set:
            if "=" not in value:
                raise ManagerError("usage", "--set requires KEY=VALUE.", 2)
            key, raw = value.split("=", 1)
            overrides[key] = settings.parse_value(raw)
        settings.effective(lib, args.title, overrides)
        if args.detach:
            return jobs.start(lib, "run", dict(title=args.title, build=args.build, overrides=overrides),
                              args.toolchain, args.unscoped)
        return runs.run(lib, args.title, args.build, overrides, args.unscoped)
    if cmd == "stop":
        return runs.stop(lib, args.title)
    if cmd == "runs":
        if len(args.values) == 1:
            return dict(runs=runs.records(lib, args.values[0]))
        require_values(args.values, 3)
        action, title, run_id = args.values
        if action != "show":
            raise ManagerError("usage", "Use runs show TITLE RUN.", 2)
        path = lib.title_dir(title)/"runs"/safe_id(run_id)
        return dict(run=read_json(path/"run.json"), stdout=str(path/"stdout.log"), stderr=str(path/"stderr.log"),
                    excerpt=(path/"stderr.log").read_bytes()[-16384:].decode(errors="replace"))
    if cmd == "saves":
        counts = {"list": 0, "backups": 0, "backup": 0, "restore": 1, "export": 2, "import": 1}
        require_values(args.values, counts[args.action])
        if args.action == "list":
            return dict(saves=saves.list_saves(lib, args.title))
        if args.action == "backups":
            return dict(backups=[dict(id=p.name, size=p.stat().st_size) for p in sorted((lib.title_dir(args.title)/"backups").glob("*.tar.*"), reverse=True)])
        if args.action == "backup":
            with lib.run_lock(args.title):
                return saves.backup(lib, args.title)
        return {"restore": saves.restore, "export": saves.export_save, "import": saves.import_save}[args.action](lib, args.title, *args.values)
    if cmd == "settings":
        if args.action == "schema":
            from .capabilities import environment_reads
            return dict(settings=settings.schema(available(lib, args.title), environment_reads(lib.source/"anyps3")))
        if args.action == "get":
            return dict(settings=settings.effective(lib, args.title),
                        overrides=settings.stored_values(lib, args.title))
        require_values(args.values, 2 if args.action == "set" else 1)
        key = args.values[0]
        value = settings.parse_value(args.values[1]) if args.action == "set" else None
        return settings.set_value(lib, key, value, args.title, reset=args.action == "reset")
    if cmd == "jobs":
        if args.action == "list":
            require_values(args.ids, 0)
            return dict(jobs=jobs.records(lib))
        if args.action == "clear":
            require_values(args.ids, 0)
            return jobs.remove(lib)
        if not args.ids:
            raise ManagerError("usage", "Specify a job ID.", 2)
        if args.action == "remove":
            return jobs.remove(lib, args.ids)
        require_values(args.ids, 1)
        job = jobs.Job(lib, args.ids[0])
        if args.action == "show":
            return dict(job=job.record(), log=str(job.path/"log.txt"))
        if args.action == "cancel":
            return job.cancel()
        for event in job.follow():
            emit(event)
        record = job.record()
        return record.get("exit_code", 0 if record["state"] == "succeeded" else 1)
    if cmd == "pads":
        from .pads import Pads
        pads = Pads()
        try:
            if args.action == "monitor":
                while True:
                    emit(dict(event="input", pads=pads.devices()))
                    time.sleep(.05)
            if args.action == "rumble-test" and args.pad is None:
                raise ManagerError("usage", "Specify the controller ID from pads list.", 2)
            return dict(pads=pads.devices(args.pad if args.action == "rumble-test" else None))
        finally:
            pads.close()
    raise ManagerError("usage", "Unknown command.", 2)


def main(argv=None):
    args = None
    try:
        args = parser().parse_args(normalize_options(list(sys.argv[1:] if argv is None else argv)))
        lib = Library(args.library, args.source)
        result = dispatch(args, lib)
        if isinstance(result, int):
            return result
        if result is not None:
            if args.events and result.get("job") and isinstance(result["job"], str):
                for event in jobs.Job(lib, result["job"]).follow():
                    emit(event)
                record = jobs.Job(lib, result["job"]).record()
                return record.get("exit_code", 0 if record["state"] == "succeeded" else 1)
            emit(dict(event="result", **result) if args.events else result)
        return 0
    except KeyboardInterrupt:
        emit(ManagerError("cancelled", "Client interrupted; detached jobs continue.", 5).record())
        return 5
    except Exception as exc:
        error = exc if isinstance(exc, ManagerError) else ManagerError("error", str(exc))
        emit(error.record())
        return error.exit_code
