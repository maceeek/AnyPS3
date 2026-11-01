"""Reproducible synthetic GUI captures. Run as a memory-capped unittest.

SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null GUIFIX_PHASE=after \
 systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
 env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null QT_QPA_PLATFORM=offscreen \
 python3 -m unittest discover -s docs/manager/evidence/guifix -p capture.py

All titles, jobs and settings are in-memory fixtures. No key values are entered.
"""
import ast
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from PySide6.QtCore import QItemSelectionModel, QSettings
from PySide6.QtWidgets import QApplication, QFileDialog, QWizard
if os.environ.get('GUIFIX_SOURCE'):
    sys.path.insert(0, os.environ['GUIFIX_SOURCE'])
from anyps3_gui.app import Window, Controllers
from anyps3_gui.import_wizard import ImportWizard
from anyps3_gui.keys import KeyEntry
from tests.gui.guifix_fixture import FixtureClient, title


class Capture(unittest.TestCase):
    def test_walkthrough(self):
        self.app = QApplication.instance() or QApplication([])
        self.app.setStyle('Fusion')
        import anyps3_gui.app as gui_module
        for node in ast.walk(ast.parse(Path(gui_module.__file__).read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'setStyleSheet' and node.args and isinstance(node.args[0], ast.Constant):
                if 'QMainWindow' in str(node.args[0].value):
                    self.app.setStyleSheet(node.args[0].value)
        with tempfile.TemporaryDirectory(prefix='anyps3-guifix-capture-') as config:
            QSettings.setDefaultFormat(QSettings.Format.IniFormat)
            QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, config)
            out = Path(__file__).parent / os.environ.get('GUIFIX_PHASE', 'after')
            out.mkdir(exist_ok=True)
            def settle():
                for _ in range(20):
                    self.app.processEvents()
            def shot(widget, name):
                widget.show()
                settle()
                self.assertTrue(widget.grab().save(str(out / (name + '.png'))))
            client = FixtureClient()
            window = Window(client)
            window.timer.stop()
            shot(window, '01-empty-library')
            for name, record in [('02-incomplete', title('incomplete', None)), ('03-imported-no-build', title('imported', None)),
                                 ('04-built', title()), ('05-stale', dict(title(), stale=True, staleness_reasons=['runtime changed']))]:
                client.titles = [record]
                window.refresh()
                settle()
                shot(window, name)
            if os.environ.get('GUIFIX_PHASE', 'after') == 'after':
                client.titles = [dict(title('incomplete', None), resume_available=True)]
                window.refresh()
                settle()
                shot(window, '02-incomplete-resumable')
                client.titles = [title()]
                window.refresh()
                settle()
            for index, name in [(0, '06-builds'), (1, '07-saves'), (2, '08-runs'), (3, '09-title-settings')]:
                window.tabs.setCurrentIndex(index)
                if index < 3:
                    (window.builds, window.saves, window.runs)[index].selectRow(0)
                shot(window, name)
            client.jobs = [dict(id='job-failed', operation='import', state='failed', stage='decrypt'),
                           dict(id='job-completed', operation='build', state='succeeded', stage='done')]
            window.fill_jobs({'jobs': client.jobs})
            shot(window, '10-jobs')
            if os.environ.get('GUIFIX_PHASE', 'after') == 'after':
                flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
                for row in (0, 1):
                    window.jobs.selectionModel().select(window.jobs.model().index(row, 0), flags)
                shot(window, '10-jobs-multiselect')
            client.titles = [dict(title(), busy=True, busy_jobs=[dict(operation='run')])]
            window.refresh()
            settle()
            shot(window, '10-running-title')
            window.show_error(dict(code='invalid-key', message='APP key did not validate against the selected SELF.'))
            shot(window, '11-actionable-error')
            window.global_settings()
            settle()
            shot(window.dialogs[-1], '12-global-settings')
            window.dialogs[-1].close()
            controllers = Controllers(client, window)
            shot(controllers, '13-controllers')
            if os.environ.get('GUIFIX_PHASE', 'after') == 'after':
                controllers.table.fill([])
                shot(controllers, '13-controllers-empty')
            controllers.close()
            wizard = ImportWizard(client, window)
            shot(wizard, '14-wizard-iso')
            with patch.object(QFileDialog, 'getOpenFileName', return_value=('/synthetic/owned-disc-backups/Ratchet and Clank A Crack in Time (Europe)/disc.complete.iso', '')):
                wizard.choose(wizard.iso, 'PS3 disc (*.iso)')
            shot(wizard, '14-wizard-iso-selected')
            wizard.next()
            settle()
            shot(wizard, '15-wizard-identity')
            wizard.next()
            shot(wizard, '16-wizard-keys')
            from PySide6.QtWidgets import QScrollArea
            area = wizard.page(2).findChild(QScrollArea)
            area.verticalScrollBar().setValue(area.verticalScrollBar().maximum())
            shot(wizard, '16-wizard-keys-lower')
            for kind in ('pup', 'app'):
                entry = KeyEntry(kind, wizard)
                shot(entry, '17-key-' + kind)
                entry.close()
            wizard.next()
            shot(wizard, '18-wizard-confirm')
            wizard.import_status.setText('invalid-key: Select valid APP key JSON. Use Back to retry.')
            shot(wizard, '19-wizard-error')
            wizard.close()
            chooser = QFileDialog(window, 'Choose a complete PS3 disc ISO', config, 'PS3 disc (*.iso)')
            chooser.setOption(QFileDialog.Option.DontUseNativeDialog)
            chooser.setFileMode(QFileDialog.FileMode.ExistingFile)
            shot(chooser, '20-file-selection')
            chooser.close()
            window.close()
            settle()
