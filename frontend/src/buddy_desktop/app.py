"""Validated startup, native platform preferences, and a single Qt event loop."""

import json
import logging
import os
import sys
from contextlib import suppress
from dataclasses import replace
from importlib.resources import files
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from buddy_desktop import __version__
from buddy_desktop.config import AppConfig, ConfigurationError, validate_api_url
from buddy_desktop.native_callback import NativeCallbackServer
from buddy_desktop.visuals import app_icon, apply_theme
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
    identity_override = any(
        name in os.environ
        for name in (
            "BUDDY_AWS_REGION",
            "BUDDY_COGNITO_POOL_ID",
            "BUDDY_COGNITO_CLIENT_ID",
            "BUDDY_COGNITO_DOMAIN",
        )
    )
    # An operator's identity provider must not inherit the preview's browser return.
    # Without an explicit callback, IdentityConfig retains its native URI default.
    added = [
        name
        for name in deployment
        if name != "BUDDY_API_URL"
        and name not in os.environ
        and not (identity_override and name == "BUDDY_COGNITO_CALLBACK_URL")
    ]
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
    _configure_logging()
    if QApplication.instance() is not None:
        print("Buddy already has a Qt application instance.", file=sys.stderr)
        return 1
    if sys.platform == "win32":
        # Group the taskbar button under Buddy's own icon, not the Python interpreter's,
        # when running from source. Packaged builds carry the same icon in Buddy.exe.
        import ctypes

        with suppress(AttributeError, OSError):
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("org.buddy.desktop")
    app = QApplication(sys.argv)
    app.setOrganizationName("Buddy")
    app.setApplicationName("BuddyDesktop")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
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
        window.review.runner.wait_for_shutdown()
        window.review.cleanup_runner.wait_for_shutdown()
        window.present.executor.wait_for_shutdown()
        window.present.powerpoint_runner.wait_for_shutdown()
        window.present.agent.wait_for_shutdown()
        window.present.policy_runner.wait_for_shutdown()
        window.present.generated_runner.wait_for_shutdown()
        activation.close()


def _configure_logging() -> None:
    """Keep bounded, privacy-safe desktop diagnostics available in packaged builds."""
    if logging.getLogger().handlers:
        return
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    state_root = (
        Path(os.environ["LOCALAPPDATA"])
        if os.name == "nt" and os.environ.get("LOCALAPPDATA")
        else Path(os.environ["XDG_STATE_HOME"])
        if os.environ.get("XDG_STATE_HOME")
        else Path.home() / ".local" / "state"
    )
    try:
        log_dir = state_root / "Buddy" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_dir / "desktop.log",
                maxBytes=1_000_000,
                backupCount=2,
                encoding="utf-8",
            )
        )
    except OSError:
        # Logging must never prevent the presenter from opening Buddy.
        pass
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )
    # Keep performance events available in packaged desktop logs without turning on
    # INFO output from unrelated application components.
    for name in (
        "buddy_desktop.agent_loop",
        "buddy_desktop.preflight_service",
        "buddy_desktop.present_widget",
        "buddy_desktop.review_widget",
        "buddy_desktop.slide_executor",
    ):
        logging.getLogger(name).setLevel(logging.INFO)
