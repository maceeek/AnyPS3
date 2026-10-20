"""Exercise the real CLI against synthetic data and capture the title screen."""

import os
from pathlib import Path
import shutil
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
from anyps3_gui.app import Window
from anyps3_gui.client import Client
from tests.manager.test_manager import ManagerTests

fixture = ManagerTests()
fixture.setUp()
app = QApplication([])
app.setStyle("Fusion")
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(fixture.root/"qt"))
try:
    fixture.import_title()
    fixture.pipeline().build("BCES00511")
    shutil.rmtree(fixture.lib.root/"jobs")
    (fixture.lib.root/"jobs").mkdir()
    client = Client([sys.executable, "-P", "-m", "anyps3_manager"], str(fixture.lib.root), app)
    errors = []
    client.error.connect(errors.append)
    window = Window(client)
    window.show()

    def capture():
        if client.calls:
            QTimer.singleShot(50, capture)
            return
        assert not errors, errors
        assert window.title_id == "BCES00511"
        assert window.builds.rowCount() == 1
        assert window.title_settings.editors
        window.grab().save(sys.argv[1])
        window.close()

    QTimer.singleShot(100, capture)
    QTimer.singleShot(10000, app.quit)
    app.exec()
    if not Path(sys.argv[1]).is_file():
        raise SystemExit("Preview did not complete.")
finally:
    fixture.tearDown()
