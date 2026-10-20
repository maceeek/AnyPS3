"""Import preflight and key selection; the manager owns every write."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QLabel, QLineEdit,
                              QPushButton, QVBoxLayout, QWizard, QWizardPage)


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
        self.setWindowTitle("Import a PS3 disc")
        self.resize(700, 500)
        page = QWizardPage()
        page.setTitle("Choose a complete PS3 disc ISO")
        form = QFormLayout(page)
        self.iso = QLineEdit()
        page.registerField("iso*", self.iso)
        choose = QPushButton("Choose ISO…")
        choose.clicked.connect(lambda: self.choose(self.iso, "PS3 disc (*.iso)"))
        form.addRow(self.iso, choose)
        self.addPage(page)
        self.addPage(ScanPage(client, self.iso))
        sources = QWizardPage()
        sources.setTitle("Firmware and keys")
        form = QFormLayout(sources)
        self.sources_form = form
        note = QLabel("The manager uses library firmware, then the disc's firmware.\n"
                      "Choose a PUP below to add another version. Key files are referenced, never bundled.")
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
        self.addPage(sources)
        confirm = QWizardPage()
        confirm.setTitle("Import and build")
        layout = QVBoxLayout(confirm)
        label = QLabel("Import extracts and verifies the original game, then creates a new native build.\n"
                       "This can take significant time and disk space. Closing the GUI leaves the job running.\n"
                       "The development PC stays silent.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.no_build = QCheckBox("Import only; build later")
        layout.addWidget(self.no_build)
        self.replace = QCheckBox("Replace an existing disc for this title; preserve its saves")
        layout.addWidget(self.replace)
        self.addPage(confirm)
        self.currentIdChanged.connect(self.configure_sources)

    def configure_sources(self, page):
        if page != 2:
            return
        self.firmware_choice.clear()
        self.firmware_choice.addItem("Automatic: library, then disc", "")
        for name in self.scan.get("firmware", []):
            self.firmware_choice.addItem(name, name)
        for kind in ("app", "pup"):
            missing = not self.scan.get("keys", {}).get(kind, False)
            self.sources_form.setRowVisible(self.paths[kind], missing)
            self.sources_form.setRowVisible(self.path_buttons[kind], missing)

    def choose(self, edit, filter_text):
        path, _ = QFileDialog.getOpenFileName(self, "Choose file", "", filter_text)
        if path:
            edit.setText(path)

    def accept(self):
        self.button(QWizard.WizardButton.FinishButton).setEnabled(False)
        self.firmware_id = self.firmware_choice.currentData() or ""
        operations = []
        for name in ("pup", "app"):
            if self.paths[name].text():
                operations.append(["keys", "set", name, self.paths[name].text()])
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
                if result.get("firmware"):
                    self.firmware_id = result["firmware"]
                next_operation()

        def failed(error):
            self.client.error.emit(error)
            self.button(QWizard.WizardButton.FinishButton).setEnabled(True)

        def complete(result):
            self.submitted.emit(result)
            super(ImportWizard, self).accept()

        next_operation()
