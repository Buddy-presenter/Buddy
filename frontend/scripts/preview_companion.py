"""Launch the real desktop UI and companion offline with isolated, disposable settings.

Run from the repository root: .venv/Scripts/python.exe frontend/scripts/preview_companion.py
No account, upload, processing or saved-deck results are fabricated.
"""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from buddy_desktop.config import AppConfig
from buddy_desktop.visuals import apply_theme
from buddy_desktop.window import MainWindow


def main() -> int:
    for name in (
        "BUDDY_AWS_REGION",
        "BUDDY_COGNITO_POOL_ID",
        "BUDDY_COGNITO_CLIENT_ID",
        "BUDDY_COGNITO_DOMAIN",
    ):
        os.environ.pop(name, None)
    with TemporaryDirectory(prefix="buddy-companion-preview-") as directory:
        app = QApplication(sys.argv)
        apply_theme(app)
        settings = QSettings(str(Path(directory) / "preview.ini"), QSettings.Format.IniFormat)
        window = MainWindow(AppConfig("http://127.0.0.1:8000", 0.5), settings)
        window.setWindowTitle("Buddy — Interactive companion preview (offline)")
        window.resize(1120, 740)
        window.show()
        window.raise_()
        window.activateWindow()
        window.statusBar().showMessage(
            "Offline preview · Buddy wanders on his own. Move nearby, click him or drag him."
        )
        try:
            return app.exec()
        finally:
            window.runner.wait_for_shutdown()
            window.account.runner.wait_for_shutdown()
            window.library.runner.wait_for_shutdown()
            window.library.preview.runner.wait_for_shutdown()
            window.present.executor.wait_for_shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
