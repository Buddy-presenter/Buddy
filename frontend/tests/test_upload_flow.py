"""Real HTTP transfer and Qt cancellation/recovery at the remote-service boundary."""

import hashlib
import json
import socket
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from uuid import uuid4

import pytest
from frontend.tests.test_library import signed_in
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryAccessExpired, LibraryPage, LibraryService
from buddy_desktop.presentation_service import (
    MAX_BYTES,
    PresentationService,
    UploadCancelled,
    UploadRejected,
)
from buddy_desktop.services import ServiceError
from buddy_desktop.visuals import apply_theme


@pytest.fixture
def upload_http():
    class Handler(BaseHTTPRequestHandler):
        def reply(self, data):
            self.send_response(server.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "1")
            self.end_headers()
            with suppress(OSError):
                self.wfile.write(json.dumps({"data": data}).encode())

        def do_POST(self):
            server.keys.append(self.headers["Idempotency-Key"])
            server.metadata = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            server.reserve_entered.set()
            server.reserve_release.wait(5)
            self.reply(server.record)

        def do_GET(self):
            server.gets += 1
            server.record["status"] = "complete"
            self.reply(server.record)

        def do_PUT(self):
            server.bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            server.entered.set()
            server.release.wait(5)
            server.record["status"] = "complete"
            if server.drop:
                with suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.reply(server.record)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.code, server.drop, server.gets = 200, False, 0
    server.keys, server.bodies = [], []
    server.record = {"id": str(uuid4()), "status": "pending", "deck": {"id": str(uuid4())}}
    server.entered, server.release = Event(), Event()
    server.reserve_entered, server.reserve_release = Event(), Event()
    server.release.set()
    server.reserve_release.set()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.reserve_release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def client(server):
    return PresentationService(AppConfig(f"http://127.0.0.1:{server.server_port}", 2), "test")


def source(tmp_path):
    path = tmp_path / "original.pdf"
    path.write_bytes(b"%PDF-" + b"original content\n" * 20000)
    return path


def test_transfer_progress_original_bytes_and_lost_response_recovery(upload_http, tmp_path):
    service, key, path = client(upload_http), uuid4(), source(tmp_path)
    progress = []
    service.progress = lambda *args: progress.append(args)
    upload_http.drop = True
    with pytest.raises(ServiceError, match="same file"):
        service.upload(path, key)
    assert upload_http.bodies == [path.read_bytes()]
    assert upload_http.metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    sent = [p[1] for p in progress if p[0] == "Uploading"]
    assert sent[0] == 0 and sent[-1] == path.stat().st_size
    assert sent == sorted(sent) and len(sent) > 2
    assert progress[-1] == ("Validating and saving", 0, 0)
    saved = service.upload(path, key)
    assert saved.deck_id == upload_http.record["deck"]["id"]
    assert upload_http.keys == [str(key), str(key)]
    assert len(upload_http.bodies) == 1


@pytest.mark.parametrize("status", ["receiving", "cleanup"])
def test_uncertain_transfer_polls_instead_of_resending(upload_http, tmp_path, status):
    upload_http.record["status"] = status
    service = client(upload_http)
    saved = service.upload(source(tmp_path), uuid4())
    assert saved.deck_id == upload_http.record["deck"]["id"]
    assert upload_http.gets == 1 and not upload_http.bodies


# One status per outcome: expired access, rejected file, transient failure, no redirects.
@pytest.mark.parametrize("code", [401, 413, 503, 302])
def test_upload_safe_actionable_errors(upload_http, tmp_path, code):
    upload_http.code = code
    expected = (
        LibraryAccessExpired
        if code in {401, 403}
        else UploadRejected
        if code in {413, 415, 422}
        else ServiceError
    )
    with pytest.raises(expected):
        client(upload_http).upload(source(tmp_path), uuid4())
    assert not upload_http.bodies


@pytest.mark.parametrize("kind", ["unsupported", "empty", "oversized", "missing"])
def test_invalid_local_file_never_contacts_server(upload_http, tmp_path, kind):
    path = tmp_path / ("deck.txt" if kind == "unsupported" else "deck.pdf")
    if kind != "missing":
        with path.open("wb") as stream:
            if kind == "oversized":
                stream.truncate(MAX_BYTES + 1)
    with pytest.raises(ServiceError):
        client(upload_http).upload(path, uuid4())
    assert not upload_http.keys


def test_cancel_between_chunks_stops_sending(upload_http, tmp_path):
    service = client(upload_http)

    def progress(phase, sent, total):
        if phase == "Uploading" and sent:
            service.cancel()

    service.progress = progress
    with pytest.raises(UploadCancelled):
        service.upload(source(tmp_path), uuid4())


def test_navigation_does_not_hide_upload_session_expiry(
    upload_http, desktop_window, monkeypatch, qtbot, tmp_path
):
    monkeypatch.setattr(LibraryService, "list", lambda *a, **kw: LibraryPage((), 0, 1, 20))
    window = desktop_window(f"http://127.0.0.1:{upload_http.server_port}", request_timeout=5)
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    upload_http.code = 401
    upload_http.reserve_release.clear()
    library.start_upload(source(tmp_path))
    qtbot.waitUntil(upload_http.reserve_entered.is_set)
    window.navigation.setCurrentRow(0)
    upload_http.reserve_release.set()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert window.account.session.tokens is None
    assert not library.upload_panel.isVisible()
    assert not library.upload_keys


def test_upload_native_layout_and_keyboard(
    upload_http, desktop_window, monkeypatch, qtbot, tmp_path, qapp
):
    old_style, old_palette = qapp.styleSheet(), qapp.palette()
    apply_theme(qapp)
    monkeypatch.setattr(LibraryService, "list", lambda *a, **kw: LibraryPage((), 0, 1, 20))
    try:
        window = desktop_window(f"http://127.0.0.1:{upload_http.server_port}")
        window.resize(800, 600)  # The minimum window size is the binding layout constraint.
        library = signed_in(window)
        qtbot.waitUntil(lambda: not library.runner.busy)
        upload_http.release.clear()
        library.start_upload(source(tmp_path))
        qtbot.waitUntil(upload_http.entered.is_set)
        qtbot.waitUntil(lambda: "Validating" in library.upload_status.text())
        scroll = window.pages.currentWidget()
        scroll.ensureWidgetVisible(library.cancel_upload_button)
        assert scroll.horizontalScrollBar().maximum() == 0
        assert library.upload_panel.width() <= scroll.viewport().width()
        library.cancel_upload_button.setFocus()
        from PySide6.QtCore import Qt

        qtbot.keyClick(library.cancel_upload_button, Qt.Key.Key_Space)
        qtbot.waitUntil(lambda: not library.runner.busy)
        assert library.retry_upload_button.isVisible()
    finally:
        upload_http.release.set()
        qapp.setStyleSheet(old_style)
        qapp.setPalette(old_palette)


@pytest.mark.parametrize("action", ["cancel", "signout", "shutdown", "backend"])
def test_qt_cancel_waiting_response_and_private_invalidation(
    upload_http, desktop_window, monkeypatch, qtbot, tmp_path, action
):
    monkeypatch.setattr(LibraryService, "list", lambda *a, **kw: LibraryPage((), 0, 1, 20))
    path = source(tmp_path)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(path), ""))
    window = desktop_window(f"http://127.0.0.1:{upload_http.server_port}")
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    upload_http.release.clear()
    library.choose_upload()
    qtbot.waitUntil(upload_http.entered.is_set)
    ticks = []
    timer = QTimer(window)
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    qtbot.waitUntil(lambda: len(ticks) >= 3)
    assert library.upload_panel.isVisible()
    assert "Validating" in library.upload_status.text()
    if action == "cancel":
        library.cancel_upload_button.click()
    elif action == "signout":
        window.account.session = None
        window.account.session_changed.emit()
    elif action == "backend":
        library.configure(AppConfig("http://127.0.0.1:9999", 1))
    else:
        window.close()
    # Cancellation interrupts a blocked socket, without waiting for the server.
    qtbot.waitUntil(lambda: not library.runner.busy, timeout=2000)
    assert not upload_http.release.is_set()
    if action == "cancel":
        assert library.retry_upload_button.isVisible()
        assert len(library.upload_keys) == 1
        upload_http.record["status"] = "complete"
        library.retry_upload_button.click()
        qtbot.waitUntil(lambda: not library.runner.busy)
        assert not library.upload_keys
        assert len(upload_http.bodies) == 1
    else:
        assert not library.upload_panel.isVisible()
        assert not library.upload_keys and library.retry_path is None
    upload_http.release.set()
