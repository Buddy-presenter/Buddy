"""Validated startup, native platform preferences, and a single Qt event loop."""

import logging
import os
import sys
from dataclasses import replace
from importlib.resources import files

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from buddy_desktop import __version__
from buddy_desktop.config import AppConfig, ConfigurationError, validate_api_url
from buddy_desktop.window import MainWindow


def resolve_config(config: AppConfig, settings: QSettings) -> AppConfig:
    """Environment takes precedence; invalid saved preferences remain recoverable."""
    if "BUDDY_API_URL" in os.environ:
        return config
    value = settings.value("connection/api_url")
    if isinstance(value, str):
        try:
            return replace(config, api_url=validate_api_url(value))
        except ConfigurationError:
            logging.getLogger(__name__).warning("Invalid saved backend URL; using default")
    return config


def main() -> int:
    try:
        config = AppConfig.from_environment()
    except ConfigurationError as exc:
        print(f"Buddy configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    if QApplication.instance() is not None:
        print("Buddy already has a Qt application instance.", file=sys.stderr)
        return 1
    app = QApplication(sys.argv)
    app.setOrganizationName("Buddy")
    app.setApplicationName("BuddyDesktop")
    app.setApplicationVersion(__version__)
    app.setStyleSheet(files("buddy_desktop").joinpath("theme.qss").read_text(encoding="utf-8"))
    settings = QSettings()
    window = MainWindow(
        resolve_config(config, settings),
        settings,
        environment_override="BUDDY_API_URL" in os.environ,
    )
    window.show()
    try:
        return app.exec()
    finally:
        window.runner.wait_for_shutdown()
        window.account.runner.wait_for_shutdown()
        window.library.runner.wait_for_shutdown()
        window.present.executor.wait_for_shutdown()
