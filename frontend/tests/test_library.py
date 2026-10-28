import json
import time
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from PySide6.QtCore import Qt, QTimer

from buddy_desktop.auth import Tokens
from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import (
    DeckSummary,
    LibraryAccessExpired,
    LibraryPage,
    LibraryService,
    ProcessingDetail,
    ProcessingResult,
)
from buddy_desktop.services import ServiceError
from buddy_desktop.visuals import apply_theme


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


def test_processing_detail_is_typed_and_retry_calls_owned_job_endpoint(backend):
    job_id = str(uuid4())
    value = payload()
    value["data"]["items"][0]["processing"] = {
        "job_id": job_id,
        "state": "failed",
        "stage": "provider",
        "progress": 60,
        "attempts": 2,
        "error_code": "provider_unavailable",
        "can_retry": True,
        "can_cancel": False,
    }
    backend.body = json.dumps(value).encode()
    library = service(backend)
    deck = library.list("test-token", search="", sort="newest", page=1).items[0]
    assert deck.processing is not None
    assert deck.processing.state == "failed" and deck.processing.can_retry
    backend.body = json.dumps(
        {"data": {"id": job_id, "status": "queued", "stage": "pdf_metadata.v1"}}
    ).encode()
    result = library.operate_job("test-token", UUID(job_id), "retry")
    assert result.job_id == UUID(job_id) and result.status == "queued"
    assert backend.method == "POST"
    assert backend.path == f"/api/v1/jobs/{job_id}/retry"
    assert backend.authorization == "Bearer test-token"


@pytest.mark.parametrize("code", [401, 403, 429, 503, 302])
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


def test_library_exposes_retry_for_failed_job_and_refreshes_after_action(
    desktop_window, monkeypatch, qtbot
):
    job_id = uuid4()
    deck = DeckSummary(
        uuid4(),
        "Failed PDF",
        datetime.now(UTC),
        "incomplete",
        2,
        "pdf",
        True,
        ProcessingDetail(job_id, "failed", "provider", 60, 2, "provider_unavailable", True, False),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        LibraryService,
        "list",
        lambda self, *args, **kwargs: LibraryPage((deck,), 1, 1, 20),
    )

    def operate(self, access, actual_job, operation):
        calls.append(operation)
        return ProcessingResult(actual_job, "queued", "pdf_metadata.v1")

    monkeypatch.setattr(LibraryService, "operate_job", operate)
    window = desktop_window()
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.retry_job_button.isVisible()
    assert not library.cancel_job_button.isVisible()
    library.retry_job_button.click()
    qtbot.waitUntil(lambda: not library.runner.busy and len(calls) == 1)
    qtbot.waitUntil(lambda: library.table.rowCount() == 1)
    assert calls == ["retry"]


def test_native_layout_with_long_titles(desktop_window, monkeypatch, qtbot, qapp):
    rows = tuple(
        DeckSummary(uuid4(), title, datetime.now(UTC), status, 2)
        for title, status in [
            ("Quarterly results", "processing"),
            ("Product roadmap", "failed"),
            ("Research notes " + "W" * 220, "incomplete"),
        ]
    )
    monkeypatch.setattr(LibraryService, "list", lambda *args, **kwargs: LibraryPage(rows, 3, 1, 20))
    old_style, old_palette = qapp.styleSheet(), qapp.palette()
    apply_theme(qapp)
    try:
        window = desktop_window()
        window.resize(800, 600)  # The minimum window size is the binding layout constraint.
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
    finally:
        qapp.setStyleSheet(old_style)
        qapp.setPalette(old_palette)


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


@pytest.mark.parametrize(
    "source_format,label", [("pptx", "Inspecting PowerPoint"), ("pdf", "Inspecting PDF")]
)
def test_inspection_label_matches_the_source_format(source_format, label):
    from buddy_desktop.library_widget import LibraryWidget

    deck = DeckSummary(
        uuid4(),
        "Slides",
        datetime.now(UTC),
        "processing",
        1,
        source_format=source_format,
        processing=ProcessingDetail(
            uuid4(),
            "inspecting_pdf_metadata",
            "pptx_metadata.v1" if source_format == "pptx" else "pdf_metadata.v1",
            10,
            1,
            None,
            False,
            True,
        ),
    )
    assert LibraryWidget.deck_status_text(deck)[0] == label


def test_poll_keeps_rows_selectable_and_preserves_latest_selection(
    desktop_window, monkeypatch, qtbot
):
    rows = tuple(
        DeckSummary(uuid4(), name, datetime.now(UTC), "processing", 1)
        for name in ("First PowerPoint", "Second PowerPoint")
    )
    entered, release = Event(), Event()
    calls = []

    def listing(*_args, **_kwargs):
        calls.append(True)
        if len(calls) > 1:
            entered.set()
            assert release.wait(5)
            return LibraryPage(tuple(reversed(rows)), 2, 1, 20)
        return LibraryPage(rows, 2, 1, 20)

    monkeypatch.setattr(LibraryService, "list", listing)
    window = desktop_window()
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    library.table.selectRow(1)
    try:
        library.poll.timeout.emit()
        qtbot.waitUntil(entered.is_set)
        assert not library.table.isHidden() and library.table.rowCount() == 2
        assert library.table.currentRow() == 1
        qtbot.mouseClick(
            library.table.viewport(),
            Qt.MouseButton.LeftButton,
            pos=library.table.visualItemRect(library.table.item(0, 0)).center(),
        )
        assert rows[0].title in library.detail.toPlainText()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.table.currentRow() == 1  # First deck moved to row 1 in the refreshed data.
    assert rows[0].title in library.detail.toPlainText()
    assert "Open saved PowerPoint" in library.open_button.toolTip()
