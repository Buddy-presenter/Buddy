import json
import time
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt, QTimer

from buddy_desktop.auth import Tokens
from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import (
    DeckSummary,
    LibraryAccessExpired,
    LibraryPage,
    LibraryService,
)
from buddy_desktop.services import ServiceError


def payload(title="Saved deck", status="ready", *, total=1, page=1):
    return {
        "data": {
            "items": [
                {
                    "id": str(uuid4()),
                    "title": title,
                    "created_at": "2026-01-01T12:00:00Z",
                    "status": status,
                    "attempts": 2,
                }
            ],
            "total": total,
            "page": page,
            "page_size": 20,
        },
        "request_id": "test",
    }


def service(backend):
    return LibraryService(AppConfig(f"http://127.0.0.1:{backend.server_port}", 0.5))


def test_real_http_library_and_typed_payload(backend):
    backend.body = json.dumps(payload()).encode()
    page = service(backend).list("test-token", search="", sort="newest", page=1)
    assert page.items[0].title == "Saved deck"
    assert page.items[0].created_at.tzinfo is not None


@pytest.mark.parametrize("code", [401, 403, 429, 500, 503, 302])
def test_safe_http_failures_and_access_expiry(backend, code):
    backend.response_code = code
    backend.body = b"private token and diagnostic"
    expected = LibraryAccessExpired if code in {401, 403} else ServiceError
    with pytest.raises(expected) as error:
        service(backend).list("test", search="", sort="newest", page=1)
    assert "private" not in str(error.value) and "token" not in str(error.value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["data"].update(total=True),
        lambda p: p["data"].update(total=2),
        lambda p: p["data"].update(page=2),
        lambda p: p["data"].update(page_size=999),
        lambda p: p["data"]["items"][0].update(status="invented"),
        lambda p: p["data"]["items"][0].update(id="invalid"),
        lambda p: p["data"]["items"][0].update(title=""),
        lambda p: p["data"]["items"][0].update(attempts=-1),
        lambda p: p["data"]["items"][0].update(created_at="2026-01-01"),
    ],
)
def test_invalid_contract_rejected(backend, mutation):
    value = payload()
    mutation(value)
    backend.body = json.dumps(value).encode()
    with pytest.raises(ServiceError, match="invalid library response"):
        service(backend).list("test", search="", sort="newest", page=1)


@pytest.mark.parametrize(
    "body,content_type",
    [(b"{}", "text/html"), (b"x" * 65537, "application/json"), (b"not-json", "application/json")],
    ids=["wrong-content-type", "oversized", "invalid-json"],
)
def test_response_size_content_type_and_json(backend, body, content_type):
    backend.body = body
    backend.content_type = content_type
    with pytest.raises(ServiceError, match="invalid library response"):
        service(backend).list("test", search="", sort="newest", page=1)


def signed_in(window):
    window.account.session = SimpleNamespace(
        tokens=Tokens("test-token", "refresh", time.monotonic() + 3600),
        cancelled=Event(),
    )
    window.account.session_changed.emit()
    window.navigation.setCurrentRow(1)
    return window.library


def test_signed_out_never_lists_decks(desktop_window, backend):
    window = desktop_window(f"http://127.0.0.1:{backend.server_port}")
    window.navigation.setCurrentRow(1)
    assert not backend.entered.is_set()
    assert "Sign in" in window.library.state.title.text()
    assert window.library.table.rowCount() == 0
    assert not window.library.refresh.isEnabled()


def test_real_library_loading_responsive_refresh_and_keyboard(desktop_window, backend, qtbot):
    backend.body = json.dumps(payload("<b>Literal deck</b>", "processing")).encode()
    backend.release.clear()
    window = desktop_window(f"http://127.0.0.1:{backend.server_port}", request_timeout=5)
    ticks = []
    timer = QTimer(window)
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    library = signed_in(window)
    qtbot.waitUntil(backend.entered.is_set)
    qtbot.waitUntil(lambda: len(ticks) > 2)
    assert library.runner.busy and "Loading" in library.state.title.text()
    assert not window.preferences.save.isEnabled()
    backend.release.set()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.table.item(0, 0).text() == "<b>Literal deck</b>"
    assert library.table.item(0, 1).text() == "Processing"
    assert "running" in library.detail.toPlainText()
    assert library.poll.isActive()
    assert not library.state.isVisible()
    backend.body = json.dumps(payload("Updated deck", "failed")).encode()
    qtbot.keyClick(library.refresh, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.table.item(0, 0).text() == "Updated deck"
    assert "Contact support" in library.detail.toPlainText()
    assert not library.poll.isActive()
    window.resize(800, 600)
    library.search.setFocus()
    qtbot.keyClick(library.search, Qt.Key.Key_Tab)
    assert library.sort.hasFocus()


def test_failure_retry_empty_and_access_expiry(desktop_window, backend, qtbot):
    backend.response_code = 503
    window = desktop_window(f"http://127.0.0.1:{backend.server_port}")
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.state.retry.isVisible()
    backend.response_code = 200
    backend.body = json.dumps(
        {"data": {"items": [], "total": 0, "page": 1, "page_size": 20}}
    ).encode()
    library.reload()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.state.title.text() == "No saved decks"
    library.search.setText("missing")
    library.debounce.stop()
    library.reload()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.state.title.text() == "No matching decks"
    backend.response_code = 401
    library.reload()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert window.account.session.tokens is None
    assert library.table.rowCount() == 0 and not library.refresh.isEnabled()


def test_signout_drops_inflight_private_results(desktop_window, backend, qtbot):
    backend.release.clear()
    backend.body = json.dumps(payload("PRIVATE")).encode()
    window = desktop_window(f"http://127.0.0.1:{backend.server_port}", request_timeout=5)
    library = signed_in(window)
    qtbot.waitUntil(backend.entered.is_set)
    window.account.activity_changed.emit(True)
    window.account.session.tokens = None
    window.account.activity_changed.emit(False)
    window.account.session_changed.emit()
    backend.release.set()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.table.rowCount() == 0 and library.last_page is None
    assert "Sign in" in library.state.title.text()


def test_filter_edits_coalesce_and_hide_stale_responses(desktop_window, monkeypatch, qtbot):
    entered, release = Event(), Event()
    calls = []

    def listing(self, access, *, search, sort, page):
        calls.append((search, sort, page))
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return LibraryPage(
            (DeckSummary(uuid4(), search or "Old", datetime.now(UTC), "ready", 0),), 1, page, 20
        )

    monkeypatch.setattr(LibraryService, "list", listing)
    window = desktop_window()
    library = signed_in(window)
    qtbot.waitUntil(entered.is_set)
    library.search.setText("first")
    library.search.setText("latest")
    library.sort.setCurrentIndex(2)
    library.reload()
    assert library.table.rowCount() == 0
    release.set()
    qtbot.waitUntil(lambda: not library.runner.busy and len(calls) == 2)
    assert calls[-1] == ("latest", "title_asc", 1)
    assert library.table.item(0, 0).text() == "latest"


def test_paging_and_return_to_view_reload(desktop_window, monkeypatch, qtbot):
    calls = []

    def listing(self, access, *, search, sort, page):
        calls.append(page)
        rows = tuple(
            DeckSummary(uuid4(), f"Deck {i}", datetime.now(UTC), "ready", 0)
            for i in range(20 if page == 1 else 1)
        )
        return LibraryPage(rows, 21, page, 20)

    monkeypatch.setattr(LibraryService, "list", listing)
    window = desktop_window()
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.next.isEnabled() and not library.previous.isEnabled()
    library.next.click()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.previous.isEnabled() and not library.next.isEnabled()
    assert library.table.rowCount() == 1
    window.navigation.setCurrentRow(0)
    assert library.table.rowCount() == 0
    window.navigation.setCurrentRow(1)
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert calls == [1, 2, 2]


def test_shutdown_discards_results_and_keeps_event_loop_running(desktop_window, backend, qtbot):
    backend.release.clear()
    backend.body = json.dumps(payload()).encode()
    window = desktop_window(f"http://127.0.0.1:{backend.server_port}", request_timeout=5)
    library = signed_in(window)
    qtbot.waitUntil(backend.entered.is_set)
    assert not window.close()
    backend.release.set()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert library.table.rowCount() == 0


@pytest.mark.parametrize("remaining", [0, 1], ids=["empty-collection", "nonempty-collection"])
def test_disappeared_page_recovers_and_account_check_preserves_page(
    desktop_window, monkeypatch, qtbot, remaining
):
    calls = []

    def listing(self, access, *, search, sort, page):
        calls.append(page)
        count = 21 if len(calls) == 1 else remaining
        rows = (
            ()
            if page > 1
            else tuple(
                DeckSummary(uuid4(), f"Deck {i}", datetime.now(UTC), "ready", 0)
                for i in range(min(count, 20))
            )
        )
        return LibraryPage(rows, count, page, 20)

    monkeypatch.setattr(LibraryService, "list", listing)
    window = desktop_window()
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    library.page = 2
    window.account.session_changed.emit()
    assert library.page == 2
    qtbot.waitUntil(lambda: not library.runner.busy and len(calls) == 3)
    assert calls == [1, 2, 1]
    assert library.page == 1 and library.table.rowCount() == remaining
    assert library.count.text().startswith("Page 1 of 1")
    assert not library.previous.isEnabled() and not library.next.isEnabled()
    if remaining == 0:
        assert library.state.title.text() == "No saved decks"


def test_backend_switch_and_abandoned_view_ignore_late_results(desktop_window, backend, qtbot):
    backend.release.clear()
    backend.body = json.dumps(payload("Private old backend")).encode()
    window = desktop_window(f"http://127.0.0.1:{backend.server_port}", request_timeout=5)
    library = signed_in(window)
    qtbot.waitUntil(backend.entered.is_set)
    window.navigation.setCurrentRow(3)
    # Settings cannot repoint a live authenticated request.
    original = window.config.api_url
    window.save_settings("http://127.0.0.1:9000")
    assert window.config.api_url == original
    backend.release.set()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.last_page is None and library.table.rowCount() == 0
    window.save_settings("http://127.0.0.1:9000")
    assert library.config.api_url == "http://127.0.0.1:9000"
    assert window.account.session is None and library.last_page is None


def test_poll_uses_current_filters_and_stops_when_leaving(desktop_window, monkeypatch, qtbot):
    calls = []

    def listing(self, access, *, search, sort, page):
        calls.append(search)
        return LibraryPage(
            (DeckSummary(uuid4(), "Deck", datetime.now(UTC), "processing", 1),), 1, page, 20
        )

    monkeypatch.setattr(LibraryService, "list", listing)
    window = desktop_window()
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    library.search.setText("Deck")
    library.reload()
    qtbot.waitUntil(lambda: not library.runner.busy)
    library.poll.timeout.emit()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert calls == ["", "Deck", "Deck"]
    window.navigation.setCurrentRow(0)
    assert not library.poll.isActive()


@pytest.mark.parametrize("size", [(800, 600), (1120, 760)])
def test_native_layout_with_long_titles(desktop_window, monkeypatch, qtbot, qapp, size):
    rows = tuple(
        DeckSummary(uuid4(), title, datetime.now(UTC), status, 2)
        for title, status in [
            ("Quarterly results", "processing"),
            ("Product roadmap", "failed"),
            ("Research notes " + "W" * 220, "incomplete"),
        ]
    )
    monkeypatch.setattr(LibraryService, "list", lambda *args, **kwargs: LibraryPage(rows, 3, 1, 20))
    old_style = qapp.styleSheet()
    qapp.setStyleSheet(files("buddy_desktop").joinpath("theme.qss").read_text(encoding="utf-8"))
    try:
        window = desktop_window()
        window.resize(*size)
        library = signed_in(window)
        qtbot.waitUntil(lambda: not library.runner.busy)
        qtbot.waitUntil(lambda: library.table.columnWidth(0) > 0)
        assert library.table.columnWidth(0) > 100
        for control in [library.search, library.sort, library.refresh, library.table, library.next]:
            assert library.rect().contains(control.geometry())
        library.table.selectRow(2)
        assert rows[2].title in library.detail.toPlainText()
        qtbot.waitUntil(lambda: library.detail.verticalScrollBar().maximum() > 0)
        qtbot.waitUntil(lambda: library.count.width() >= library.count.sizeHint().width())
        destination = Path("build")
        destination.mkdir(exist_ok=True)
        assert window.grab().save(str(destination / f"library-preview-{size[0]}.png"))
    finally:
        qapp.setStyleSheet(old_style)


@pytest.mark.parametrize("invalidate_during_success", [False, True])
def test_upload_retry_reuses_key_until_confirmed_success(
    desktop_window, qtbot, monkeypatch, tmp_path, invalidate_during_success
):
    from PySide6.QtWidgets import QFileDialog

    from buddy_desktop.presentation_service import PresentationService, SavedPresentation

    window = desktop_window()
    window.account.session = SimpleNamespace(
        tokens=Tokens("access", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.session_changed.emit()
    library = window.library
    library.active = True
    monkeypatch.setattr(LibraryService, "list", lambda *a, **kw: LibraryPage((), 0, 1, 20))
    path = tmp_path / "deck.pdf"
    path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(path), ""))
    keys = []
    entered, release = Event(), Event()

    def upload(self, chosen, key):
        assert chosen == path
        keys.append(key)
        if len(keys) == 1:
            raise ServiceError("Uncertain transfer outcome")
        if len(keys) == 2 and invalidate_during_success:
            entered.set()
            assert release.wait(5)
        return SavedPresentation(str(uuid4()))

    monkeypatch.setattr(PresentationService, "upload", upload)
    library.choose_upload()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert len(library.upload_keys) == 1
    library.choose_upload()
    if invalidate_during_success:
        qtbot.waitUntil(entered.is_set)
        library.invalidate()
        release.set()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert not library.upload_keys
    library.choose_upload()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert keys[0] == keys[1] and keys[2] != keys[1]
    assert not library.upload_keys
