"""Validated startup, native platform preferences, and a single Qt event loop."""

import json
import logging
import os
import sys
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from buddy_desktop import __version__
from buddy_desktop.config import AppConfig, ConfigurationError, validate_api_url
from buddy_desktop.native_callback import NativeCallbackServer
from buddy_desktop.visuals import apply_theme
from buddy_desktop.window import MainWindow
from buddy_identity.config import IdentityError


def resolve_config(
    config: AppConfig, settings: QSettings, *, environment_override: bool | None = None
) -> AppConfig:
    """Environment takes precedence; invalid saved preferences remain recoverable."""
    if environment_override is None:
        environment_override = "BUDDY_API_URL" in os.environ
    if environment_override:
        return config
    value = settings.value("connection/api_url")
    if isinstance(value, str):
        try:
            return replace(config, api_url=validate_api_url(value))
        except ConfigurationError:
            logging.getLogger(__name__).warning("Invalid saved backend URL; using default")
    return config


def main() -> int:
    # Public deployment identifiers only. Explicit environment values always win.
    deployment_path = (
        Path(sys.__dict__["_MEIPASS"]) / "buddy-deployment.json"
        if getattr(sys, "frozen", False)
        else files("buddy_desktop").joinpath("deployment.json")
    )
    deployment = json.loads(deployment_path.read_text())
    # Identity values are consumed by AccountWidget, but the bundled API URL is a
    # default only. Treating it as an environment override would disable the user's
    # saved backend selection in Settings.
    added = [name for name in deployment if name != "BUDDY_API_URL" and name not in os.environ]
    environment_override = "BUDDY_API_URL" in os.environ
    try:
        for name in added:
            os.environ[name] = deployment[name]
        config = AppConfig.from_environment({**deployment, **os.environ})
        return run_application(config, environment_override=environment_override)
    except ConfigurationError as exc:
        print(f"Buddy configuration error: {exc}", file=sys.stderr)
        return 2
    finally:
        for name in added:
            os.environ.pop(name, None)


def run_application(
    config: AppConfig | None = None, *, environment_override: bool | None = None
) -> int:
    try:
        config = config or AppConfig.from_environment()
    except ConfigurationError as exc:
        print(f"Buddy configuration error: {exc}", file=sys.stderr)
        return 2
    if environment_override is None:
        environment_override = "BUDDY_API_URL" in os.environ
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    if QApplication.instance() is not None:
        print("Buddy already has a Qt application instance.", file=sys.stderr)
        return 1
    app = QApplication(sys.argv)
    app.setOrganizationName("Buddy")
    app.setApplicationName("BuddyDesktop")
    app.setApplicationVersion(__version__)
    activation = NativeCallbackServer(app)
    app.installEventFilter(activation)
    arguments = sys.argv[1:]
    try:
        if arguments and (len(arguments) != 2 or arguments[0] != "--auth-callback"):
            raise IdentityError("Unsupported launch arguments. Open Buddy normally.")
        if not activation.start(arguments[1] if arguments else None):
            return 0
    except IdentityError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    apply_theme(app)
    settings = QSettings()
    window = MainWindow(
        resolve_config(config, settings, environment_override=environment_override),
        settings,
        environment_override=environment_override,
    )
    window.show()

    def activate() -> None:
        if window.isMinimized():
            window.showNormal()
        window.raise_()
        window.activateWindow()

    activation.activated.connect(activate)
    try:
        return app.exec()
    finally:
        window.runner.wait_for_shutdown()
        window.account.runner.wait_for_shutdown()
        window.library.runner.wait_for_shutdown()
        window.present.executor.wait_for_shutdown()
        activation.close()
