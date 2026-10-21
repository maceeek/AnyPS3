import ast
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["ANYPS3_AUDIO_SINK"] = "null"

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QProcess, QSettings, QTimer
from PySide6.QtWidgets import QApplication, QPushButton
from anyps3_gui.app import Window
from anyps3_gui.client import Client
from anyps3_gui.import_wizard import ImportWizard
from anyps3_gui.settings import Settings


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="anyps3-qt-test-")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, cls.temp.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def client(self):
        client = Client([sys.executable, str(Path(__file__).with_name("stub_cli.py"))], parent=self.app)
        self.addCleanup(client.deleteLater)
        return client

    def tearDown(self):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait(self, predicate):
        loop = QEventLoop()
        timer = QTimer()
        timer.timeout.connect(lambda: loop.quit() if predicate() else None)
        timer.start(5)
        QTimer.singleShot(8000, loop.quit)
        loop.exec()
        timer.stop()
        self.assertTrue(predicate())

    def test_gui_has_no_runtime_or_manager_imports(self):
        root = Path(__file__).resolve().parents[2]/"anyps3_gui"
        for path in root.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                for name in names:
                    self.assertNotIn(name.split(".")[0], ("anyps3", "anyps3_manager"), str(path))

    def test_streaming_events_and_unknown_fields(self):
        client = self.client()
        events, results, errors = [], [], []
        client.call(["jobs", "follow", "test"], results.append, events.append, errors.append)
        self.wait(lambda: not client.calls)
        self.assertEqual([e["event"] for e in events], ["stage", "progress"])
        self.assertEqual(results[0]["build"], "fixture")
        self.assertEqual(results[0]["uint64"], 18446744073709551615)
        self.assertEqual(errors, [])

    def test_errors_preserve_code_message_and_detail(self):
        client = self.client()
        errors = []
        client.call(["error"], error=errors.append)
        self.wait(lambda: not client.calls)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], "guest-stop")
        self.assertEqual(errors[0]["message"], "STOP pc=0x10: fixture")
        self.assertEqual(errors[0]["detail"], {"run": "test"})

    def test_malformed_protocol_is_visible(self):
        client = self.client()
        errors = []
        client.call(["malformed"], error=errors.append)
        self.wait(lambda: not client.calls)
        self.assertEqual(errors[0]["code"], "protocol")

    def test_window_smoke_and_geometry(self):
        client = self.client()
        window = Window(client)
        window.show()
        self.wait(lambda: not client.calls)
        self.assertEqual(window.library.count(), 0)
        self.assertFalse(window.error_box.isVisible())
        window.close()
        self.assertIsNotNone(window.preferences.value("geometry"))
        self.app.processEvents()

    def test_window_close_reaps_followers_without_new_manager_requests(self):
        client = self.client()
        window = Window(client)
        window.show()
        self.wait(lambda: not client.calls)
        window.follow('long')
        follower = window.followers['long']
        self.wait(lambda: follower.received)
        process = follower.process
        self.assertEqual(process.state(), QProcess.ProcessState.Running)
        with patch.object(client, 'call', wraps=client.call) as requests:
            window.close()
            self.assertEqual(process.state(), QProcess.ProcessState.NotRunning)
            self.wait(lambda: not client.calls)
            self.assertEqual(requests.call_count, 0)
        self.assertTrue(client.closing)
        self.assertEqual(client.calls, set())

    def test_pending_settings_replies_disconnect_when_widget_is_deleted(self):
        client = Client([sys.executable, str(Path(__file__).with_name('stub_cli.py')), '--slow-settings'], parent=self.app)
        self.addCleanup(client.deleteLater)
        self.addCleanup(client.close)
        exceptions = []
        with patch('sys.excepthook', side_effect=lambda *args: exceptions.append(args)):
            settings = Settings(client, 'BCES00511')
            self.assertEqual(len(client.calls), 3)
            settings.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.wait(lambda: not client.calls)
        self.assertEqual(exceptions, [])

    def test_pending_setting_writes_disconnect_after_widget_deletion(self):
        client = Client([sys.executable, str(Path(__file__).with_name('stub_cli.py')), '--slow-settings'], parent=self.app)
        self.addCleanup(client.deleteLater)
        self.addCleanup(client.close)
        for operation in ('apply', 'inherit'):
            with self.subTest(operation=operation):
                settings = Settings(client, 'BCES00511')
                self.wait(lambda: not client.calls)
                settings.values = {'audio.enabled': False}
                settings.got_schema({'settings': {'audio.enabled': dict(type='boolean', group='Audio', description='Game audio')}})
                settings.got_values({'settings': {'audio.enabled': False}, 'overrides': {'audio.enabled': False}})
                if operation == 'apply':
                    settings.save('audio.enabled', lambda: False)
                else:
                    next(button for button in settings.findChildren(QPushButton) if button.text() == 'Inherit' and button.isEnabled()).click()
                self.assertEqual(len(client.calls), 1)
                exceptions = []
                with patch.object(client, 'call', wraps=client.call) as requests, patch('sys.excepthook', side_effect=lambda *args: exceptions.append(args)):
                    settings.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    self.wait(lambda: not client.calls)
                    self.assertEqual(requests.call_count, 0)
                self.assertEqual(exceptions, [])

    def test_controllers_show_motion_and_rumble_capabilities(self):
        from anyps3_gui.app import Controllers
        class Client:
            def call(self, args, result):
                result({"pads": [{"id": "1", "name": "Fixture", "rumble": True, "motion": False}]})
        dialog = Controllers(Client())
        try:
            columns = [dialog.table.horizontalHeaderItem(i).text() for i in range(dialog.table.columnCount())]
            self.assertIn("Rumble", columns)
            self.assertIn("Motion (accel + gyro)", columns)
            self.assertEqual(dialog.table.item(0, columns.index("Rumble")).text(), "True")
            self.assertEqual(dialog.table.item(0, columns.index("Motion (accel + gyro)")).text(), "False")
        finally:
            dialog.deleteLater()

    def test_firmware_terminal_result_advances_to_disc_import_once(self):
        class PendingClient:
            def __init__(self):
                self.pending = []
                self.commands = []

            def call(self, arguments, result=None, **kwargs):
                self.commands.append(arguments)
                self.pending.append(result)

            def reply(self, value):
                self.pending.pop(0)(value)

        client = PendingClient()
        wizard = ImportWizard(client)
        try:
            wizard.iso.setText("/synthetic/disc.iso")
            wizard.paths["firmware"].setText("/synthetic/update.pup")
            wizard.no_build.setChecked(True)
            submitted = []
            wizard.submitted.connect(submitted.append)
            wizard.accept()
            client.reply(dict(job="fw-job", state="queued"))
            client.reply(dict(schema="anyps3.manager/1", event="result", job="fw-job", firmware="3.01-test"))
            self.assertEqual(client.commands, [
                ["firmware", "add", "/synthetic/update.pup"],
                ["jobs", "follow", "fw-job"],
                ["import", "/synthetic/disc.iso", "--no-build", "--firmware", "3.01-test"]])
            client.reply(dict(job="import-job", state="queued"))
            self.assertEqual(submitted, [dict(job="import-job", state="queued")])
            self.assertEqual(client.pending, [])
        finally:
            wizard.deleteLater()


if __name__ == "__main__":
    unittest.main()
