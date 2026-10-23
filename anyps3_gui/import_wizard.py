"""Import preflight and key selection; the manager owns every write."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                              QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget, QWizard, QWizardPage)

from .keys import KeyEntry, KEY_EXPLANATION


class ScanPage(QWizardPage):
    def __init__(self, client, iso, parent=None):
        super().__init__(parent)
        self.client, self.iso = client, iso
        self.ready = False
        self.setTitle("Disc identity and disk space")
        layout = QVBoxLayout(self)
        self.summary = QLabel("Inspecting disc…")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

    def initializePage(self):
        self.ready = False
        self.completeChanged.emit()
        self.client.call(["inspect-iso", self.iso.text()], self.scanned, error=self.failed)

    def scanned(self, data):
        self.ready = data["space_ok"]
        self.summary.setText(f"{data['title']}\n{data['id']} · {data['version']}\n\n"
            f"Required estimate: {data['estimated_required_bytes']/1024**3:.1f} GiB\n"
            f"Free: {data['free_bytes']/1024**3:.1f} GiB\n\n{data['compatibility']}\n"
            + ("" if self.ready else "\nInsufficient disk space."))
        self.wizard().scan = data
        self.completeChanged.emit()

    def failed(self, error):
        self.summary.setText(f"{error['code']}: {error['message']}")
        self.completeChanged.emit()

    def isComplete(self):
        return self.ready


class ImportWizard(QWizard):
    submitted = Signal(dict)

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self.scan = {}
        self.firmware_id = ""
        self.key_busy = False
        self.import_busy = False
        self.setWindowTitle("Import a PS3 disc")
        self.resize(700, 500)
        page = QWizardPage()
        page.setTitle("Choose a complete PS3 disc ISO")
        form = QFormLayout(page)
        self.iso = QLineEdit()
        page.registerField("iso*", self.iso)
        choose = QPushButton("Choose ISO…")
        choose.clicked.connect(lambda: self.choose(self.iso, "PS3 disc (*.iso)"))
        self.iso.setAccessibleName("PS3 disc ISO path")
        self.iso.textChanged.connect(self.iso.setToolTip)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.iso, 1)
        path_layout.addWidget(choose)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Disc ISO", path_row)
        self.addPage(page)
        self.addPage(ScanPage(client, self.iso))
        sources = QWizardPage()
        sources.setTitle("Firmware and keys")
        source_layout = QVBoxLayout(sources)
        # Outcome of the last key download/entry, outside the scroll area so it
        # is visible without scrolling: verified, format-only or failed.
        self.key_result = QLabel()
        self.key_result.setWordWrap(True)
        self.key_result.setVisible(False)
        source_layout.addWidget(self.key_result)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        source_layout.addWidget(scroll)
        form = QFormLayout(content)
        self.sources_form = form
        note = QLabel(KEY_EXPLANATION + "\nThe manager uses library firmware, then the disc's PUP; choose another PUP below if needed. "
                      "Select that PUP before setting or downloading keys.")
        note.setWordWrap(True)
        form.addRow(note)
        self.firmware_choice = QComboBox()
        form.addRow("Library firmware", self.firmware_choice)
        self.paths = {}
        self.path_buttons = {}
        for name, label in (("pup", "PUP key"), ("app", "Application key"), ("firmware", "Firmware PUP (optional)")):
            edit = QLineEdit()
            button = QPushButton("Choose…")
            button.clicked.connect(lambda checked=False, e=edit: self.choose(e, "All files (*)"))
            form.addRow(label, edit)
            form.addRow("", button)
            self.paths[name] = edit
            self.path_buttons[name] = button
        self.key_buttons = {}
        for kind in ("pup", "app"):
            button = QPushButton(f"{kind.upper()} JSON format / paste hex values…")
            button.clicked.connect(lambda checked=False, k=kind: self.enter_key(k))
            form.addRow(button)
            self.key_buttons[kind] = button
        self.fetch_button = QPushButton("Download from RPCS3…")
        self.fetch_button.clicked.connect(self.fetch_keys)
        form.addRow(self.fetch_button)
        self.key_status = QLabel("Choose JSON files above (validated on Finish), paste hex values, or explicitly download published keys.")
        self.key_status.setWordWrap(True)
        form.addRow(self.key_status)
        self.addPage(sources)
        confirm = QWizardPage()
        confirm.setTitle("Import and build")
        layout = QVBoxLayout(confirm)
        label = QLabel("Import extracts and verifies the original game, then creates a new native build.\n"
                       "This can take significant time and disk space. Closing the GUI leaves the job running.\n"
                       "Follow import and build progress in Jobs, then choose Play when the build is ready.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.no_build = QCheckBox("Import only; build later")
        layout.addWidget(self.no_build)
        self.replace = QCheckBox("Replace an existing disc for this title; preserve its saves")
        layout.addWidget(self.replace)
        self.import_status = QLabel()
        self.import_status.setWordWrap(True)
        layout.addWidget(self.import_status)
        self.addPage(confirm)
        self.currentIdChanged.connect(self.configure_sources)

    def configure_sources(self, page):
        if page != 2:
            return
        self.firmware_choice.clear()
        self.firmware_choice.addItem("Automatic: library, then disc", "")
        for name in self.scan.get("firmware", []):
            self.firmware_choice.addItem(name, name)
        configured = [kind.upper() for kind in ("app", "pup") if self.scan.get("keys", {}).get(kind, False)]
        if configured:
            self.key_status.setText("Configured: " + ", ".join(configured) + ". You can replace a key below. "
                                    "New JSON files are validated when you click Finish; paste/download validates now.")

    def choose(self, edit, filter_text):
        path, _ = QFileDialog.getOpenFileName(self, "Choose file", "", filter_text)
        if path:
            edit.setText(path)

    def target_args(self):
        args = ["--iso", self.iso.text()] if self.iso.text() else []
        if self.paths["firmware"].text():
            args += ["--pup", self.paths["firmware"].text()]
        return args

    def busy_keys(self, busy):
        self.key_busy = busy
        self.fetch_button.setEnabled(not busy)
        for button in self.key_buttons.values():
            button.setEnabled(not busy)
        for name in ("pup", "app", "firmware"):
            self.paths[name].setEnabled(not busy)
            self.path_buttons[name].setEnabled(not busy)
        for role in (QWizard.WizardButton.NextButton, QWizard.WizardButton.BackButton, QWizard.WizardButton.FinishButton):
            self.button(role).setEnabled(not busy)

    def show_key_result(self, state, headline, detail="", popup=False):
        colors = {"verified": "#1b5e20", "format": "#8a5a00", "failed": "#b71c1c"}
        marks = {"verified": "✔", "format": "⚠", "failed": "✖"}
        self.key_result.setText(f"{marks[state]} {headline}")
        self.key_result.setStyleSheet(f"QLabel {{ border: 2px solid {colors[state]}; color: {colors[state]};"
                                      " border-radius: 4px; padding: 6px; font-weight: bold; }")
        self.key_result.setVisible(True)
        if popup:
            # Non-blocking: the result stays on the page as well.
            box = QMessageBox(QMessageBox.Icon.Information if state == "verified" else
                              QMessageBox.Icon.Warning if state == "format" else QMessageBox.Icon.Critical,
                              "RPCS3 key download", headline, QMessageBox.StandardButton.Ok, self)
            box.setInformativeText(detail)
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            box.open()

    def keys_saved(self, result, downloaded=False):
        lines = []
        format_only = []
        for kind, record in result["keys"].items():
            self.scan.setdefault("keys", {})[kind] = True
            self.paths[kind].clear()
            lines.append(kind.upper() + ": " + record["validation"])
            if record["validation"].startswith("Format only"):
                format_only.append(kind.upper())
        detail = "\n".join(lines)
        self.key_status.setText(detail)
        kinds = " and ".join(kind.upper() for kind in result["keys"])
        action = "downloaded from RPCS3 and saved" if downloaded else "saved"
        if format_only:
            self.show_key_result("format", f"{kinds} key {action}, but {', '.join(format_only)} could only be format-checked "
                                 "(no matching PUP/EBOOT selected). Import validates it against the disc.", detail, downloaded)
        else:
            self.show_key_result("verified", f"{kinds} key {action} and verified against the selected files.", detail, downloaded)
        self.busy_keys(False)

    def enter_key(self, kind):
        if self.key_busy:
            return
        dialog = KeyEntry(kind, self)

        def save():
            self.busy_keys(True)
            dialog.saving = True
            dialog.buttons.setEnabled(False)
            dialog.status.setText("Validating…")

            def saved(result):
                dialog.saving = False
                self.keys_saved(result)
                dialog.accept()

            def failed(error):
                dialog.saving = False
                self.busy_keys(False)
                dialog.buttons.setEnabled(True)
                dialog.status.setText(error["code"] + ": " + error["message"])

            self.client.call(["keys", "set", kind, "--stdin", *self.target_args()], saved,
                             error=failed, input_data=dialog.payload())

        dialog.buttons.accepted.connect(save)
        dialog.exec()
        dialog.deleteLater()

    def fetch_keys(self):
        if self.key_busy:
            return
        answer = QMessageBox.question(self, "Download public PS3 keys from RPCS3?",
            "Fetch rpcs3/Crypto/key_vault.h and key_vault.cpp over HTTPS from "
            "https://raw.githubusercontent.com/RPCS3/rpcs3/ at pinned commit "
            "ffba2bf09ef3980638f9327b25240d81414034ab.\n\n"
            "The manager checks both SHA-256 hashes, extracts PUP and matching APP keys locally, "
            "validates them against available targets and saves them privately in your library. "
            "Without targets only format checks run (APP defaults to Ratchet revision 1).\n\n"
            "You are responsible for complying with local law. Download and replace configured keys?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.busy_keys(True)
        self.key_status.setText("Downloading pinned RPCS3 source and validating keys…")
        self.key_result.setVisible(False)

        def failed(error):
            self.key_status.setText(error["code"] + ": " + error["message"])
            self.show_key_result("failed", "Key download failed; no keys were changed.",
                                 error["code"] + ": " + error["message"], True)
            self.busy_keys(False)

        self.client.call(["keys", "fetch-rpcs3", *self.target_args()],
                         lambda result: self.keys_saved(result, downloaded=True), error=failed)

    def reject(self):
        # A running key download/validation would otherwise finish unseen and replace configured keys.
        if self.key_busy:
            self.key_status.setText("Key download/validation is still running; wait for its result before closing.")
            return
        super().reject()

    def closeEvent(self, event):
        if self.key_busy:
            self.key_status.setText("Key download/validation is still running; wait for its result before closing.")
            event.ignore()
            return
        super().closeEvent(event)

    def accept(self):
        if self.key_busy or self.import_busy:
            return
        self.import_busy = True
        for role in (QWizard.WizardButton.FinishButton, QWizard.WizardButton.BackButton):
            self.button(role).setEnabled(False)
        self.import_status.setText("Validating key selections and preparing import…")
        self.firmware_id = self.firmware_choice.currentData() or ""
        operations = []
        for name in ("pup", "app"):
            if self.paths[name].text():
                operations.append(["keys", "set", name, self.paths[name].text(), *self.target_args()])
        if self.paths["firmware"].text():
            operations.append(["firmware", "add", self.paths["firmware"].text()])

        def next_operation(_=None):
            if operations:
                self.client.call(operations.pop(0), saved, error=failed)
            else:
                args = ["import", self.iso.text()]
                if self.no_build.isChecked():
                    args.append("--no-build")
                if self.replace.isChecked():
                    args += ["--replace-disc", self.scan["id"]]
                if self.firmware_id:
                    args += ["--firmware", self.firmware_id]
                self.client.call(args, complete, error=failed)

        def saved(result):
            if result.get("job") and result.get("event") != "result":
                self.client.call(["jobs", "follow", result["job"]], saved, error=failed)
            else:
                if result.get("validation"):
                    self.import_status.setText(result["kind"].upper() + ": " + result["validation"])
                if result.get("firmware"):
                    self.firmware_id = result["firmware"]
                next_operation()

        def failed(error):
            self.import_busy = False
            self.button(QWizard.WizardButton.BackButton).setEnabled(True)
            self.import_status.setText(error["code"] + ": " + error["message"] + "\nUse Back to return to Firmware and keys, then retry.")
            self.button(QWizard.WizardButton.FinishButton).setEnabled(True)

        def complete(result):
            self.submitted.emit(result)
            super(ImportWizard, self).accept()

        next_operation()
