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

    def fill(self, records):
        selected = self.selected()
        self.records = records
        self.setRowCount(len(records))
        for row, record in enumerate(records):
            for col, (key, label) in enumerate(self.columns):
                value = record.get(key)
                self.setItem(row, col, QTableWidgetItem("" if value is None else str(value)))
            if selected and selected.get("id") == record.get("id"):
                self.selectRow(row)
        if self.currentRow() < 0 and records:
            self.selectRow(0)

    def selected(self):
        row = self.currentRow()
        return self.records[row] if 0 <= row < len(self.records) else None


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
                            ("rumble", "Rumble"), ("battery", "Battery")])
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        button("Refresh", self.refresh, actions)
        button("Live input", self.start_monitor, actions)
        button("Stop monitor", self.stop_monitor, actions)
        button("Rumble test", self.rumble, actions)
        self.live = QPlainTextEdit()
        self.live.setReadOnly(True)
        self.live.setMaximumBlockCount(100)
        layout.addWidget(self.live)
        self.config_note = QLabel("Port assignment, remapping, dead zones and in-game rumble require the separate pad-config runtime package.\n"
                                 "They are unavailable in the current runtime. Default SDL mapping remains active.")
        self.config_note.setWordWrap(True)
        layout.addWidget(self.config_note)
        self.refresh()

    def refresh(self):
        self.client.call(["pads", "list"], lambda result: self.table.fill(result["pads"]))

    def start_monitor(self):
        if self.monitor:
            return
        self.monitor = self.client.call(["pads", "monitor"], event=self.input)
        self.monitor.done.connect(lambda: setattr(self, "monitor", None))

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
        self.title_id = None
        self.followers = {}
        self.dialogs = []
        self.polling = False
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
        split.addWidget(detail)
        split.setStretchFactor(1, 1)
        self.jobs = Table([("id", "Job"), ("operation", "Operation"), ("state", "State"), ("stage", "Stage")])
        self.jobs.setMaximumHeight(150)
        layout.addWidget(QLabel("Jobs · continue running when this window closes"))
        layout.addWidget(self.jobs)
        actions = QHBoxLayout()
        button("Follow selected", self.follow_selected, actions)
        button("Cancel job", self.cancel_job, actions)
        button("Open job log", self.job_log, actions)
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
        self.client.call(["init"], lambda _: self.refresh())

    def add_table_tab(self, name, table, actions):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(table)
        row = QHBoxLayout()
        for label, action in actions:
            button(label, action, row)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(widget, name)

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
        self.client.call(["titles", "list"], self.fill_titles)
        self.refresh_jobs()

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
            self.play.setEnabled(False)
            self.rebuild.setEnabled(False)
            return
        title = item.data(Qt.ItemDataRole.UserRole)
        changed = self.title_id != title["id"]
        self.title_id = title["id"]
        self.title_label.setText(title["title"])
        self.status.setText(f"{title['id']} · {title.get('status')} · Active build: {title.get('active_build') or 'none'}\n"
            f"{title.get('compatibility', '')}\n"
            f"Last played: {title.get('last_played', 'never')} · Play time: {title.get('play_seconds', 0)/3600:.2f} h\n"
            + ("Rebuild recommended: "+", ".join(title["staleness_reasons"]) if title.get("stale") else ""))
        pixmap = QPixmap(title.get("background", ""))
        if pixmap.isNull():
            self.background.clear()
        else:
            self.background.setPixmap(pixmap.scaled(800, 160, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.play.setEnabled(bool(title.get("active_build")))
        self.rebuild.setEnabled(title["status"] != "incomplete")
        selected = self.title_id
        for command, table, key in ((["builds", selected], self.builds, "builds"),
                                    (["saves", "list", selected], self.saves, "saves"),
                                    (["runs", selected], self.runs, "runs")):
            self.client.call(command, lambda data, t=table, k=key, s=selected: t.fill(data[k]) if self.title_id == s else None)
        if changed or self.title_settings is None:
            if self.title_settings:
                self.tabs.removeTab(self.tabs.indexOf(self.title_settings))
                self.title_settings.deleteLater()
            self.title_settings = Settings(self.client, selected)
            self.tabs.addTab(self.title_settings, "Settings")

    def refresh_jobs(self):
        if self.polling:
            return
        self.polling = True
        call = self.client.call(["jobs", "list"], self.fill_jobs)
        call.done.connect(lambda: setattr(self, "polling", False))

    def fill_jobs(self, result):
        self.jobs.fill(result["jobs"])
        for job in result["jobs"]:
            if job["state"] in ("running", "queued") and job["id"] not in self.followers:
                self.follow(job["id"])

    def follow(self, job_id):
        if job_id in self.followers:
            return
        call = self.client.call(["jobs", "follow", job_id], self.job_finished, self.job_event)
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

    def job_finished(self, result):
        self.log.appendPlainText(json.dumps(result, ensure_ascii=False))
        self.refresh()

    def submitted(self, result):
        if result.get("job"):
            self.follow(result["job"])
        self.refresh_jobs()

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
        if record := self.jobs.selected():
            self.client.call(["jobs", "cancel", record["id"]], lambda _: self.refresh_jobs())

    def job_log(self):
        if record := self.jobs.selected():
            self.client.call(["jobs", "show", record["id"]],
                lambda data: QDesktopServices.openUrl(QUrl.fromLocalFile(data["log"])))

    def closeEvent(self, event):
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
