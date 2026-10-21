"""Key explanation and hex entry; all validation and persistence belong to the CLI."""

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel,
                              QLineEdit, QSpinBox, QVBoxLayout)

KEY_EXPLANATION = (
    "PUP key: decrypts the firmware update PS3UPDAT.PUP. "
    "APP key: decrypts the game's EBOOT.BIN SELF. Both are PS3 system keys, "
    "the same for every user, not per-game; APP must match the SELF revision (1 for Ratchet).\n"
    "Sources: your own console, or published sources such as RPCS3's "
    "rpcs3/Crypto/key_vault.{h,cpp}. Keys are not included with AnyPS3.\n"
    "Validation uses the selected PUP and EBOOT inside the ISO. Without a target, "
    "only JSON format and hex lengths can be checked. Results below state which checks ran."
)


class KeyEntry(QDialog):
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.saving = False
        self.setWindowTitle(f"{kind.upper()} key format and hex entry")
        self.resize(680, 450)
        layout = QVBoxLayout(self)
        note = QLabel(KEY_EXPLANATION)
        note.setWordWrap(True)
        layout.addWidget(note)
        fields = '"erk":"<64 hex digits>", "riv":"<32 hex digits>"'
        if kind == "pup":
            fields += ', "pup_hmac":"<128 hex digits>"'
        else:
            fields = '"type":4, "revision":1, ' + fields
        example = QLabel("JSON file format:\n{" + fields + "}")
        example.setWordWrap(True)
        example.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(example)
        form = QFormLayout()
        self.values = {}
        lengths = [("erk", 64), ("riv", 32)] + ([("pup_hmac", 128)] if kind == "pup" else [])
        for name, size in lengths:
            edit = QLineEdit()
            edit.setPlaceholderText(f"Paste exactly {size} hex digits, without 0x or spaces")
            edit.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
            self.values[name] = edit
            form.addRow(name, edit)
        self.revision = QSpinBox()
        self.revision.setRange(0, 32767)
        self.revision.setValue(1)
        if kind == "app":
            form.addRow("type", QLabel("4 (APP)"))
            form.addRow("revision (must match SELF)", self.revision)
        layout.addLayout(form)
        note = QLabel("Save validates the values and writes JSON to the library's private key store (0600). "
                      "Key values are sent privately to the manager over stdin.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def reject(self):
        if not self.saving:
            super().reject()

    def closeEvent(self, event):
        if self.saving:
            event.ignore()
        else:
            super().closeEvent(event)

    def payload(self):
        spec = {name: edit.text().strip() for name, edit in self.values.items()}
        if self.kind == "app":
            spec.update(type=4, revision=self.revision.value())
        return json.dumps(spec).encode("utf-8")
