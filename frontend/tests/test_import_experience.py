"""Import interaction, real isolated PDF previews and private-state lifecycle."""

from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest
from frontend.tests.test_library import signed_in
from frontend.tests.test_upload_flow import upload_http as upload_http_fixture  # noqa: F401
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage

from buddy_desktop.import_surface import ImportPreview, dropped_path
from buddy_desktop.library_service import DeckSummary, LibraryPage, LibraryService
from buddy_desktop.visuals import BuddyMark, apply_theme


@pytest.fixture
def upload_http(request):
    return request.getfixturevalue("upload_http_fixture")


FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"


def mime_for(*paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime


@pytest.mark.parametrize("kind", ["many", "remote", "directory", "unsupported", "missing"])
def test_drop_rejects_non_local_or_ambiguous_inputs(tmp_path, kind):
    file = tmp_path / "deck.pptx"
    file.write_bytes(b"fixture")
    mime = mime_for(file)
    if kind == "many":
        mime = mime_for(file, file)
    elif kind == "remote":
        mime.setUrls([QUrl("https://example.test/deck.pptx")])
    elif kind == "directory":
        mime = mime_for(tmp_path)
    elif kind == "unsupported":
        file = tmp_path / "deck.exe"
        file.write_bytes(b"fixture")
        mime = mime_for(file)
    else:
        mime = mime_for(tmp_path / "absent.pdf")
    assert dropped_path(mime) is None


def test_real_pdf_thumbnails_are_progressive_and_clearable(qtbot):
    preview = ImportPreview()
    qtbot.addWidget(preview)
    preview.show()
    counts = []
    preview.page_ready.connect(lambda *_: counts.append(len(preview.images)))
    preview.start(FIXTURE)
    qtbot.waitUntil(lambda: not preview.runner.busy, timeout=20000)
    assert counts == [1, 2, 3]
    assert len(preview.images) == 3
    assert all(not label.pixmap().isNull() for label in preview.images)
    assert "3 slides" in preview.title.text()
    assert len({label.pixmap().cacheKey() for label in preview.images}) == 3
    preview.clear()
    assert not preview.images and not preview.isVisible()


def test_late_private_preview_is_discarded(qtbot, monkeypatch):
    import buddy_desktop.import_surface as module

    entered, release = Event(), Event()

    def render(*args):
        entered.set()
        release.wait(4)
        image = QImage(20, 20, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.white)
        return {"sizes": [[20, 20]]}, image

    monkeypatch.setattr(module, "render", render)
    preview = ImportPreview()
    qtbot.addWidget(preview)
    preview.start(FIXTURE)
    qtbot.waitUntil(entered.is_set)
    old_operation = preview.cancelled
    preview.start(FIXTURE)
    assert preview.pending_path == FIXTURE
    preview.clear()
    release.set()
    qtbot.waitUntil(lambda: not preview.runner.busy)
    preview.page_ready.emit(old_operation, 0, QImage(), 1)
    assert not preview.images and not preview.title.text()
    assert preview.pending_path is None


def test_close_waits_for_preview_without_blocking_gui(desktop_window, qtbot, monkeypatch):
    import buddy_desktop.import_surface as module

    entered, release = Event(), Event()

    def render(*args):
        entered.set()
        release.wait(4)
        image = QImage(20, 20, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.white)
        return {"sizes": [[20, 20]]}, image

    monkeypatch.setattr(module, "render", render)
    window = desktop_window()
    preview = window.library.preview
    preview.start(FIXTURE)
    qtbot.waitUntil(entered.is_set)
    assert not window.close()
    assert window.isVisible() and window._closing
    qtbot.wait(40)
    assert preview.runner.busy
    release.set()
    qtbot.waitUntil(lambda: not preview.runner.busy and not window.isVisible())
    assert not preview.images


def test_window_drop_uses_real_upload_and_pptx_truth(
    upload_http, desktop_window, qtbot, monkeypatch
):
    monkeypatch.setattr(LibraryService, "list", lambda *a, **kw: LibraryPage((), 0, 1, 20))
    window = desktop_window(f"http://127.0.0.1:{upload_http.server_port}", request_timeout=5)
    library = signed_in(window)
    qtbot.waitUntil(lambda: not library.runner.busy)
    path = Path(__file__).parent / "fixtures/slide_actions/standard.pptx"
    mime = mime_for(path.resolve())
    upload_http.release.clear()
    hover = QDragEnterEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dragEnterEvent(hover)
    assert hover.isAccepted()
    assert window.activity_mark.state == "attention"
    drop = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(drop)
    assert drop.isAccepted() and window.pages.currentIndex() == 1
    qtbot.waitUntil(upload_http.entered.is_set)
    assert library.upload_name.text() == path.name
    assert window.activity_mark.state == "working"
    assert not library.preview.images
    assert "not available" in library.preview.title.text()
    upload_http.release.set()
    qtbot.waitUntil(lambda: not library.runner.busy)
    assert upload_http.bodies == [path.read_bytes()]
    assert "PowerPoint saved" in library.upload_status.text()
    assert window.activity_mark.state == "success"
    assert library.upload_bar.value() == 100
    library.account.session.tokens = None
    library.account_changed()
    assert not library.upload_name.text() and not library.preview.images
    assert not library.upload_panel.isVisible()


def test_signed_out_drop_does_not_start_upload(desktop_window):
    window = desktop_window()
    event = QDragEnterEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        mime_for(FIXTURE),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dragEnterEvent(event)
    assert not event.isAccepted() and not window.library.runner.busy
    assert not window.library.import_surface.browse.isEnabled()


@pytest.mark.parametrize("size", [(800, 600), (1200, 800)])
def test_pdf_import_card_with_real_previews_and_upload(
    upload_http, desktop_window, qtbot, monkeypatch, qapp, size
):
    def listing(*args, **kwargs):
        rows = (
            ()
            if upload_http.record["status"] != "complete"
            else (
                DeckSummary(
                    UUID(upload_http.record["deck"]["id"]),
                    FIXTURE.stem,
                    datetime.now(UTC),
                    "uploaded",
                    0,
                    "pdf",
                    True,
                ),
            )
        )
        return LibraryPage(rows, len(rows), 1, 20)

    monkeypatch.setattr(LibraryService, "list", listing)
    palette, stylesheet = qapp.palette(), qapp.styleSheet()
    apply_theme(qapp)
    try:
        window = desktop_window(f"http://127.0.0.1:{upload_http.server_port}", request_timeout=5)
        window.resize(*size)
        library = signed_in(window)
        qtbot.waitUntil(lambda: not library.runner.busy)
        upload_http.release.clear()
        library.start_upload(FIXTURE)
        qtbot.waitUntil(upload_http.entered.is_set)
        qtbot.waitUntil(lambda: not library.preview.runner.busy, timeout=20000)
        assert len(library.preview.images) == 3
        scroll = window.pages.currentWidget()
        scroll.ensureWidgetVisible(library.import_surface)
        qapp.processEvents()
        assert scroll.horizontalScrollBar().maximum() == 0
        assert library.preview.parent() is library.upload_panel
        assert library.upload_panel.parent() is library.import_surface
        for thumbnail in library.preview.images:
            assert not thumbnail.pixmap().isNull()
            assert thumbnail.pixmap().width() <= thumbnail.width()
        destination = Path("build/issue112")
        destination.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(destination / f"pdf-import-{size[0]}.png"))
        upload_http.release.set()
        qtbot.waitUntil(lambda: not library.runner.busy)
        assert upload_http.bodies == [FIXTURE.read_bytes()]
        assert "Presentation saved" in library.upload_status.text()
        qapp.processEvents()
        assert window.grab().save(str(destination / f"pdf-saved-{size[0]}.png"))
        assert library.dismiss_upload_button.isVisible()
        library.dismiss_upload_button.click()
        assert not library.upload_panel.isVisible() and not library.preview.images
        assert library.table.rowCount() == 1
    finally:
        upload_http.release.set()
        qapp.setStyleSheet(stylesheet)
        qapp.setPalette(palette)


def test_mascot_interrupts_and_preserves_semantics_with_reduced_motion(qtbot):
    mark = BuddyMark(100)
    qtbot.addWidget(mark)
    mark.show()
    mark.set_state("attention")
    qtbot.wait(40)
    current = mark.pose
    mark.set_state("error")
    assert mark.animation.startValue() == current
    assert "attention" in mark.accessibleDescription()
    mark.reduced_motion = True
    mark.set_state("working")
    assert mark.active and not mark.timer.isActive()
    assert mark.animation.state() == mark.animation.State.Stopped
    mark.set_state("presenting")
    assert not mark.active
    mark.reduced_motion = False
    mark.set_state("working")
    assert mark.timer.isActive()
    mark.hide()
    assert not mark.timer.isActive()


@pytest.mark.parametrize("size", [(800, 600), (1200, 800)])
def test_revamp_rendered_routes_and_long_import_name(desktop_window, qapp, size):
    palette, stylesheet = qapp.palette(), qapp.styleSheet()
    apply_theme(qapp)
    try:
        window = desktop_window()
        window.resize(*size)
        destination = Path("build/issue112")
        destination.mkdir(parents=True, exist_ok=True)
        for route, name in enumerate(("home", "library", "present", "settings", "profile")):
            window.navigation.setCurrentRow(route if route < 4 else -1)
            if route == 4:
                window.navigate(4)
            qapp.processEvents()
            scroll = window.pages.currentWidget()
            assert scroll.horizontalScrollBar().maximum() == 0
            assert window.grab().save(str(destination / f"{name}-{size[0]}.png"))
        window.navigation.setCurrentRow(1)
        library = window.library
        library.upload_name.setText("<Quarterly> " + "long presentation title " * 15 + ".pptx")
        library.upload_panel.show()
        qapp.processEvents()
        assert window.pages.currentWidget().horizontalScrollBar().maximum() == 0
        assert library.upload_name.textFormat() == Qt.TextFormat.PlainText
    finally:
        qapp.setStyleSheet(stylesheet)
        qapp.setPalette(palette)
