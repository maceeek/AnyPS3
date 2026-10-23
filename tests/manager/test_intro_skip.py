"""Intro-skip recipe driver, settings gating and the manager launch path.

PERF lines are synthetic, in the runtime's 'PERF interval_s=...' format; the
expected states are chosen from the recipe's documented thresholds (menu
74-84 draws/flip, title 62-66, video >40M SPU steps/s with <30 draws/s).
No game, compiler or SDL is used; the child is a scripted stand-in.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from anyps3_manager import intro_skip, runs, settings
from anyps3_manager.storage import atomic_json, read_json, sha256
from tests.manager.render_scale_fixture import BUILD, TITLE, make_library

ACIT = intro_skip.RECIPES["BCES00511"]


def perf(flips, rate, draws, spu):
    return (f"PERF interval_s=2.001 flips={flips} flips_per_s={rate} draws_per_s={draws} "
            f"spu_steps_per_s={spu} main_ppu_ms=12.0")


VIDEO = perf(40, 20, 10, 60_000_000)
TITLE_LIKE = perf(500, 60, 3840, 0)       # 64 draws/flip
MENU = perf(4000, 60, 4740, 0)           # 79 draws/flip
LOADING = perf(100, 2, 4, 0)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ClassifyTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(intro_skip.classify(MENU, ACIT), ("menu", 4000))
        self.assertEqual(intro_skip.classify(VIDEO, ACIT), ("video", 40))
        self.assertEqual(intro_skip.classify(TITLE_LIKE, ACIT), ("title", 500))
        self.assertEqual(intro_skip.classify(LOADING, ACIT), ("boot", 100))
        self.assertEqual(intro_skip.classify(perf(3000, 60, 600, 0), ACIT), ("screen", 3000))
        # Rate too low for a ratio: 79 draws/flip at 4 flips/s is not a menu.
        self.assertEqual(intro_skip.classify(perf(4000, 4, 316, 0), ACIT)[0], "screen")

    def test_rejects_malformed_or_other_lines(self):
        for line in ("RSX captured frame=3", "PERF interval_s=2 flips=1", perf("nan", 60, 10, 0),
                     perf(-1, 60, 10, 0), perf(1, "x", 10, 0), " "+MENU):
            self.assertIsNone(intro_skip.classify(line, ACIT), line)


class DriverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/"pad-inject.txt"
        self.clock = Clock()
        self.driver = intro_skip.IntroSkip(self.path, ACIT, self.clock)

    def file(self):
        return self.path.read_text()

    def advance(self, seconds, line=None):
        self.clock.now += seconds
        if line:
            self.driver.line(line)
        self.driver.tick()

    def test_presses_start_through_video_and_title_until_menu(self):
        self.assertEqual(self.file(), "v1 1 0 0\n")       # attached, released
        self.advance(.1)
        self.assertEqual(self.file(), "v1 1 0 0\n")       # nothing reported yet
        self.advance(.1, LOADING)
        self.assertEqual(self.file(), "v1 1 0 0\n")       # boot: no press
        self.advance(.1, VIDEO)
        self.assertEqual(self.file(), "v1 1 8 0\n")       # START = CELL_PAD_CTRL_START 0x0008
        self.advance(.5)
        self.assertEqual(self.file(), "v1 1 8 0\n")       # held for 1 s
        self.advance(.6)
        self.assertEqual(self.file(), "v1 1 0 0\n")
        self.advance(1, VIDEO)
        self.assertEqual(self.file(), "v1 1 0 0\n")       # 4 s gap after release
        self.advance(3.1, TITLE_LIKE)
        self.assertEqual(self.file(), "v1 1 0 0\n")       # first title sample only
        self.advance(.1)
        self.assertEqual(self.file(), "v1 1 0 0\n")       # stale sample is not re-used
        self.advance(.1, TITLE_LIKE)
        self.assertEqual(self.file(), "v1 1 8 0\n")       # second consecutive title sample
        self.assertEqual(self.driver.presses, 2)
        self.advance(1.1, MENU)
        self.assertTrue(self.driver.active)               # one menu sample is not enough
        self.advance(.1, MENU)
        self.assertFalse(self.driver.active)
        self.assertEqual(self.file(), "v1 0 0 0\n")       # detached: port 0 back to physical pads
        self.assertEqual(self.driver.summary()["reason"], "menu")
        self.advance(10, VIDEO)
        self.assertEqual(self.file(), "v1 0 0 0\n")       # finished drivers never press again

    def test_physical_input_releases_and_yields(self):
        self.advance(.1, VIDEO)
        self.assertEqual(self.file(), "v1 1 8 0\n")
        self.driver.line("native pad inject physical input port=0")
        self.assertEqual(self.file(), "v1 0 0 0\n")
        self.assertEqual(self.driver.summary()["reason"], "player-input")

    def test_timeout_and_stop_end_the_driver(self):
        self.advance(ACIT["timeout_s"], VIDEO)
        self.assertEqual((self.driver.summary()["reason"], self.driver.presses), ("timeout", 0))
        self.assertEqual(self.file(), "v1 0 0 0\n")
        other = intro_skip.IntroSkip(self.path, ACIT, self.clock)
        other.line("STOP pc=0x1234 unsupported")
        other.line(VIDEO)
        other.tick()
        self.assertEqual((other.summary()["reason"], other.presses), ("guest-stop", 0))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="anyps3-skip-settings-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lib = make_library(self.root/"library")

    def test_schema_applies_only_to_titles_with_a_recipe(self):
        available = ["ANYPS3_PAD_INJECT"]
        self.assertFalse(settings.FIELDS["input.skip_intro"]["default"])
        self.assertTrue(settings.schema(available)["input.skip_intro"]["available"])
        self.assertTrue(settings.schema(available, title="BCES00511")["input.skip_intro"]["available"])
        self.assertNotIn("applies", settings.schema(available, title="BCES00511")["input.skip_intro"])
        spec = settings.schema(available, title=TITLE)["input.skip_intro"]
        self.assertEqual((spec["available"], spec["applies"]), (False, False))
        self.assertFalse(settings.schema([], title="BCES00511")["input.skip_intro"]["available"])

    def test_environment_requires_setting_recipe_and_runtime_support(self):
        values = settings.effective(self.lib, TITLE, {"input.skip_intro": True, "diagnostics.perf_report": False})
        cases = (({}, ["ANYPS3_PAD_INJECT", "ANYPS3_PERF_REPORT"], False),
                 ({TITLE: ACIT}, [], False),
                 ({TITLE: ACIT}, ["ANYPS3_PAD_INJECT", "ANYPS3_PERF_REPORT"], True))
        for recipes, available, expected in cases:
            with self.subTest(recipes=bool(recipes), available=available), \
                 patch.dict(intro_skip.RECIPES, recipes), \
                 patch.dict(os.environ, ANYPS3_PAD_INJECT="/ambient"):
                run_dir = Path(tempfile.mkdtemp(dir=self.root))
                env = settings.run_environment(self.lib, TITLE, values, available, run_dir)
                if expected:
                    self.assertEqual(env["ANYPS3_PAD_INJECT"], str(run_dir/"pad-inject.txt"))
                    self.assertEqual((run_dir/"pad-inject.txt").read_text(), "v1 1 0 0\n")
                    self.assertEqual(env["ANYPS3_PERF_REPORT"], "1")
                else:
                    self.assertNotIn("ANYPS3_PAD_INJECT", env)
                    self.assertFalse((run_dir/"pad-inject.txt").exists())
        values["input.skip_intro"] = False
        with patch.dict(intro_skip.RECIPES, {TITLE: ACIT}):
            env = settings.run_environment(self.lib, TITLE, values, ["ANYPS3_PAD_INJECT"], Path(tempfile.mkdtemp(dir=self.root)))
        self.assertNotIn("ANYPS3_PAD_INJECT", env)


CHILD = r'''
import json, os, sys, time
path = os.environ["ANYPS3_PAD_INJECT"]
seen = []
def wait(expected, timeout=15):
    end = time.time()+timeout
    while time.time() < end:
        state = open(path).read()
        if state == expected:
            seen.append(state.strip())
            return True
        time.sleep(.02)
    seen.append("missing "+expected.strip())
    return False
def perf(line):
    print(line, file=sys.stderr, flush=True)
ok = wait("v1 1 0 0\n")
perf(VIDEO)
ok = wait("v1 1 8 0\n") and ok
ok = wait("v1 1 0 0\n") and ok
perf(MENU); perf(MENU)
ok = wait("v1 0 0 0\n") and ok
print(json.dumps(dict(ok=ok, seen=seen, perf=os.environ.get("ANYPS3_PERF_REPORT"))))
'''


class LaunchTests(unittest.TestCase):
    def test_manager_run_drives_injection_file_and_records_result(self):
        temp = tempfile.TemporaryDirectory(prefix="anyps3-skip-run-")
        self.addCleanup(temp.cleanup)
        lib = make_library(Path(temp.name)/"library")
        native = lib.title_dir(TITLE)/"builds"/BUILD/"native"
        program = native/"program"
        program.write_text(f"#!{sys.executable}\nVIDEO={VIDEO!r}\nMENU={MENU!r}\n"+CHILD)
        build = native.parent
        verification = read_json(build/"verification.json")
        verification["program_sha256"] = sha256(program)
        atomic_json(build/"verification.json", verification)
        record = read_json(build/"record.json")
        record["available_env"] = ["ANYPS3_PAD_INJECT", "ANYPS3_PERF_REPORT"]
        atomic_json(build/"record.json", record)
        settings.set_value(lib, "input.skip_intro", True, TITLE)
        fast = dict(ACIT, hold_s=.3, gap_s=.3)
        with patch.dict(intro_skip.RECIPES, {TITLE: fast}), \
             patch.dict(os.environ, SDL_AUDIODRIVER="dummy", ANYPS3_AUDIO_SINK="null"):
            result = runs.run(lib, TITLE, unscoped=True)
        run_dir = lib.title_dir(TITLE)/"runs"/result["id"]
        child = json.loads((run_dir/"stdout.log").read_text())
        self.assertTrue(child["ok"], child)
        self.assertEqual(child["perf"], "1")
        saved = read_json(run_dir/"run.json")["intro_skip"]
        self.assertEqual((saved["state"], saved["reason"], saved["presses"]), ("finished", "menu", 1))


if __name__ == "__main__":
    unittest.main()
