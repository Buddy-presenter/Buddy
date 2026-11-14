"""Real HTTP and Qt interaction coverage for owned deck details and rename."""

import json
from uuid import UUID

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from test_library import payload, signed_in

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryService
from buddy_desktop.services import ServiceError


def detail_payload(row, *, title=None):
    return {
        "data": {
            "id": row["id"],
            "title": title or row["title"],
            "created_at": row["created_at"],
            "source_format": "pdf",
            "processing": {
                "job_id": None,
                "state": "queued",
                "stage": "provider",
                "progress": 20,
                "attempts": 1,
                "error_code": None,
                "can_retry": False,
                "can_cancel": False,
            },
            "analysis": {"status": "unavailable", "version": None, "slide_count": 0},
        },
        "request_id": "test",
    }


def test_details_and_normalized_rename_use_owned_http_contract(backend):
    row = payload()["data"]["items"][0]
    backend.body = json.dumps(detail_payload(row)).encode()
    service = LibraryService(AppConfig(f"http://127.0.0.1:{backend.server_port}"))
    deck_id = UUID(row["id"])
    assert service.deck_details("test-token", deck_id).processing.progress == 20
    assert backend.path == f"/api/v1/decks/{deck_id}"
    backend.body = json.dumps(detail_payload(row, title="New name")).encode()
    assert service.rename_deck("test-token", deck_id, "  New name  ").title == "New name"
    assert backend.method == "PATCH" and backend.authorization == "Bearer test-token"
    assert json.loads(backend.request_body) == {"title": "New name"}
    with pytest.raises(ServiceError):
        service.rename_deck("test-token", deck_id, "\n  ")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(id="bad"),
        lambda d: d.update(source_format="html"),
        lambda d: d["processing"].update(progress=True),
        lambda d: d["analysis"].update(slide_count=501),
        lambda d: d["analysis"].update(version=True),
    ],
)
def test_malformed_detail_payloads_are_rejected(backend, mutate):
    row = payload()["data"]["items"][0]
    value = detail_payload(row)
    mutate(value["data"])
    backend.body = json.dumps(value).encode()
    service = LibraryService(AppConfig(f"http://127.0.0.1:{backend.server_port}"))
    with pytest.raises(ServiceError, match="invalid deck details"):
        service.deck_details("test-token", UUID(row["id"]))


def test_detail_view_rename_back_and_cancel_delete(desktop_window, backend, qtbot):
    listing = payload("<b>Literal deck</b>")
    row = listing["data"]["items"][0]
    backend.body = json.dumps(listing).encode()
    window = desktop_window(f"http://127.0.0.1:{backend.server_port}")
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    backend.body = json.dumps(detail_payload(row)).encode()
    library.details_button.click()
    qtbot.waitUntil(lambda: not library.runner.busy)
    panel = library.deck_details
    assert panel.isVisible() and library.table.isHidden()
    assert panel.name.text() == row["title"]
    assert "Progress: 20%" in panel.summary.toPlainText()
    window.resize(800, 600)
    qtbot.waitUntil(lambda: window.pages.currentWidget().horizontalScrollBar().maximum() == 0)
    panel.name.setText("New name")
    backend.body = json.dumps(detail_payload(row, title="New name")).encode()
    qtbot.keyClick(panel.name, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert panel.data.title == "New name" and not panel.save.isEnabled()
    assert library.last_page.items[0].title == "New name"
    backend.entered.clear()

    def cancel_delete():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QMessageBox)
        assert dialog.textFormat() == Qt.TextFormat.PlainText
        dialog.button(QMessageBox.StandardButton.Cancel).click()

    QTimer.singleShot(0, cancel_delete)
    panel.delete.click()
    assert not backend.entered.is_set()
    row["title"] = "New name"
    backend.body = json.dumps(listing).encode()
    panel.back.click()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.table.isVisible() and panel.isHidden()
    assert library.table.item(0, 0).text() == "New name"


def test_signout_discards_late_details_without_blocking_gui(desktop_window, backend, qtbot):
    listing = payload()
    backend.body = json.dumps(listing).encode()
    window = desktop_window(f"http://127.0.0.1:{backend.server_port}", request_timeout=5)
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    backend.entered.clear()
    backend.release.clear()
    backend.body = json.dumps(
        detail_payload(listing["data"]["items"][0], title="private late name")
    ).encode()
    library.details_button.click()
    try:
        qtbot.waitUntil(backend.entered.is_set)
        assert library.runner.busy
        window.account.session = None
        window.account.session_changed.emit()
        assert library.deck_details.isHidden()
        assert library.deck_details.name.text() == ""
    finally:
        backend.release.set()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert library.deck_details.data is None and library.table.rowCount() == 0
