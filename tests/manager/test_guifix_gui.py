"""Independent Qt interaction expectations over a synthetic asynchronous CLI."""
import os
os.environ['SDL_AUDIODRIVER'] = 'dummy'
os.environ['ANYPS3_AUDIO_SINK'] = 'null'
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QItemSelectionModel, QSettings
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox, QWizard, QLineEdit, QPushButton
from anyps3_gui.app import Window, Controllers
from anyps3_gui.import_wizard import ImportWizard
from anyps3_gui.keys import KeyEntry
from tests.gui.guifix_fixture import FixtureClient, Pending, title


class GuiFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.temp = tempfile.TemporaryDirectory(prefix='anyps3-guifix-qt-')
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def settle(self):
        for _ in range(25):
            self.app.processEvents()

    def setUp(self):
        self.client = FixtureClient()
        self.window = Window(self.client)
        self.window.timer.stop()
        self.window.show()
        self.settle()

    def tearDown(self):
        self.settle()
        for widget in list(self.app.topLevelWidgets()):
            widget.close()
        self.settle()

    def show_title(self, record=None):
        self.client.titles = [record or title()]
        self.window.refresh()
        self.settle()

    def test_empty_library_gates_actions_and_clears_removed_title(self):
        self.show_title()
        self.client.titles = []
        self.window.refresh()
        self.settle()
        self.assertEqual(self.window.title_label.text(), 'No title selected')
        self.assertEqual(self.window.builds.rowCount(), 0)
        for button in (self.window.play, self.window.stop, self.window.rebuild):
            self.assertFalse(button.isEnabled())
            self.assertTrue(button.toolTip())
        self.assertFalse(self.window.tabs.isEnabled())

    def test_imported_title_explains_build_then_play(self):
        self.show_title(title('imported', None))
        self.assertFalse(self.window.play.isEnabled())
        self.assertIn('Build', self.window.play.toolTip())
        self.assertTrue(self.window.rebuild.isEnabled())
        self.assertEqual(self.window.rebuild.text(), 'Build')
        self.assertIn('then Play', self.window.action_hint.text())
        self.window.rebuild.click()
        self.settle()
        self.assertIn(['build', 'BCES00511', '--reason', ''], self.client.commands)

    def test_incomplete_resume_and_remove_preserve_userdata_contract(self):
        self.show_title(dict(title('incomplete', None), resume_available=True))
        self.assertFalse(self.window.play.isEnabled())
        self.assertFalse(self.window.rebuild.isEnabled())
        self.assertFalse(self.window.tabs.isEnabled())
        self.window.resume.click()
        self.settle()
        self.assertIn(['titles', 'resume', 'BCES00511'], self.client.commands)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            self.window.remove_title_button.click()
        self.settle()
        self.assertIn(['titles', 'remove', 'BCES00511'], self.client.commands)
        self.assertFalse(any('--delete-userdata' in args for args in self.client.commands))

    def test_legacy_incomplete_can_choose_original_iso(self):
        self.show_title(title('incomplete', None))
        self.window.resume.click()
        self.settle()
        self.assertIsInstance(self.window.dialogs[-1], ImportWizard)
        self.assertIn('Choose ISO', self.window.resume.text())

    def test_title_switch_clears_actions_before_delayed_cli_replies(self):
        self.show_title()
        for table in (self.window.builds, self.window.saves, self.window.runs):
            table.selectRow(0)
        original_call = self.client.call
        delayed = []
        def intercept(args, result=None, event=None, error=None, **kwargs):
            if args[0] == 'builds' or args[:2] == ['saves', 'list'] or (args[0] == 'runs' and len(args) == 2):
                delayed.append((list(args), result))
                return Pending(self.client)
            return original_call(args, result, event, error, **kwargs)
        with patch.object(self.client, 'call', side_effect=intercept):
            self.window.refresh()
            self.settle()
            self.show_title(dict(title(), id='BCES99999'))
            self.assertEqual(self.window.title_id, 'BCES99999')
            # Old A responses arriving after the switch must not repopulate B.
            for args, result in delayed:
                if 'BCES00511' in args:
                    key = 'saves' if args[0] == 'saves' else args[0]
                    result({key: [dict(id='OLD-A', status='built')]})
            for table in (self.window.builds, self.window.saves, self.window.runs):
                self.assertEqual(table.rowCount(), 0)
                self.assertIsNone(table.selected())
            self.assertFalse(self.window.builds.actions['Activate / rollback'].isEnabled())
            self.assertFalse(self.window.builds.actions['Pin'].isEnabled())
            self.assertFalse(self.window.saves.actions['Export'].isEnabled())
            self.assertFalse(self.window.runs.actions['Show run and log'].isEnabled())
            command_count = len(self.client.commands)
            self.window.activate()
            self.window.pin(True)
            self.window.export_save()
            self.window.run_log()
            self.assertEqual(len(self.client.commands), command_count)
            for args, result in delayed:
                if 'BCES99999' in args:
                    key = 'saves' if args[0] == 'saves' else args[0]
                    result({key: [dict(id='NEW-B', status='built')]})
            self.assertEqual(self.window.builds.records[0]['id'], 'NEW-B')
            self.assertIsNone(self.window.builds.selected())

    def test_recovered_title_does_not_inherit_another_titles_settings(self):
        self.show_title()
        self.show_title(dict(title('incomplete', None), id='BCES99999'))
        self.show_title(dict(title(), id='BCES99999'))
        self.assertEqual(self.window.title_settings.title, 'BCES99999')

    def test_busy_title_blocks_conflicts_but_allows_stop_for_run(self):
        self.show_title(dict(title(), busy=True, busy_jobs=[dict(operation='run')]))
        self.assertFalse(self.window.play.isEnabled())
        self.assertFalse(self.window.rebuild.isEnabled())
        self.assertTrue(self.window.stop.isEnabled())
        with patch.object(QMessageBox, 'warning', return_value=QMessageBox.StandardButton.Yes):
            self.window.stop.click()
        self.settle()
        self.assertIn(['stop', 'BCES00511'], self.client.commands)

    def test_foreground_live_run_without_detached_job_enables_stop(self):
        self.show_title(dict(title(), running=True, busy=True, busy_jobs=[]))
        self.assertTrue(self.window.stop.isEnabled())
        self.assertFalse(self.window.play.isEnabled())
        with patch.object(QMessageBox, 'warning', return_value=QMessageBox.StandardButton.Yes):
            self.window.stop.click()
        self.settle()
        self.assertIn(['stop', 'BCES00511'], self.client.commands)

    def test_unchanged_empty_jobs_poll_detects_foreground_run_exit(self):
        self.show_title(dict(title(), running=True, busy=True, busy_jobs=[]))
        self.assertEqual(self.window.job_states, {})
        self.assertTrue(self.window.stop.isEnabled())
        self.assertFalse(self.window.play.isEnabled())
        # A Stop request can return before the supervisor has released its lock.
        self.window.refresh_jobs()
        self.settle()
        self.assertTrue(self.window.stop.isEnabled())
        self.client.titles = [dict(title(), running=False, busy=False, busy_jobs=[])]
        self.window.refresh_jobs()
        self.settle()
        self.assertEqual(self.window.job_states, {})
        self.assertFalse(self.window.stop.isEnabled())
        self.assertTrue(self.window.play.isEnabled())
        self.assertTrue(self.window.rebuild.isEnabled())

    def test_title_polls_do_not_overlap(self):
        before = self.client.commands.count(['titles', 'list'])
        self.window.refresh_jobs()
        self.window.refresh_jobs()
        self.assertEqual(self.client.commands.count(['titles', 'list']), before + 1)
        self.settle()
        self.assertFalse(self.window.titles_polling)
        self.window.refresh_jobs()
        self.settle()
        self.assertEqual(self.client.commands.count(['titles', 'list']), before + 2)

    def test_failed_follower_preserves_error_and_refreshes_title_controls(self):
        self.show_title(dict(title(), busy=True))
        original_call = self.client.call
        callbacks = {}
        pending = Pending(self.client)
        def intercept(args, result=None, event=None, error=None, **kwargs):
            if args[:2] == ['jobs', 'follow']:
                callbacks['error'] = error
                return pending
            return original_call(args, result, event, error, **kwargs)
        with patch.object(self.client, 'call', side_effect=intercept):
            self.window.follow('failed-import')
            self.client.titles = [title()]
            callbacks['error'](dict(code='invalid-key', message='APP key rejected.'))
            pending.done.emit()
            self.settle()
        self.assertIn('invalid-key: APP key rejected.', self.window.error_box.toPlainText())
        self.assertTrue(self.window.play.isEnabled())
        self.assertTrue(self.window.rebuild.isEnabled())
        self.assertNotIn('failed-import', self.window.followers)

    def test_polled_terminal_job_refreshes_title_after_missed_follower(self):
        for terminal in ('failed', 'cancelled'):
            with self.subTest(state=terminal):
                self.show_title(dict(title(), busy=True))
                self.window.job_states = {'external-job': 'running'}
                self.client.titles = [title()]
                self.window.fill_jobs({'jobs': [dict(id='external-job', state=terminal)]})
                self.settle()
                self.assertTrue(self.window.play.isEnabled())
                self.assertTrue(self.window.rebuild.isEnabled())
                self.assertFalse(self.window.cancel_button.isEnabled())

    def test_play_uses_detached_cli_and_idle_stop_is_disabled(self):
        self.show_title(dict(title(), next_action="play"))
        self.assertIn("Ready to Play", self.window.action_hint.text())
        self.assertTrue(self.window.play.isEnabled())
        self.assertFalse(self.window.stop.isEnabled())
        self.window.play.click()
        self.settle()
        self.assertIn(['run', 'BCES00511', '--detach'], self.client.commands)

    def test_build_activate_pin_prune_and_selection_gates(self):
        self.show_title()
        actions = self.window.builds.actions
        self.assertFalse(actions['Activate / rollback'].isEnabled())
        self.window.builds.selectRow(0)
        actions['Activate / rollback'].click()
        self.settle()
        actions['Pin'].click()
        self.settle()
        actions['Unpin'].click()
        self.settle()
        with patch.object(QInputDialog, 'getInt', return_value=(2, True)):
            actions['Prune'].click()
        self.settle()
        for command in (['activate', 'BCES00511', 'build-001'], ['pin', 'BCES00511', 'build-001'],
                        ['unpin', 'BCES00511', 'build-001'], ['prune', 'BCES00511', '--keep', '2']):
            self.assertIn(command, self.client.commands)

    def test_saves_backup_restore_export_import(self):
        self.show_title()
        actions = self.window.saves.actions
        self.assertFalse(actions['Export'].isEnabled())
        actions['Backup now'].click()
        self.settle()
        with patch.object(QInputDialog, 'getItem', return_value=('backup-001', True)):
            actions['Restore backup'].click()
            self.settle()
        self.window.saves.selectRow(0)
        with patch.object(QFileDialog, 'getSaveFileName', return_value=('/synthetic/save.zip', '')):
            actions['Export'].click()
        self.settle()
        with patch.object(QFileDialog, 'getOpenFileName', return_value=('/synthetic/save.zip', '')):
            actions['Import save'].click()
        self.settle()
        for command in (['saves', 'backup', 'BCES00511'], ['saves', 'restore', 'BCES00511', 'backup-001'],
                        ['saves', 'export', 'BCES00511', 'SAVE001', '/synthetic/save.zip'],
                        ['saves', 'import', 'BCES00511', '/synthetic/save.zip']):
            self.assertIn(command, self.client.commands)

    def test_jobs_multiselect_remove_preserves_selection_on_refresh(self):
        jobs = [dict(id='done', state='succeeded'), dict(id='failed', state='failed'), dict(id='live', state='running')]
        self.window.jobs.fill(jobs)
        selection = self.window.jobs.selectionModel()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for row in (0, 1):
            selection.select(self.window.jobs.model().index(row, 0), flags)
        self.window.jobs.fill(jobs)
        self.assertEqual([r['id'] for r in self.window.jobs.selected_records()], ['done', 'failed'])
        self.assertTrue(self.window.remove_jobs_button.isEnabled())
        self.assertFalse(self.window.cancel_button.isEnabled())
        self.window.remove_jobs_button.click()
        self.settle()
        self.assertIn(['jobs', 'remove', 'done', 'failed'], self.client.commands)
        self.window.jobs.fill(jobs)
        selection.select(self.window.jobs.model().index(2, 0), flags)
        self.assertFalse(self.window.remove_jobs_button.isEnabled())
        self.window.jobs.clearSelection()
        self.window.jobs.selectRow(2)
        self.assertTrue(self.window.cancel_button.isEnabled())
        self.window.cancel_button.click()
        self.settle()
        self.assertIn(['jobs', 'cancel', 'live'], self.client.commands)

    def test_clear_finished_jobs_uses_terminal_only_cli(self):
        self.window.jobs.fill([dict(id='done', state='succeeded')])
        self.window.clear_jobs_button.click()
        self.settle()
        self.assertIn(['jobs', 'clear'], self.client.commands)

    def test_settings_audio_rumble_and_advanced_schema(self):
        self.show_title()
        settings = self.window.title_settings
        self.assertFalse(settings.editors['audio.enabled'].isChecked())
        settings.editors['audio.enabled'].setChecked(True)
        settings.save('audio.enabled', settings.editors['audio.enabled'].isChecked)
        self.settle()
        self.assertIn(['settings', 'set', '--title', 'BCES00511', 'audio.enabled', 'true'], self.client.commands)
        settings.tabs.setCurrentIndex(1)
        settings.editors['input.rumble'].setChecked(False)
        settings.save('input.rumble', settings.editors['input.rumble'].isChecked)
        self.settle()
        self.assertIn(['settings', 'set', '--title', 'BCES00511', 'input.rumble', 'false'], self.client.commands)
        self.assertEqual(settings.tabs.tabText(settings.tabs.currentIndex()), 'Input')
        self.assertNotIn('trace.enabled', settings.editors)
        settings.advanced.setChecked(True)
        self.assertIn('trace.enabled', settings.editors)

    def test_same_title_build_change_refreshes_setting_capabilities(self):
        self.client.schema['input.rumble']['available'] = False
        self.show_title(dict(title(), active_build='build-no-rumble'))
        settings = self.window.title_settings
        settings.tabs.setCurrentIndex(1)
        self.assertFalse(settings.editors['input.rumble'].isEnabled())
        self.client.schema['input.rumble']['available'] = True
        self.show_title(dict(title(), active_build='build-with-rumble'))
        self.assertIs(self.window.title_settings, settings)
        self.assertTrue(settings.editors['input.rumble'].isEnabled())
        self.assertEqual(settings.tabs.tabText(settings.tabs.currentIndex()), 'Input')
        self.client.schema['input.rumble']['available'] = False
        self.show_title(dict(title(), active_build='build-no-rumble'))
        self.assertFalse(settings.editors['input.rumble'].isEnabled())
        self.assertEqual(settings.tabs.tabText(settings.tabs.currentIndex()), 'Input')

    def test_controller_rumble_requires_supported_selected_device(self):
        dialog = Controllers(self.client, self.window)
        self.settle()
        self.assertFalse(dialog.rumble_button.isEnabled())
        self.assertFalse(dialog.stop_button.isEnabled())
        dialog.table.selectRow(0)
        self.assertTrue(dialog.rumble_button.isEnabled())
        dialog.rumble_button.click()
        self.settle()
        self.assertIn(['pads', 'rumble-test', '0'], self.client.commands)
        dialog.table.fill([dict(id='0', name='No vibration', rumble=False)])
        self.assertFalse(dialog.rumble_button.isEnabled())
        dialog.close()

    def test_wizard_file_selection_scan_all_pages_and_masks_keys(self):
        wizard = ImportWizard(self.client, self.window)
        wizard.show()
        self.settle()
        self.assertFalse(wizard.button(QWizard.WizardButton.NextButton).isEnabled())
        with patch.object(QFileDialog, 'getOpenFileName', return_value=('/synthetic/disc.iso', '')):
            wizard.choose(wizard.iso, 'PS3 disc (*.iso)')
        wizard.next()
        self.settle()
        self.assertEqual(wizard.scan['id'], 'BCES00511')
        wizard.next()
        self.assertEqual(wizard.currentId(), 2)
        for kind in ('pup', 'app'):
            entry = KeyEntry(kind, wizard)
            self.assertTrue(all(edit.echoMode() == QLineEdit.EchoMode.Password for edit in entry.values.values()))
            entry.close()
        wizard.next()
        self.assertEqual(wizard.currentId(), 3)
        wizard.no_build.setChecked(True)
        wizard.accept()
        wizard.accept()
        self.settle()
        self.assertEqual(self.client.commands.count(['import', '/synthetic/disc.iso', '--no-build']), 1)
        wizard.close()

    def test_selected_iso_path_expands_instead_of_becoming_a_narrow_label(self):
        wizard = ImportWizard(self.client, self.window)
        wizard.show()
        self.settle()
        path = '/synthetic/owned-disc-backups/Ratchet and Clank A Crack in Time (Europe)/disc.complete.iso'
        with patch.object(QFileDialog, 'getOpenFileName', return_value=(path, '')):
            wizard.choose(wizard.iso, 'PS3 disc (*.iso)')
        self.settle()
        chooser = next(button for button in wizard.page(0).findChildren(QPushButton) if button.text() == 'Choose ISO…')
        self.assertGreater(wizard.iso.width(), chooser.width() * 2)
        self.assertEqual(wizard.iso.toolTip(), path)
        self.assertEqual(wizard.iso.text(), path)
        self.assertTrue(wizard.button(QWizard.WizardButton.NextButton).isEnabled())
        wizard.close()

    def test_error_display_retains_cli_code_and_message(self):
        self.window.show_error(dict(code='invalid-key', message='Selected APP key does not match SELF.'))
        self.assertEqual(self.window.error_box.toPlainText().strip(), 'invalid-key: Selected APP key does not match SELF.')
