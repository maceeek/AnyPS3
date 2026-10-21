"""Real QProcess→CLI walkthrough, with synthetic disc/tools and isolated data."""
import os
os.environ['SDL_AUDIODRIVER'] = 'dummy'
os.environ['ANYPS3_AUDIO_SINK'] = 'null'
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox
from anyps3_gui.app import Window
from anyps3_gui.client import Client
from anyps3_gui.import_wizard import ImportWizard
from anyps3.sfo import encode_sfo
from anyps3_manager.storage import atomic_json, read_json, remove_tree
from tests.manager import test_manager as fixtures


class RealProtocolWalkthrough(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.fixture = fixtures.ManagerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.lib = self.fixture.lib
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(self.fixture.root/'qt'))
        self.client = Client([sys.executable, '-P', '-m', 'anyps3_manager', '--source', str(fixtures.SOURCE),
                              '--toolchain', str(self.fixture.tools), '--unscoped'], str(self.lib.root), self.app)
        self.window = Window(self.client)
        self.window.show()
        self.addCleanup(self.close)
        self.wait(lambda: not self.client.calls)

    def close(self):
        self.window.close()
        self.client.close()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait(self, condition, timeout=15):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.005)
        self.fail('Timed out: '+self.window.error_box.toPlainText())

    def idle(self):
        self.wait(lambda: not self.client.calls)

    def test_failed_import_resume_build_play_and_save_actions_over_real_protocol(self):
        remove_tree(self.lib.root/'firmware/test')
        wizard = ImportWizard(self.client, self.window)
        wizard.iso.setText(str(self.fixture.iso))
        wizard.no_build.setChecked(True)
        wizard.submitted.connect(self.window.submitted)
        wizard.accept()
        self.wait(lambda: self.window.title_record and self.window.title_record.get('status') == 'incomplete'
                  and self.window.title_record.get('resume_available'))
        self.assertTrue(self.window.resume.isVisible())
        self.assertFalse(self.window.play.isEnabled())
        self.assertIn('missing-firmware', self.window.error_box.toPlainText())
        firmware = self.lib.root/'firmware/test'
        (firmware/'dev_flash/sys/external').mkdir(parents=True)
        atomic_json(firmware/'extraction.json', dict(version='test', pup_sha256='0'*64, files=[]))
        self.window.resume.click()
        self.wait(lambda: self.window.title_record.get('status') == 'imported' and self.window.rebuild.isEnabled())
        self.assertEqual(self.window.rebuild.text(), 'Build')
        self.window.rebuild.click()
        self.wait(lambda: self.window.play.isEnabled() and self.window.title_record.get('active_build'))
        self.idle()
        self.assertEqual(self.window.builds.rowCount(), 1)
        build = self.lib.title('BCES00511')['active_build']
        self.assertTrue((self.lib.title_dir('BCES00511')/'builds'/build/'verification.json').is_file())
        # A synthetic runtime STOP must be visible, then controls recover without manual Refresh.
        self.window.play.click()
        self.wait(lambda: 'guest-stop' in self.window.error_box.toPlainText() and self.window.play.isEnabled())
        self.idle()
        runs = list((self.lib.title_dir('BCES00511')/'runs').glob('*/run.json'))
        self.assertEqual(len(runs), 1)
        record = read_json(runs[0])
        self.assertFalse(record['settings']['audio.enabled'])
        self.assertIn('synthetic unsupported operation', record['stop'])
        self.assertNotIn('verification_status', record)
        # Save UI actions use actual archives and persistent data, not mocked manager responses.
        save = self.lib.title_dir('BCES00511')/'userdata/hdd0/home/00000001/savedata/SAVE001'
        save.mkdir()
        (save/'PARAM.SFO').write_bytes(encode_sfo({'TITLE': 'Synthetic progress'}))
        (save/'DATA').write_bytes(b'progress one')
        self.window.refresh()
        self.wait(lambda: self.window.saves.rowCount() == 1)
        self.idle()
        actions = self.window.saves.actions
        actions['Backup now'].click()
        self.idle()
        backup, = list((self.lib.title_dir('BCES00511')/'backups').glob('*-manual-*.tar.*'))
        (save/'DATA').write_bytes(b'progress two')
        with patch.object(QInputDialog, 'getItem', return_value=(backup.name, True)):
            actions['Restore backup'].click()
            self.idle()
        self.assertEqual((save/'DATA').read_bytes(), b'progress one')
        self.window.saves.selectRow(0)
        export = self.fixture.root/'portable.zip'
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(export), '')):
            actions['Export'].click()
            self.idle()
        self.assertTrue(export.is_file())
        (save/'DATA').write_bytes(b'progress three')
        with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(export), '')):
            actions['Import save'].click()
            self.idle()
        self.assertEqual((save/'DATA').read_bytes(), b'progress one')
        self.window.clear_jobs_button.click()
        self.idle()
        self.assertEqual(self.window.jobs.rowCount(), 0)
        self.assertTrue(self.window.play.isEnabled())
        self.assertEqual((save/'DATA').read_bytes(), b'progress one')
        wizard.deleteLater()


if __name__ == '__main__':
    unittest.main()
