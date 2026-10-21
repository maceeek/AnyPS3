"""Offscreen key setup and stdin protocol checks, with synthetic inputs only."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from anyps3_gui.client import Client
from anyps3_gui.import_wizard import ImportWizard
from anyps3_gui.keys import KeyEntry


class PendingClient:
    def __init__(self):
        self.calls = []

    def call(self, args, result=None, **kwargs):
        self.calls.append((args, result, kwargs))


class GuiKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wizard(self):
        client = PendingClient()
        wizard = ImportWizard(client)
        self.addCleanup(wizard.deleteLater)
        wizard.iso.setText("/synthetic/game.iso")
        wizard.paths["firmware"].setText("/synthetic/PS3UPDAT.PUP")
        return wizard, client

    def test_download_requires_confirmation_and_passes_selected_targets(self):
        wizard, client = self.wizard()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question:
            wizard.fetch_keys()
        self.assertEqual(client.calls, [])
        text = question.call_args.args[2]
        for expected in ("key_vault.h", "key_vault.cpp", "raw.githubusercontent.com", "ffba2bf", "local law"):
            self.assertIn(expected, text)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            wizard.fetch_keys()
        self.assertEqual(client.calls[0][0], ["keys", "fetch-rpcs3", "--iso", "/synthetic/game.iso", "--pup", "/synthetic/PS3UPDAT.PUP"])
        self.assertTrue(wizard.key_busy)
        client.calls[0][1]({"keys": {"app": {"validation": "SELF validated"}, "pup": {"validation": "Format only; no PUP selected."}}})
        self.assertFalse(wizard.key_busy)
        self.assertIn("Format only", wizard.key_status.text())
        self.assertTrue(wizard.scan["keys"]["app"])

    def test_download_error_is_actionable_and_allows_retry(self):
        wizard, client = self.wizard()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            wizard.fetch_keys()
        client.calls[0][2]["error"]({"code": "key-source-hash", "message": "Hash mismatch; nothing saved. Retry."})
        self.assertIn("Retry", wizard.key_status.text())
        self.assertFalse(wizard.key_busy)
        self.assertTrue(wizard.fetch_button.isEnabled())

    def test_hex_entry_uses_stdin_and_failure_keeps_dialog_open(self):
        wizard, client = self.wizard()
        seen = []

        def entry(dialog):
            dialog.values["erk"].setText(bytes(range(32)).hex())
            dialog.values["riv"].setText(bytes(range(16)).hex())
            dialog.buttons.accepted.emit()
            args, saved, kwargs = client.calls[0]
            self.assertIn("--stdin", args)
            spec = json.loads(kwargs["input_data"])
            self.assertEqual(spec["revision"], 1)
            self.assertNotIn(spec["erk"], " ".join(args))
            kwargs["error"]({"code": "invalid-key", "message": "Wrong key; check the source and retry."})
            self.assertIn("retry", dialog.status.text())
            self.assertTrue(dialog.buttons.isEnabled())
            seen.append(True)
            return 0

        with patch.object(KeyEntry, "exec", entry):
            wizard.enter_key("app")
        self.assertEqual(seen, [True])
        self.assertFalse(wizard.key_busy)

    def test_existing_keys_can_be_replaced(self):
        wizard, _ = self.wizard()
        wizard.scan = {"keys": {"pup": True, "app": True}}
        wizard.configure_sources(2)
        self.assertFalse(wizard.paths["pup"].isHidden())
        self.assertFalse(wizard.paths["app"].isHidden())
        self.assertIn("Configured", wizard.key_status.text())

    def test_json_file_setup_sends_validation_targets_before_import(self):
        wizard, client = self.wizard()
        wizard.paths["app"].setText("/synthetic/app.json")
        wizard.accept()
        self.assertEqual(client.calls[0][0], ["keys", "set", "app", "/synthetic/app.json", "--iso", "/synthetic/game.iso", "--pup", "/synthetic/PS3UPDAT.PUP"])
        client.calls[0][2]["error"]({"code": "invalid-key", "message": "Wrong key; choose another JSON file."})
        self.assertIn("Use Back", wizard.import_status.text())
        self.assertEqual(len(client.calls), 1)

    def test_real_qprocess_stdin_private_save(self):
        with tempfile.TemporaryDirectory(prefix="anyps3-key-qt-") as directory:
            client = Client([sys.executable, "-m", "anyps3_manager"], library=str(Path(directory)/"library"), parent=self.app)
            self.addCleanup(client.deleteLater)
            results, errors = [], []
            payload = json.dumps(dict(type=4, revision=1, erk=bytes(range(32)).hex(), riv=bytes(range(16)).hex())).encode()
            client.call(["keys", "set", "app", "--stdin"], results.append, error=errors.append, input_data=payload)
            loop = QEventLoop()
            timer = QTimer()
            timer.timeout.connect(lambda: loop.quit() if not client.calls else None)
            timer.start(5)
            QTimer.singleShot(8000, loop.quit)
            loop.exec()
            timer.stop()
            self.assertFalse(client.calls)
            self.assertEqual(errors, [])
            self.assertIn("Format only", results[0]["keys"]["app"]["validation"])
            files = list((Path(directory)/"library/private-keys").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
