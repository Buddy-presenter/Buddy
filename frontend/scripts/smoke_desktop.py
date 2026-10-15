"""Launch the real shell in an isolated settings profile and optionally capture it."""

import argparse
import sys
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication

from buddy_desktop.config import AppConfig
from buddy_desktop.window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(files("buddy_desktop").joinpath("theme.qss").read_text(encoding="utf-8"))
    with TemporaryDirectory() as directory:
        settings = QSettings(str(Path(directory) / "buddy.ini"), QSettings.Format.IniFormat)
        window = MainWindow(AppConfig.from_environment(), settings)
        window.show()
        failures = []

        def verify() -> None:
            if not window.isVisible() or window.pages.currentIndex() != 0:
                failures.append("Shell did not open on Home.")
            if args.screenshot:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                if not window.grab().save(str(args.screenshot)):
                    failures.append("Cannot save screenshot.")
            window.close()

        QTimer.singleShot(300, verify)
        code = app.exec()
        window.runner.wait_for_shutdown()
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1 if failures else code


if __name__ == "__main__":
    raise SystemExit(main())
