"""Subprocess fixture: original ISO extraction, synthetic decryption/build."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

tool, *args = sys.argv[1:]
assert os.environ["SDL_AUDIODRIVER"] == "dummy"
assert os.environ["ANYPS3_AUDIO_SINK"] == "null"
assert "ANYPS3_AUDIO_AUDIBLE" not in os.environ
assert "ANYPS3_VIRTUAL_PAD_CONTROL" not in os.environ

def argument(name):
    return args[args.index(name)+1]

if tool == "cli":
    if args[0] == "disc-extract":
        from anyps3.disc import extract
        print(json.dumps(extract(Path(args[1]), Path(argument("-o")))))
    elif args[0] == "unself":
        shutil.copyfile(args[1], argument("-o"))
        print(json.dumps({"fixture": True}))
elif tool == "translate":
    time.sleep(.3)
    output = Path(argument("-o"))
    output.mkdir()
    shutil.copyfile(args[0], output/"program.elf")
    source = Path(os.environ["PYTHONPATH"])/"anyps3"
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob("*.py")}
    (output/"build.json").write_text(json.dumps(dict(source_sha256=hashes)))
    (output/"units.json").write_text(json.dumps(dict(units={"program.cpp": "fixture"})))
    (output/"program.cpp").write_text("// fixture")
elif tool == "compile":
    output = Path(argument("-o"))
    output.write_text("#!"+sys.executable+"\nimport os, sys\n"
        "assert os.environ['SDL_AUDIODRIVER']=='dummy'\n"
        "assert os.environ['ANYPS3_AUDIO_SINK']=='null'\n"
        "assert 'ANYPS3_AUDIO_AUDIBLE' not in os.environ\n"
        "assert 'ANYPS3_VIRTUAL_PAD_CONTROL' not in os.environ\n"
        "print('PERF interval flips_per_s=12.5', file=sys.stderr)\n"
        "print('STOP pc=0x10 lr=0 steps=1: synthetic unsupported operation', file=sys.stderr)\n"
        "sys.exit(1)\n")
    output.chmod(0o755)
    bundle = Path(args[args.index("--vulkan")+1])
    (bundle/"compile.json").write_text(json.dumps(dict(compiler_flags=["-march=native", "-ffp-contract=off"])))
else:
    raise SystemExit("Unexpected fixture tool")
