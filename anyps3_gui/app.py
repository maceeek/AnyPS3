"""AnyPS3 desktop library. The CLI is the sole owner of library state."""

import argparse
import json
import sys

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget)

from .client import Client
from .import_wizard import ImportWizard
from .settings import Settings


def button(text, action, layout):
    item = QPushButton(text)
    item.clicked.connect(action)
    layout.addWidget(item)
    return item


class Table(QTableWidget):
    def __init__(self, columns):
        super().__init__(0, len(columns))
        self.columns = columns
        self.setHorizontalHeaderLabels([label for key, label in columns])
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.records = []
        self.verticalHeader().hide()
        self.setAlternatingRowColors(True)

    def fill(self, records):
        selected = {r.get("id") for r in self.selected_records()}
        self.blockSignals(True)
        self.clearSelection()
        self.records = records
        self.setRowCount(len(records))
        for row, record in enumerate(records):
            for col, (key, label) in enumerate(self.columns):
                value = record.get(key)
                self.setItem(row, col, QTableWidgetItem("" if value is None else str(value)))
            if record.get("id") in selected:
                from PySide6.QtCore import QItemSelectionModel
                self.selectionModel().select(self.model().index(row, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self.blockSignals(False)
        self.itemSelectionChanged.emit()

    def selected_records(self):
        return [self.records[i.row()] for i in self.selectionModel().selectedRows() if i.row() < len(self.records)]

    def selected(self):
        records = self.selected_records()
        return records[0] if records else None


class Controllers(QDialog):
    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self.monitor = None
        self.setWindowTitle("Controllers")
        self.resize(850, 600)
        layout = QVBoxLayout(self)
        note = QLabel("SDL controllers • Xbox class and DualSense\n"
                      "Device diagnostics do not prove input in the game. Physical-controller acceptance is still required.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = Table([("name", "Controller"), ("guid", "GUID"), ("serial", "Serial"),
                            ("rumble", "Rumble"), ("motion", "Motion (accel + gyro)"), ("battery", "Battery")])
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        button("Refresh", self.refresh, actions)
        self.monitor_button = button("Live input", self.start_monitor, actions)
        self.stop_button = button("Stop monitor", self.stop_monitor, actions)
        self.rumble_button = button("Rumble test", self.rumble, actions)
        self.table.itemSelectionChanged.connect(self.update_actions)
        self.update_actions()
        self.live = QPlainTextEdit()
        self.live.setReadOnly(True)
        self.live.setMaximumBlockCount(100)
        layout.addWidget(self.live)
        self.config_note = QLabel("Game rumble follows Input → Allow game controller vibration; motion requires both sensors.\n"
                                 "Port assignment, remapping and dead zones require the separate pad-config runtime package.\n"
                                 "They are unavailable in the current runtime. Default SDL mapping remains active.")
        self.config_note.setWordWrap(True)
        layout.addWidget(self.config_note)
        self.refresh()

    def refresh(self):
        self.client.call(["pads", "list"], lambda result: self.table.fill(result["pads"]))

    def update_actions(self):
        self.monitor_button.setEnabled(self.monitor is None)
        self.stop_button.setEnabled(self.monitor is not None)
        self.stop_button.setToolTip("Start Live input first." if self.monitor is None else "Stop live input monitoring.")
        selected = self.table.selected()
        supported = bool(selected and selected.get("rumble"))
        self.rumble_button.setEnabled(supported)
        self.rumble_button.setToolTip("Select a controller with rumble support." if not supported else "Test vibration on the selected controller.")

    def monitor_done(self):
        self.monitor = None
        self.update_actions()

    def start_monitor(self):
        if self.monitor:
            return
        self.monitor = self.client.call(["pads", "monitor"], event=self.input)
        self.monitor.done.connect(self.monitor_done)
        self.update_actions()

    def input(self, data):
        self.table.fill(data.get("pads", []))
        self.live.setPlainText(json.dumps(data.get("pads", []), indent=2))

    def stop_monitor(self):
        if self.monitor:
            self.monitor.process.terminate()

    def rumble(self):
        if record := self.table.selected():
            self.client.call(["pads", "rumble-test", record["id"]])

    def closeEvent(self, event):
        self.stop_monitor()
        super().closeEvent(event)


class Window(QMainWindow):
    def __init__(self, client):
        super().__init__()
        self.client = client
        self.closing = False
        self.title_id = None
        self.title_record = None
        self.followers = {}
        self.job_states = {}
        self.dialogs = []
        self.polling = False
        self.titles_polling = False
        self.setWindowTitle("AnyPS3")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1220, 820)
        self.preferences = QSettings("AnyPS3", "Manager")
        geometry = self.preferences.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.client.error.connect(self.show_error)
        main = QWidget()
        self.setCentralWidget(main)
        layout = QVBoxLayout(main)
        top = QHBoxLayout()
        heading = QLabel("AnyPS3  /  Game library")
        heading.setObjectName("heading")
        top.addWidget(heading, 1)
        button("Import ISO", self.import_iso, top)
        button("Adopt Ratchet", self.adopt, top)
        button("Controllers", lambda: self.dialog(Controllers(self.client, self)), top)
        button("Settings", self.global_settings, top)
        button("Refresh", self.refresh, top)
        layout.addLayout(top)
        self.error_box = QPlainTextEdit()
        self.error_box.setReadOnly(True)
        self.error_box.setMaximumHeight(120)
        self.error_box.setObjectName("error")
        self.error_box.hide()
        layout.addWidget(self.error_box)
        split = QSplitter()
        layout.addWidget(split, 1)
        self.library = QListWidget()
        self.library.setMinimumWidth(280)
        self.library.currentItemChanged.connect(self.select_title)
        split.addWidget(self.library)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        self.background = QLabel("Import a PS3 disc to get started")
        self.background.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.background.setMaximumHeight(160)
        detail_layout.addWidget(self.background)
        self.title_label = QLabel("No title selected")
        self.title_label.setObjectName("title")
        detail_layout.addWidget(self.title_label)
        self.status = QLabel()
        self.status.setWordWrap(True)
        detail_layout.addWidget(self.status)
        self.action_hint = QLabel("Import ISO or adopt an existing setup to add a title.")
        self.action_hint.setWordWrap(True)
        self.action_hint.setStyleSheet("background: #e8f1ff; color: #183d69; padding: 8px; border-radius: 4px;")
        detail_layout.addWidget(self.action_hint)
        recovery = QHBoxLayout()
        self.resume = button("Resume import", self.resume_title, recovery)
        self.remove_title_button = button("Remove incomplete title", self.remove_title, recovery)
        recovery.addStretch()
        detail_layout.addLayout(recovery)
        controls = QHBoxLayout()
        self.play = button("Play", self.run, controls)
        self.stop = button("Stop", self.stop_run, controls)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Rebuild reason (optional)")
        controls.addWidget(self.reason, 1)
        self.rebuild = button("Rebuild", self.build, controls)
        detail_layout.addLayout(controls)
        self.tabs = QTabWidget()
        detail_layout.addWidget(self.tabs, 1)
        self.builds = Table([("id", "Build"), ("status", "Status"), ("reason", "Reason"), ("duration_seconds", "Seconds")])
        self.add_table_tab("Builds", self.builds, [
            ("Activate / rollback", self.activate), ("Pin", lambda: self.pin(True)),
            ("Unpin", lambda: self.pin(False)), ("Prune", self.prune), ("Logs", self.build_logs)])
        self.saves = Table([("id", "Save"), ("title", "Title"), ("subtitle", "Subtitle"), ("size", "Bytes")])
        self.add_table_tab("Saves", self.saves, [
            ("Backup now", lambda: self.title_call(["saves", "backup"])), ("Restore backup", self.restore),
            ("Export", self.export_save), ("Import save", self.import_save)])
        self.runs = Table([("id", "Run"), ("build", "Build"), ("fps_mean", "Mean FPS"), ("stop", "Guest STOP")])
        self.add_table_tab("Runs", self.runs, [("Show run and log", self.run_log)])
        self.title_settings = None
        self.title_settings_build = None
        for table in (self.builds, self.saves, self.runs):
            table.itemSelectionChanged.connect(self.update_table_actions)
        split.addWidget(detail)
        split.setStretchFactor(1, 1)
        self.jobs = Table([("id", "Job"), ("operation", "Operation"), ("state", "State"), ("stage", "Stage")])
        self.jobs.setMaximumHeight(150)
        self.jobs.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.jobs.itemSelectionChanged.connect(self.update_job_actions)
        layout.addWidget(QLabel("Jobs · continue running when this window closes"))
        layout.addWidget(self.jobs)
        actions = QHBoxLayout()
        self.follow_button = button("Follow selected", self.follow_selected, actions)
        self.cancel_button = button("Cancel job", self.cancel_job, actions)
        self.job_log_button = button("Open job log", self.job_log, actions)
        self.remove_jobs_button = button("Remove selected", self.remove_jobs, actions)
        self.clear_jobs_button = button("Clear finished", self.clear_jobs, actions)
        self.progress = QProgressBar()
        actions.addWidget(self.progress, 1)
        layout.addLayout(actions)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        self.log.setMaximumBlockCount(2000)
        layout.addWidget(self.log)
        self.timer = QTimer(self)
        self.timer.setInterval(2500)
        self.timer.timeout.connect(self.refresh_jobs)
        self.timer.start()
        self.select_title(None)
        self.update_job_actions()
        self.client.call(["init"], lambda _: self.refresh())

    def add_table_tab(self, name, table, actions):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(table)
        row = QHBoxLayout()
        table.actions = {}
        for label, action in actions:
            table.actions[label] = button(label, action, row)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(widget, name)

    def update_table_actions(self):
        ready = bool(self.title_record and self.title_record.get("status") != "incomplete")
        busy = bool(self.title_record and self.title_record.get("busy"))
        selected_build = self.builds.selected()
        for label, action in self.builds.actions.items():
            enabled = ready and not busy and (label == "Prune" or selected_build is not None)
            if label == "Activate / rollback" and selected_build:
                enabled = enabled and selected_build.get("status") in {"built", "verified-boot", "verified-play"}
            if label == "Logs":
                enabled = ready and bool(selected_build and selected_build.get("logs"))
            action.setEnabled(enabled)
            action.setToolTip("Wait for the active title operation to finish." if busy else "Select a build first." if not selected_build and label != "Prune" else "Manage the selected build.")
        for label, action in self.saves.actions.items():
            action.setEnabled(ready and not busy and (label != "Export" or self.saves.selected() is not None))
            action.setToolTip("Wait for the active title operation to finish." if busy else "Select a save to export." if label == "Export" else "Manage save data for the selected title.")
        self.runs.actions["Show run and log"].setEnabled(ready and self.runs.selected() is not None)
        self.runs.actions["Show run and log"].setToolTip("Select a run to view its evidence and log.")

    def dialog(self, dialog):
        self.dialogs.append(dialog)
        dialog.finished.connect(lambda: self.dialogs.remove(dialog))
        dialog.show()

    def show_error(self, error):
        detail = error.get("detail", {})
        self.error_box.setPlainText(f"{error.get('code', 'error')}: {error.get('message', '')}\n"
                                   + (json.dumps(detail, indent=2) if detail else ""))
        self.error_box.show()

    def refresh(self):
        self.refresh_jobs()

    def refresh_titles(self):
        if self.closing or self.titles_polling:
            return
        self.titles_polling = True
        call = self.client.call(["titles", "list"], self.fill_titles)
        call.done.connect(lambda: setattr(self, "titles_polling", False))

    def fill_titles(self, data):
        chosen = self.title_id
        self.library.blockSignals(True)
        self.library.clear()
        for title in data["titles"]:
            status = "stale" if title.get("stale") else title["status"]
            item = QListWidgetItem(QIcon(title.get("icon", "")), f"{title['title']}\n{title['id']} · {title.get('version', '')} · {status}")
            item.setData(Qt.ItemDataRole.UserRole, title)
            self.library.addItem(item)
            if title["id"] == chosen:
                self.library.setCurrentItem(item)
        if self.library.currentRow() < 0 and self.library.count():
            self.library.setCurrentRow(0)
        self.library.blockSignals(False)
        self.select_title(self.library.currentItem())

    def select_title(self, item, previous=None):
        if not item:
            self.title_id = None
            self.title_record = None
            self.title_label.setText("No title selected")
            self.status.clear()
            self.background.clear()
            self.background.hide()
            self.play.setEnabled(False)
            self.play.setToolTip("Select a title with an active build.")
            self.stop.setEnabled(False)
            self.stop.setToolTip("No game is running.")
            self.rebuild.setEnabled(False)
            self.rebuild.setToolTip("Select an imported title first.")
            self.resume.hide()
            self.remove_title_button.hide()
            self.reason.setEnabled(False)
            self.tabs.setEnabled(False)
            self.builds.fill([])
            self.saves.fill([])
            self.runs.fill([])
            self.action_hint.setText("Import ISO or adopt an existing setup to add a title.")
            self.update_table_actions()
            return
        title = item.data(Qt.ItemDataRole.UserRole)
        changed = self.title_id != title["id"]
        if changed:
            for table in (self.builds, self.saves, self.runs):
                table.fill([])
        self.title_id = title["id"]
        self.title_record = title
        self.title_label.setText(title["title"])
        self.status.setText(f"{title['id']} · {title.get('status')} · Active build: {title.get('active_build') or 'none'}\n"
            f"{title.get('compatibility', '')}\n"
            f"Last played: {title.get('last_played', 'never')} · Play time: {title.get('play_seconds', 0)/3600:.2f} h\n"
            + ("Rebuild recommended: "+", ".join(title["staleness_reasons"]) if title.get("stale") else ""))
        pixmap = QPixmap(title.get("background", ""))
        self.background.setVisible(not pixmap.isNull())
        if pixmap.isNull():
            self.background.clear()
        else:
            self.background.setPixmap(pixmap.scaled(800, 160, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        incomplete = title["status"] == "incomplete"
        busy = bool(title.get("busy"))
        running = bool(title.get("running")) or any(isinstance(j, dict) and j.get("operation") == "run" for j in title.get("busy_jobs", []))
        self.play.setEnabled(bool(title.get("active_build")) and not busy)
        self.play.setToolTip("Wait for the active job to finish." if busy else "Build this title first." if not title.get("active_build") else "Launch the active build.")
        self.stop.setEnabled(running)
        self.stop.setToolTip("Stop the running game after saving." if running else "No game is running for this title.")
        self.rebuild.setText("Rebuild" if title.get("active_build") else "Build")
        self.rebuild.setEnabled(not incomplete and not busy)
        self.rebuild.setToolTip("Resume the incomplete import first." if incomplete else "Wait for the active job to finish." if busy else "Create and activate a new native build.")
        self.reason.setEnabled(not incomplete and not busy)
        self.resume.setVisible(incomplete)
        self.resume.setEnabled(not busy)
        self.resume.setText("Resume import" if title.get("resume_available") else "Choose ISO to resume…")
        self.remove_title_button.setVisible(incomplete)
        self.remove_title_button.setEnabled(not busy)
        self.tabs.setEnabled(not incomplete)
        if busy:
            hint = "A title operation is active. Follow its progress in Jobs."
        elif incomplete:
            hint = "Import is incomplete. Resume it or remove this entry; saved data is preserved."
        elif not title.get("active_build"):
            hint = "Next: Build this title, then Play. Import finished; a native build is required to play."
        else:
            hint = "Ready to Play. Use Builds to activate an earlier build or create a new one."
        self.action_hint.setText(hint)
        self.update_table_actions()
        if incomplete:
            self.builds.fill([])
            self.saves.fill([])
            self.runs.fill([])
            return
        selected = self.title_id
        for command, table, key in ((["builds", selected], self.builds, "builds"),
                                    (["saves", "list", selected], self.saves, "saves"),
                                    (["runs", selected], self.runs, "runs")):
            self.client.call(command, lambda data, t=table, k=key, s=selected: t.fill(data[k]) if self.title_id == s else None)
        if changed or self.title_settings is None or self.title_settings.title != selected:
            if self.title_settings:
                self.tabs.removeTab(self.tabs.indexOf(self.title_settings))
                self.title_settings.deleteLater()
            self.title_settings = Settings(self.client, selected)
            self.tabs.addTab(self.title_settings, "Settings")
        elif self.title_settings_build != title.get("active_build"):
            self.title_settings.load()
        self.title_settings_build = title.get("active_build")

    def refresh_jobs(self):
        if self.closing:
            return
        self.refresh_titles()
        if self.polling:
            return
        self.polling = True
        call = self.client.call(["jobs", "list"], self.fill_jobs)
        call.done.connect(lambda: setattr(self, "polling", False))

    def fill_jobs(self, result):
        states = {job["id"]: job["state"] for job in result["jobs"]}
        if states != self.job_states:
            self.job_states = states
            self.refresh_titles()
        self.jobs.fill(result["jobs"])
        self.update_job_actions()
        for job in result["jobs"]:
            if job["state"] in ("running", "queued") and job["id"] not in self.followers:
                self.follow(job["id"])

    def update_job_actions(self):
        selected = self.jobs.selected_records()
        active = {"queued", "running"}
        terminal = {"succeeded", "failed", "cancelled"}
        one = len(selected) == 1
        self.follow_button.setEnabled(one)
        self.job_log_button.setEnabled(one)
        self.cancel_button.setEnabled(one and selected[0].get("state") in active)
        self.cancel_button.setToolTip("Select one queued or running job to cancel.")
        self.remove_jobs_button.setEnabled(bool(selected) and all(r.get("state") in terminal for r in selected))
        self.remove_jobs_button.setToolTip("Select finished jobs. Active jobs cannot be removed.")
        self.clear_jobs_button.setEnabled(any(r.get("state") in terminal for r in self.jobs.records))
        self.clear_jobs_button.setToolTip("Remove completed, failed and cancelled job history; active jobs remain.")
        self.follow_button.setToolTip("Select exactly one job to follow.")
        self.job_log_button.setToolTip("Select exactly one job to open its log.")

    def remove_jobs(self):
        selected = self.jobs.selected_records()
        if selected and all(r.get("state") in {"succeeded", "failed", "cancelled"} for r in selected):
            self.client.call(["jobs", "remove", *[r["id"] for r in selected]], lambda _: self.refresh_jobs())

    def clear_jobs(self):
        self.client.call(["jobs", "clear"], lambda _: self.refresh_jobs())

    def resume_title(self):
        if self.title_record and not self.title_record.get("busy"):
            if self.title_record.get("resume_available"):
                self.client.call(["titles", "resume", self.title_id], self.submitted)
            else:
                self.import_iso()

    def remove_title(self):
        if self.title_record and self.title_record.get("status") == "incomplete" and not self.title_record.get("busy"):
            answer = QMessageBox.question(self, "Remove incomplete title", "Remove this incomplete library entry? Saved data and backups are preserved.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.title_call(["titles", "remove"])

    def follow(self, job_id):
        if job_id in self.followers:
            return
        call = self.client.call(["jobs", "follow", job_id], self.job_finished, self.job_event, error=self.job_failed)
        self.followers[job_id] = call
        call.done.connect(lambda: self.followers.pop(job_id, None))

    def job_event(self, event):
        self.log.appendPlainText(json.dumps(event, ensure_ascii=False))
        if event.get("event") == "progress":
            self.progress.setRange(0, max(1, event.get("total", 1)))
            self.progress.setValue(event.get("done", 0))
        elif event.get("event") == "stage":
            self.progress.setRange(0, event.get("count", 1))
            self.progress.setValue(event.get("index", 0))
            self.progress.setFormat(event["stage"]+" %p%")

    def job_failed(self, error):
        if self.closing:
            return
        self.show_error(error)
        self.refresh()

    def job_finished(self, result):
        self.log.appendPlainText(json.dumps(result, ensure_ascii=False))
        self.refresh()

    def submitted(self, result):
        if result.get("job"):
            self.follow(result["job"])
        self.refresh()

    def import_iso(self):
        wizard = ImportWizard(self.client, self)
        wizard.submitted.connect(self.submitted)
        self.dialog(wizard)

    def adopt(self):
        path = QFileDialog.getExistingDirectory(self, "Existing local/ratchet directory")
        if path:
            self.client.call(["adopt-ratchet", path], self.submitted)

    def global_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Global settings")
        dialog.resize(900, 650)
        QVBoxLayout(dialog).addWidget(Settings(self.client))
        self.dialog(dialog)

    def title_call(self, prefix, suffix=()):
        if self.title_id:
            self.client.call([*prefix, self.title_id, *suffix], lambda _: self.refresh())

    def build(self):
        if self.title_id:
            self.client.call(["build", self.title_id, "--reason", self.reason.text()], self.submitted)

    def run(self):
        if self.title_id:
            self.error_box.hide()
            self.client.call(["run", self.title_id, "--detach"], self.submitted)

    def stop_run(self):
        if self.title_id and QMessageBox.warning(self, "Stop game", "Save in-game first. Stopping during a save can corrupt it.\nStop now?",
                                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.title_call(["stop"])

    def activate(self):
        if record := self.builds.selected():
            self.title_call(["activate"], [record["id"]])

    def pin(self, enabled):
        if record := self.builds.selected():
            self.title_call(["pin" if enabled else "unpin"], [record["id"]])

    def prune(self):
        count, ok = QInputDialog.getInt(self, "Prune builds", "Keep newest builds (active and pinned builds are always kept):", 3, 0, 1000)
        if ok:
            self.title_call(["prune"], ["--keep", str(count)])

    def build_logs(self):
        if record := self.builds.selected():
            # Record contains its log directory, supplied by the CLI.
            path = record.get("logs")
            if path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def restore(self):
        if not self.title_id:
            return
        title = self.title_id
        def choose(result):
            entries = [r["id"] for r in result["backups"]]
            if not entries:
                self.show_error(dict(code="missing-backup", message="No backups yet."))
                return
            choice, ok = QInputDialog.getItem(self, "Restore backup", "A backup of current saves is made first.", entries, 0, False)
            if ok:
                self.client.call(["saves", "restore", title, choice], lambda _: self.refresh())
        self.client.call(["saves", "backups", title], choose)

    def export_save(self):
        record = self.saves.selected()
        if record and self.title_id:
            path, _ = QFileDialog.getSaveFileName(self, "Export save", record["id"]+".zip", "Save export (*.zip)")
            if path:
                self.title_call(["saves", "export"], [record["id"], path])

    def import_save(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import AnyPS3 save", "", "Save export (*.zip)")
        if path:
            self.title_call(["saves", "import"], [path])

    def run_log(self):
        if (record := self.runs.selected()) and self.title_id:
            def show(data):
                dialog = QDialog(self)
                dialog.setWindowTitle("Run evidence")
                dialog.resize(900, 650)
                view = QPlainTextEdit()
                view.setReadOnly(True)
                view.setPlainText(json.dumps(data["run"], indent=2)+"\n\n"+data["excerpt"])
                QVBoxLayout(dialog).addWidget(view)
                self.dialog(dialog)
            self.client.call(["runs", "show", self.title_id, record["id"]], show)

    def follow_selected(self):
        if record := self.jobs.selected():
            self.follow(record["id"])

    def cancel_job(self):
        if (record := self.jobs.selected()) and record.get("state") in {"queued", "running"}:
            self.client.call(["jobs", "cancel", record["id"]], lambda _: self.refresh_jobs())

    def job_log(self):
        if record := self.jobs.selected():
            self.client.call(["jobs", "show", record["id"]],
                lambda data: QDesktopServices.openUrl(QUrl.fromLocalFile(data["log"])))

    def closeEvent(self, event):
        self.closing = True
        self.preferences.setValue("geometry", self.saveGeometry())
        self.timer.stop()
        self.client.close()
        super().closeEvent(event)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library")
    parser.add_argument("--manager", help="Manager executable override (for integration tests)")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { font-size: 13px; }
        QMainWindow { background: #f4f5f7; }
        QLabel#heading { font-size: 23px; font-weight: 600; padding: 8px 0; }
        QLabel#title { font-size: 21px; font-weight: 600; }
        QPushButton { padding: 7px 12px; }
        QTabWidget::pane { background: white; border: 1px solid #cbd2da; }
        QPlainTextEdit#error { background: #fff1ec; color: #902f14; border: 1px solid #d65e3e; }
        QListWidget { background: white; border: 1px solid #cbd2da; }
        QListWidget::item { padding: 12px 6px; }
    """)
    command = [args.manager] if args.manager else [sys.executable, "-P", "-m", "anyps3_manager"]
    client = Client(command, args.library, app)
    window = Window(client)
    window.show()
    return app.exec()
