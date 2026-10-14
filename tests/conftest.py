import gc
from collections.abc import Iterator
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest


@pytest.fixture
def desktop_window(qtbot, tmp_path, monkeypatch):
    """Isolated settings, no native vault/device access, and bounded Qt teardown."""
    from PySide6.QtCore import QSettings

    from buddy_desktop.config import AppConfig
    from buddy_desktop.window import MainWindow

    for name in (
        "BUDDY_AWS_REGION",
        "BUDDY_COGNITO_POOL_ID",
        "BUDDY_COGNITO_CLIENT_ID",
        "BUDDY_COGNITO_DOMAIN",
        "BUDDY_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    windows = []

    def make(api_url="http://127.0.0.1:8000", *, request_timeout=0.5, **kwargs):
        # Reclaim prior Qt fixture cycles on the GUI thread, before HTTP workers can
        # trigger Python's cyclic collector (notably during Windows TLS store loading).
        gc.collect()
        settings = QSettings(
            str(tmp_path / f"buddy-{len(windows)}.ini"), QSettings.Format.IniFormat
        )
        window = MainWindow(AppConfig(api_url, request_timeout), settings, **kwargs)
        qtbot.addWidget(window)
        windows.append(window)
        window.show()
        return window

    yield make
    for window in reversed(windows):
        window.close()
        qtbot.waitUntil(
            lambda window=window: (
                not window.runner.busy
                and not window.account.runner.busy
                and not window.library.runner.busy
            ),
            timeout=5000,
        )
        qtbot.waitUntil(lambda window=window: not window.isVisible(), timeout=5000)
        window.settings.sync()


@pytest.fixture
def signal_recorder(qtbot):
    """Record queued signal arguments while pytest-qt pumps the real event loop."""
    from PySide6.QtTest import QSignalSpy

    def make(signal):
        spy = QSignalSpy(signal)
        if not spy.isValid():
            raise ValueError("Expected a valid Qt signal.")

        def wait(count=1, timeout=5000):
            qtbot.waitUntil(lambda: spy.count() >= count, timeout=timeout)
            return [spy.at(index) for index in range(spy.count())]

        return wait

    return make


@pytest.fixture
def backend() -> Iterator[ThreadingHTTPServer]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.server.entered.set()
            self.server.release.wait(3)
            self.send_response(self.server.response_code)
            self.send_header("Content-Type", self.server.content_type)
            self.end_headers()
            # Timeout tests deliberately close the client before a response.
            with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.wfile.write(self.server.body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.response_code = 200
    server.content_type = "application/json"
    server.body = b'{"status":"ok"}'
    server.entered = Event()
    server.release = Event()
    server.release.set()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
