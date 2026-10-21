"""Read-only offscreen GUI observation of the temporary real-game library.

Run in a silent 8G systemd scope, with GUIFIX_REAL_STATE=building|built|running|stopped.
This never clicks Play or changes settings; all manager calls are observations.
"""
import ast
import os
from pathlib import Path
import sys
import tempfile
import unittest

from PySide6.QtCore import QEventLoop, QSettings, QTimer
from PySide6.QtWidgets import QApplication
from anyps3_gui.app import Window
from anyps3_gui.client import Client


class RealLibraryObservation(unittest.TestCase):
    def test_observe_temporary_library(self):
        app = QApplication.instance() or QApplication([])
        app.setStyle('Fusion')
        source = Path(__file__).resolve().parents[4]/'anyps3_gui/app.py'
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'setStyleSheet' and isinstance(node.args[0], ast.Constant) and 'QMainWindow' in str(node.args[0].value):
                app.setStyleSheet(node.args[0].value)
                break
        state = os.environ.get('GUIFIX_REAL_STATE', 'building')
        with tempfile.TemporaryDirectory(prefix='anyps3-guifix-real-qt-') as config:
            QSettings.setDefaultFormat(QSettings.Format.IniFormat)
            QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, config)
            client = Client([sys.executable, '-P', '-m', 'anyps3_manager'], '/var/tmp/anyps3-e2e-lib', app)
            errors = []
            client.error.connect(errors.append)
            window = Window(client)
            window.show()
            loop, timer = QEventLoop(), QTimer()
            def ready():
                record = window.title_record
                if not record or record.get('id') != 'BCES00511':
                    return
                if state == 'running' and not record.get('running'):
                    return
                if state in ('built', 'stopped') and record.get('busy'):
                    return
                timer.stop()
                QTimer.singleShot(150, loop.quit)
            timer.timeout.connect(ready)
            timer.start(50)
            QTimer.singleShot(12000, loop.quit)
            loop.exec()
            timer.stop()
            self.assertEqual(window.title_id, 'BCES00511')
            self.assertFalse(errors, errors)
            if state == 'running':
                self.assertTrue(window.stop.isEnabled())
                self.assertFalse(window.play.isEnabled())
            if state in ('built', 'stopped'):
                self.assertTrue(window.play.isEnabled())
                self.assertFalse(window.stop.isEnabled())
            self.assertTrue(window.grab().save(str(Path(__file__).parent/('real-'+state+'.png'))))
            window.close()
            client.close()
            for call in tuple(client.calls):
                call.process.waitForFinished(3000)
            app.processEvents()
