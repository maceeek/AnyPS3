import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from anyps3_manager import runs, settings
from anyps3_manager.storage import ManagerError, atomic_json, read_json
from tests.manager.render_scale_fixture import BUILD, TITLE, make_library


class RenderScaleSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="anyps3-scale-settings-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lib = make_library(self.root/"library")

    def test_typed_values_persist_and_inherit_without_changing_window_size(self):
        self.assertEqual(settings.effective(self.lib, TITLE)["video.render_scale"], 1)
        for value in (1, 2, 3, 4):
            settings.set_value(self.lib, "video.render_scale", value)
            self.assertIs(type(read_json(self.lib.root/"settings.json")["video.render_scale"]), int)
        settings.set_value(self.lib, "video.render_scale", 2, TITLE)
        saved = read_json(self.lib.title_dir(TITLE)/"settings.json")
        self.assertEqual(saved, {"video.render_scale": 2})
        self.assertEqual(settings.effective(self.lib, TITLE)["video.render_scale"], 2)
        self.assertEqual(settings.effective(self.lib, TITLE, {"video.render_scale": 3})["video.render_scale"], 3)
        self.assertEqual(settings.effective(self.lib, TITLE)["video.width"], 1280)
        self.assertEqual(settings.effective(self.lib, TITLE)["video.height"], 720)
        settings.set_value(self.lib, "video.render_scale", None, TITLE, reset=True)
        self.assertEqual(read_json(self.lib.title_dir(TITLE)/"settings.json"), {})
        self.assertEqual(settings.effective(self.lib, TITLE)["video.render_scale"], 4)
        settings.set_value(self.lib, "video.render_scale", None, reset=True)
        self.assertEqual(settings.effective(self.lib, TITLE)["video.render_scale"], 1)

    def test_invalid_values_do_not_change_saved_configuration(self):
        settings.set_value(self.lib, "video.render_scale", 2, TITLE)
        for value in (0, 5, -1, 1.5, "2", True, None):
            with self.subTest(value=value):
                with self.assertRaises(ManagerError):
                    settings.set_value(self.lib, "video.render_scale", value, TITLE)
                with self.assertRaises(ManagerError):
                    settings.effective(self.lib, TITLE, {"video.render_scale": value})
        self.assertEqual(read_json(self.lib.title_dir(TITLE)/"settings.json"), {"video.render_scale": 2})

    def test_default_and_capability_gating_remove_ambient_scale(self):
        self.assertFalse(settings.schema([])["video.render_scale"]["available"])
        self.assertTrue(settings.schema(["ANYPS3_RENDER_SCALE"])["video.render_scale"]["available"])
        with patch.dict(os.environ, ANYPS3_RENDER_SCALE="4"):
            values = settings.effective(self.lib, TITLE)
            env = settings.run_environment(self.lib, TITLE, values, ["ANYPS3_RENDER_SCALE"], self.root)
            self.assertEqual(env["ANYPS3_RENDER_SCALE"], "1")
            env = settings.run_environment(self.lib, TITLE, values, [], self.root)
            self.assertNotIn("ANYPS3_RENDER_SCALE", env)

    def test_saved_scale_reaches_child_and_unsupported_build_omits_it(self):
        settings.set_value(self.lib, "video.render_scale", 2, TITLE)
        for supported, expected in ((True, "2"), (False, None)):
            with self.subTest(supported=supported):
                build = self.lib.title_dir(TITLE)/"builds"/BUILD/"record.json"
                record = read_json(build)
                record["available_env"] = ["ANYPS3_RENDER_SCALE"] if supported else []
                atomic_json(build, record)
                with patch.dict(os.environ, ANYPS3_RENDER_SCALE="4", SDL_AUDIODRIVER="dummy", ANYPS3_AUDIO_SINK="null"):
                    result = runs.run(self.lib, TITLE, unscoped=True)
                run_dir = self.lib.title_dir(TITLE)/"runs"/result["id"]
                child = json.loads((run_dir/"stdout.log").read_text())
                self.assertEqual(child["ANYPS3_RENDER_SCALE"], expected)
                self.assertEqual(child["ANYPS3_AUDIO_SINK"], "null")
                self.assertEqual(child["SDL_AUDIODRIVER"], "dummy")
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(read_json(run_dir/"run.json")["settings"]["video.render_scale"], 2)


if __name__ == "__main__":
    unittest.main()
