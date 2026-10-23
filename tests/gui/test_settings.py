import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["ANYPS3_AUDIO_SINK"] = "null"

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton
from anyps3_gui.client import Client
from anyps3_gui.settings import Settings
from anyps3_manager import settings
from anyps3_manager.storage import read_json
from tests.manager.render_scale_fixture import SOURCE, TITLE, make_library


class SettingsGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="anyps3-scale-gui-")
        self.addCleanup(self.temp.cleanup)
        self.lib = make_library(Path(self.temp.name)/"library")
        self.environment = patch.dict(os.environ, PYTHONPATH=str(SOURCE),
                                      SDL_AUDIODRIVER="dummy", ANYPS3_AUDIO_SINK="null")
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = Client([sys.executable, "-P", "-m", "anyps3_manager", "--source", str(SOURCE)],
                             str(self.lib.root), self.app)
        self.errors = []
        self.client.error.connect(self.errors.append)
        self.widgets = []

    def tearDown(self):
        self.client.close()
        self.wait(lambda: not self.client.calls)
        for widget in self.widgets:
            widget.deleteLater()
        self.client.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait(self, predicate):
        if not predicate():
            loop = QEventLoop()
            timer = QTimer()
            timer.timeout.connect(lambda: loop.quit() if predicate() else None)
            timer.start(5)
            QTimer.singleShot(8000, loop.quit)
            loop.exec()
            timer.stop()
        self.assertTrue(predicate())

    def widget(self):
        widget = Settings(self.client, TITLE)
        self.widgets.append(widget)
        self.wait(lambda: not self.client.calls and bool(widget.editors))
        self.assertEqual(self.errors, [])
        return widget

    def button(self, editor, text):
        return next(button for button in editor.parentWidget().findChildren(QPushButton) if button.text() == text)

    def apply(self, widget, key, value):
        editor = widget.editors[key]
        self.assertIsInstance(editor, QComboBox)
        index = editor.findData(value)
        self.assertGreaterEqual(index, 0)
        editor.setCurrentIndex(index)
        self.button(editor, "Apply").click()
        self.wait(lambda: not self.client.calls and widget.values[key] == value)
        self.assertEqual(self.errors, [])

    def test_scale_selection_persists_reopens_and_reaches_launched_child(self):
        widget = self.widget()
        editor = widget.editors["video.render_scale"]
        self.assertEqual([editor.itemText(i) for i in range(editor.count())], ["Native (1×)", "2×", "3×", "4×"])
        self.assertEqual([editor.itemData(i) for i in range(editor.count())], [1, 2, 3, 4])
        self.apply(widget, "video.render_scale", 2)
        stored = read_json(self.lib.title_dir(TITLE)/"settings.json")
        self.assertEqual(stored["video.render_scale"], 2)
        self.assertIs(type(stored["video.render_scale"]), int)
        reopened = self.widget()
        self.assertEqual(reopened.editors["video.render_scale"].currentData(), 2)
        self.assertEqual(reopened.editors["video.render_scale"].currentText(), "2×")
        results = []
        self.client.call(["run", TITLE, "--unscoped"], results.append)
        self.wait(lambda: not self.client.calls)
        self.assertEqual(self.errors, [])
        self.assertEqual(len(results), 1)
        run_dir = self.lib.title_dir(TITLE)/"runs"/results[0]["id"]
        child = json.loads((run_dir/"stdout.log").read_text())
        self.assertEqual(child["ANYPS3_RENDER_SCALE"], "2")
        self.assertEqual(child["ANYPS3_AUDIO_SINK"], "null")
        self.assertEqual(child["SDL_AUDIODRIVER"], "dummy")
        self.assertEqual(read_json(run_dir/"run.json")["settings"]["video.render_scale"], 2)

    def test_inherit_restores_global_scale_and_string_choices_remain_strings(self):
        settings.set_value(self.lib, "video.render_scale", 3)
        widget = self.widget()
        self.assertEqual(widget.editors["video.render_scale"].currentData(), 3)
        self.apply(widget, "video.render_scale", 2)
        self.button(widget.editors["video.render_scale"], "Inherit").click()
        self.wait(lambda: not self.client.calls and widget.values["video.render_scale"] == 3)
        self.assertNotIn("video.render_scale", read_json(self.lib.title_dir(TITLE)/"settings.json"))
        self.assertEqual(widget.editors["video.render_scale"].currentData(), 3)
        self.apply(widget, "video.present_mode", "mailbox")
        stored = read_json(self.lib.title_dir(TITLE)/"settings.json")
        self.assertEqual(stored["video.present_mode"], "mailbox")
        self.assertIs(type(stored["video.present_mode"]), str)

    def test_skip_intro_shown_only_for_titles_with_a_recipe(self):
        # The fixture title has no recipe: the option is hidden, not just disabled.
        self.assertNotIn("input.skip_intro", self.widget().editors)
        self.lib.register(dict(id="BCES00511", title="Recipe title without a build"))
        widget = Settings(self.client, "BCES00511")
        self.widgets.append(widget)
        self.wait(lambda: not self.client.calls and bool(widget.editors))
        editor = widget.editors["input.skip_intro"]
        self.assertTrue(editor.isEnabled())
        self.assertFalse(editor.isChecked())
        editor.setChecked(True)
        self.button(editor, "Apply").click()
        self.wait(lambda: not self.client.calls and widget.values["input.skip_intro"] is True)
        self.assertEqual(self.errors, [])
        self.assertIs(read_json(self.lib.title_dir("BCES00511")/"settings.json")["input.skip_intro"], True)

    def test_older_active_build_disables_scale_control(self):
        from anyps3_manager.storage import atomic_json
        build = self.lib.title_dir(TITLE)/"builds"/self.lib.title(TITLE)["active_build"]/"record.json"
        record = read_json(build)
        record["available_env"] = []
        atomic_json(build, record)
        widget = self.widget()
        editor = widget.editors["video.render_scale"]
        self.assertFalse(editor.isEnabled())
        self.assertFalse(self.button(editor, "Apply").isEnabled())
        self.assertIn("Unavailable in the active build", editor.parentWidget().toolTip())
        self.assertIn("rebuild", editor.parentWidget().toolTip())

    def test_unimplemented_settings_hidden_until_advanced(self):
        # No AnyPS3 runtime reads ANYPS3_WINDOW_MODE: it is noise for a player.
        widget = self.widget()
        self.assertNotIn("video.window_mode", widget.editors)
        self.assertNotIn("performance.timer_slack_ns", widget.editors)
        self.assertIn("video.present_mode", widget.editors)
        widget.advanced.setChecked(True)
        self.assertFalse(widget.editors["video.window_mode"].isEnabled())
        self.assertIn("not implemented", widget.editors["video.window_mode"].parentWidget().toolTip())
        self.assertIn("performance.timer_slack_ns", widget.editors)


if __name__ == "__main__":
    unittest.main()
