import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .demo import make_demo
from .elf import ElfImage
from .errors import AnyPS3Error
from .ppu import recompile
from .profile import check_profile


def integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected an integer (decimal or 0x-prefixed hex).") from exc


def write_new(path: Path, data: bytes) -> None:
    # Exclusive creation also rejects symlinks/hard links to existing inputs.
    with path.open("xb") as stream:
        stream.write(data)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="AnyPS3: experimental ELF inspection and PPU function recompilation.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Inspect ELF headers and load segments without executing code.")
    inspect.add_argument("input", type=Path)
    inspect.add_argument("--json", action="store_true", help="Print a machine-readable report.")
    inspect.add_argument("--profile", type=Path, help="Check input identity against a target profile JSON.")
    compile_cmd = commands.add_parser("recompile", help="Translate a supported integer PPU function into a C++ harness.")
    compile_cmd.add_argument("input", type=Path)
    compile_cmd.add_argument("-o", "--output", required=True, type=Path)
    compile_cmd.add_argument("--entry", type=integer, help="Override the ELF entry virtual address.")
    compile_cmd.add_argument("--entry-kind", choices=("descriptor", "code"), default="descriptor",
                             help="PS3 OPD descriptor (default), or an explicit code address with TOC=0.")
    compile_cmd.add_argument("--max-instructions", type=integer, default=4096)
    program = commands.add_parser("recompile-program", help="Statically translate calls/control flow; unknown services fail explicitly.")
    program.add_argument("input", type=Path)
    program.add_argument("-o", "--output", type=Path, required=True)
    program.add_argument("--entry", type=integer)
    program.add_argument("--entry-kind", choices=("descriptor", "code"), default="descriptor")
    program.add_argument("--max-instructions", type=integer, default=65536)
    program.add_argument("--max-steps", type=integer, default=1000000)
    program.add_argument("--diagnostic", action="store_true", help="Emit explicit runtime traps at unsupported sites instead of rejecting the whole graph.")
    program.add_argument("--process-entry", action="store_true", help="Set PS3 process arguments and enable the single-main-thread TLS service.")
    program.add_argument("--seed", type=integer, action="append", default=[], help="Additional known indirect code target to translate ahead of time; repeatable.")
    program.add_argument("--profile", type=Path, help="Use static entry seeds from an SHA-256-pinned target profile.")
    demo = commands.add_parser("demo", help="Create an original synthetic PPU ELF returning r3=42.")
    demo.add_argument("-o", "--output", required=True, type=Path)
    survey = commands.add_parser("analyze", help="Survey PPU sections, OPD entries, imports, embedded SPUs and translation blockers.")
    survey.add_argument("input", type=Path)
    sfo = commands.add_parser("sfo", help="Read PARAM.SFO metadata as JSON.")
    sfo.add_argument("input", type=Path)
    unself = commands.add_parser("unself", help="Extract verified APP SELF load segments into an analysis ELF; print integrity JSON.")
    unself.add_argument("input", type=Path)
    unself.add_argument("--key-file", type=Path, required=True)
    unself.add_argument("--allow-prx", action="store_true", help="Extract APP PRX containers for analysis; does not relocate or run them.")
    unself.add_argument("-o", "--output", type=Path, required=True)
    disc = commands.add_parser("disc-inventory", help="List ISO9660 file extents and their availability as JSON.")
    disc.add_argument("input", type=Path)
    extract = commands.add_parser("disc-extract", help="Extract ISO9660 files into a new directory.")
    extract.add_argument("input", type=Path)
    extract.add_argument("-o", "--output", type=Path, required=True)
    extract.add_argument("--allow-partial", action="store_true")
    recover = commands.add_parser("recover-zip", help="Recover the first ISO from a ZIP, including an explicit partial snapshot.")
    recover.add_argument("input", type=Path)
    recover.add_argument("-o", "--output", type=Path, required=True)
    recover.add_argument("--allow-partial", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            write_new(args.output, make_demo())
            print(f"Created synthetic PPU ELF: {args.output}")
            return 0
        if args.command in ("disc-inventory", "disc-extract"):
            from .disc import inventory, extract
            report = (inventory(args.input) if args.command == "disc-inventory"
                      else extract(args.input, args.output, allow_partial=args.allow_partial))
            print(json.dumps(report, indent=2))
            return 0
        if args.command == "recover-zip":
            from .archive import recover_iso
            print(json.dumps(recover_iso(args.input, args.output, allow_partial=args.allow_partial), indent=2))
            return 0
        data = args.input.read_bytes()
        if args.command == "sfo":
            from .sfo import parse_sfo
            print(json.dumps(parse_sfo(data), indent=2, ensure_ascii=False))
            return 0
        if args.command == "unself":
            from .self_file import unpack_self
            result, report = unpack_self(data, args.key_file, allow_prx=args.allow_prx)
            write_new(args.output, result)
            print(json.dumps(report, indent=2))
            return 0
        image = ElfImage.parse(data)
        if args.command == "recompile-program":
            from .program import compile_program
            seeds = list(args.seed)
            if args.profile:
                checked = check_profile(args.profile, image.report()["sha256"])
                if not checked["identity_verified"]:
                    raise AnyPS3Error("Program seeds require a profile pinned to the executable SHA-256.")
                settings = json.loads(args.profile.read_text(encoding="utf-8"))
                configured = settings.get("static_seed_entries", [])
                if (not isinstance(configured, list) or len(configured) > 10000 or
                        any(type(value) is not int for value in configured)):
                    raise AnyPS3Error("Profile static_seed_entries must be a list of at most 10000 integer code addresses.")
                seeds.extend(configured)
            result = compile_program(image, entry=args.entry, entry_kind=args.entry_kind,
                                     diagnostic=args.diagnostic, max_instructions=args.max_instructions,
                                     process_entry=args.process_entry, extra_entries=tuple(dict.fromkeys(seeds)))
            write_new(args.output, result.cpp(max_steps=args.max_steps).encode("utf-8"))
            print(json.dumps(result.report(), indent=2))
            return 0
        if args.command == "analyze":
            from .analysis import analyze
            print(json.dumps(analyze(image), indent=2))
            return 0
        if args.command == "inspect":
            report = image.report()
            if args.profile:
                report["target_profile"] = check_profile(args.profile, report["sha256"])
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(f"ELF{image.bits} {image.byte_order}-endian | {report['elf']['machine_name']}")
                print(f"Type: 0x{image.type:x} | OS ABI: 0x{image.os_abi:x} | Entry: 0x{image.entry:x}")
                print(f"SHA-256: {report['sha256']}")
                for seg in image.segments:
                    print(f"[{seg.index}] type=0x{seg.type:x} {seg.permissions} va=0x{seg.virtual_address:x} "
                          f"offset=0x{seg.offset:x} file=0x{seg.file_size:x} memory=0x{seg.memory_size:x}")
                if report["ppu_entry"]:
                    entry = report["ppu_entry"]
                    print(f"PS3 OPD interpretation: code=0x{entry['code_address']:x}, TOC=0x{entry['toc']:x}")
                for note in report["notes"]:
                    print(f"Note: {note}")
                if "target_profile" in report:
                    profile = report["target_profile"]
                    print(f"Target: {profile['title']} | {profile['note']}")
            return 0
        result = recompile(image, entry=args.entry, entry_kind=args.entry_kind,
                           max_instructions=args.max_instructions)
        write_new(args.output, result.cpp().encode("utf-8"))
        print(f"Translated {len(result.instructions)} PPU instructions into {args.output}")
        print("Output is a single-function test harness; game runtime services are not implemented.")
        return 0
    except (AnyPS3Error, OSError) as exc:
        print(f"anyps3: {exc}", file=sys.stderr)
        return 1

