"""Opt-in Windows staging acceptance with the real shell, vault, API and OS callback.

The installed preview owns the URL protocol. This source-run shell receives the relayed
callback. Only public authorization URLs and sanitized outcome labels go to stdout;
passwords/codes must be entered in Cognito's browser pages. Never run this in CI.
"""

import json
import os
import sys
from functools import partial
from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication

from buddy_desktop import auth
from buddy_desktop.config import AppConfig
from buddy_desktop.native_callback import NativeCallbackServer
from buddy_desktop.window import MainWindow


def main() -> int:
    if sys.platform != "win32":
        raise ValueError("Use the installed macOS application for native URL acceptance.")
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "infra/staging/public-config.json").read_text())
    os.environ.update(config)
    original = auth.browser_code

    def open_browser(url: str) -> bool:
        # Public client ID, PKCE challenge and random state only; never tokens/verifier/code.
        print("BROWSER_URL " + url, flush=True)
        return True

    auth.browser_code = partial(original, opener=open_browser)
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("Buddy")
    app.setApplicationName("BuddyDesktop")
    server = NativeCallbackServer(app)
    app.installEventFilter(server)
    if not server.start():
        print("Use the existing Buddy window; another instance is active.", flush=True)
        return 1
    app.setStyleSheet(files("buddy_desktop").joinpath("theme.qss").read_text())
    window = MainWindow(AppConfig.from_environment(), QSettings(), environment_override=True)
    window.account.runner.succeeded.connect(lambda *_: print("ACCOUNT_FLOW_SUCCEEDED", flush=True))
    window.account.runner.failed.connect(lambda *_: print("ACCOUNT_FLOW_FAILED", flush=True))
    window.show()
    server.activated.connect(window.activateWindow)
    QTimer.singleShot(300, lambda: window.account.start("signup"))
    try:
        return app.exec()
    finally:
        window.runner.wait_for_shutdown()
        window.account.runner.wait_for_shutdown()
        window.library.runner.wait_for_shutdown()
        window.present.executor.wait_for_shutdown()
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
