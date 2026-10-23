"""Schema-rendered settings, with explicit inheritance and capability display."""

import json

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget)


class Settings(QWidget):
    def __init__(self, client, title=None, parent=None):
        super().__init__(parent)
        self.client, self.title = client, title
        self.schema, self.values, self.overrides, self.editors = None, None, {}, {}
        layout = QVBoxLayout(self)
        self.advanced = QCheckBox("Show advanced, diagnostic and unavailable settings")
        self.advanced.toggled.connect(self.render_fields)
        layout.addWidget(self.advanced)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.load()

    def args(self):
        return ["--title", self.title] if self.title else ["--global"]

    def load(self, _result=None):
        self.client.call(["settings", "schema", *self.args()], self.got_schema)
        self.client.call(["settings", "get", *self.args()], self.got_values)
        self.client.call(["doctor"], self.got_host)

    def got_host(self, data):
        self.ram = data["checks"]["ram_bytes"]
        self.memory_warning()

    def got_schema(self, data):
        self.schema = data["settings"]
        self.render_fields()
        self.memory_warning()

    def got_values(self, data):
        self.values, self.overrides = data["settings"], data.get("overrides", {})
        self.render_fields()
        self.memory_warning()

    def memory_warning(self):
        if not self.values or not hasattr(self, "ram"):
            return
        def size(value):
            return int(value[:-1])*1024**(3 if value[-1] == "G" else 2)
        # Keys are discovered through schema metadata; memory field names need
        # no special handling by the generic editor.
        limits = [size(self.values[k]) for k, spec in (self.schema or {}).items() if spec.get("concurrent_memory")]
        warning = " Combined memory limits exceed host RAM." if sum(limits) > self.ram else ""
        self.warning.setText(f"Host RAM: {self.ram/1024**3:.1f} GiB.{warning}")

    def render_fields(self):
        if self.schema is None or self.values is None:
            return
        current_group = self.tabs.tabText(self.tabs.currentIndex())
        self.editors.clear()
        while self.tabs.count():
            widget = self.tabs.widget(0)
            self.tabs.removeTab(0)
            widget.deleteLater()
        forms = {}
        for key, spec in self.schema.items():
            # Settings this AnyPS3 version cannot honour at all stay out of the
            # way unless advanced settings are shown (then they are disabled).
            if (spec.get("advanced") or not spec.get("supported", True)) and not self.advanced.isChecked():
                continue
            group = spec["group"]
            if group not in forms:
                content = QWidget()
                form = QFormLayout(content)
                form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                area = QScrollArea()
                area.setWidgetResizable(True)
                area.setWidget(content)
                self.tabs.addTab(area, group)
                forms[group] = form
            value = self.values[key]
            if spec["type"] == "boolean":
                editor = QCheckBox()
                editor.setChecked(value)
                getter = editor.isChecked
            elif spec.get("choices"):
                editor = QComboBox()
                labels = spec.get("choice_labels", {})
                for choice in spec["choices"]:
                    editor.addItem(labels.get(str(choice), str(choice)), choice)
                editor.setCurrentIndex(editor.findData(value))
                getter = editor.currentData
            else:
                editor = QLineEdit(str(value))
                getter = (lambda e=editor: int(e.text())) if spec["type"] == "integer" else editor.text
            self.editors[key] = editor
            editable = spec.get("editable", True) and spec.get("available", True) and not (self.title and spec.get("global_only"))
            editor.setEnabled(editable)
            row = QWidget()
            horizontal = QHBoxLayout(row)
            horizontal.setContentsMargins(0, 0, 0, 0)
            horizontal.addWidget(editor, 1)
            save = QPushButton("Apply")
            save.setEnabled(editable)
            save.clicked.connect(lambda checked=False, k=key, g=getter: self.save(k, g))
            reset = QPushButton("Inherit" if self.title else "Reset")
            reset.setEnabled(editable and key in self.overrides)
            reset.clicked.connect(lambda checked=False, k=key: self.client.call(["settings", "reset", *self.args(), k], self.load))
            horizontal.addWidget(save)
            horizontal.addWidget(reset)
            description = spec["description"]
            if not spec.get("available", True):
                description += ("\nUnavailable in the active build; rebuild the title to enable it." if spec.get("supported", True)
                                else "\nUnavailable in the active build and not implemented by this AnyPS3 version.")
            if self.title and spec.get("global_only"):
                description += "\nChange this setting in global Settings."
            if not spec.get("editable", True):
                description += "\nRead-only in the GUI."
            label = QLabel(spec.get("label", key)+(" • override" if key in self.overrides else ""))
            label.setToolTip(description)
            row.setToolTip(description)
            forms[group].addRow(label, row)
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == current_group:
                self.tabs.setCurrentIndex(index)
                break

    def save(self, key, getter):
        try:
            value = getter()
        except ValueError:
            self.client.error.emit(dict(code="invalid-setting", message="Enter a whole number.", detail={}))
            return
        self.client.call(["settings", "set", *self.args(), key, json.dumps(value)], self.load)
