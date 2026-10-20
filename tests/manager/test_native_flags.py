"""Exercise the public native compiler option with an independent tiny program."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from anyps3.native_build import write_units


class NativeFlagsTests(unittest.TestCase):
    def test_extra_flags_are_applied_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_units(root, {"anyps3.hpp": "// fixture\n", "program.cpp":
                "#ifndef MANAGER_VALUE\n#error extra flag not applied\n#endif\n"
                "int main(){return MANAGER_VALUE == 73 ? 0 : 1;}\n"})
            env = {k: v for k, v in os.environ.items() if not k.startswith("ANYPS3_")}
            env.update(SDL_AUDIODRIVER="dummy", ANYPS3_AUDIO_SINK="null")
            result = subprocess.run([sys.executable, "-m", "anyps3.native_build", str(root),
                "--extra-flags=-DMANAGER_VALUE=73 -ffp-contract=off", "-j", "1"],
                capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root/"compile.json").read_text())
            self.assertIn("-DMANAGER_VALUE=73", report["compiler_flags"])
            self.assertIn("-ffp-contract=off", report["compiler_flags"])
            self.assertEqual(subprocess.run([str(root/"program")], env=env).returncode, 0)
