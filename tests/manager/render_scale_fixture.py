"""Synthetic library/child for configuration plumbing; no game or compiler."""

from pathlib import Path
import sys

from anyps3_manager import settings
from anyps3_manager.storage import Library, atomic_json, sha256


SOURCE = Path(__file__).resolve().parents[2]
TITLE = "SCALEFIXTURE"
BUILD = "synthetic-env-reader"


def make_library(root, supported=True):
    lib = Library(root, SOURCE)
    lib.init()
    lib.register(dict(id=TITLE, title="Synthetic settings fixture", active_build=BUILD))
    settings.set_value(lib, "audio.enabled", False)
    settings.set_value(lib, "saves.backup_before_run", False)
    settings.set_value(lib, "run.cpus", "")
    title = lib.title_dir(TITLE)
    user = title/"userdata/hdd0/home/00000001/localusername"
    user.parent.mkdir(parents=True)
    user.write_text("Fixture")
    native = title/"builds"/BUILD/"native"
    native.mkdir(parents=True)
    program = native/"program"
    program.write_text(f"#!{sys.executable}\n" +
                       "import json, os\n"
                       "print(json.dumps({key: os.environ.get(key) for key in "
                       "('ANYPS3_RENDER_SCALE', 'ANYPS3_AUDIO_SINK', 'SDL_AUDIODRIVER')}))\n")
    program.chmod(0o700)
    (native/"program.elf").write_bytes(b"Synthetic fixture, not a guest ELF.\n")
    atomic_json(native.parent/"record.json", dict(status="built", synthetic=True,
                available_env=["ANYPS3_RENDER_SCALE", "ANYPS3_PRESENT_MODE"] if supported else []))
    atomic_json(native.parent/"verification.json", dict(synthetic=True,
                program_sha256=sha256(program), elf_sha256=sha256(native/"program.elf")))
    return lib
