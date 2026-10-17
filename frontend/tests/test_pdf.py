"""Actual isolated PDF rendering and the existing six-command presentation path."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QInputDialog

from buddy_desktop.pdf_document import open_pdf, render
from buddy_desktop.slide_contract import ContractError, Region

FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"


def test_pdf_renderer_order_rotation_crop_fractional_dimensions_and_pixels():
    manifest, image = open_pdf(FIXTURE)
    assert [(s.width, s.height) for s in manifest.slides] == [(1440, 810), (1000, 600), (1441, 811)]
    assert (image.width(), image.height()) == (1440, 810)
    color = image.pixelColor(200, 500)
    assert color.red() > 200 and 60 < color.green() < 150 and color.blue() < 80
    metadata, rotated = render(manifest.pdf_content, 1)
    assert (rotated.width(), rotated.height()) == (1000, 600)
    assert metadata["sizes"] == [[1440, 810], [1000, 600], [1441, 811]]
    assert open_pdf(FIXTURE)[0].slides[0].slide_id == manifest.slides[0].slide_id


def test_pdf_navigation_zoom_highlight_reset_and_private_clear(desktop_window, qtbot, tmp_path):
    window = desktop_window()
    window.navigation.setCurrentRow(2)
    page = window.present
    page.load_pdf(FIXTURE)
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    assert page.executor.manifest is not None, page.feedback.text()
    assert page.counter.text() == "Slide 1 of 3"
    page.send("zoom_region", {"region": {"x": 96, "y": 410, "width": 480, "height": 240}})
    page.send("highlight_region", {"region": {"x": 100, "y": 450, "width": 100, "height": 40}})
    assert page.surface.highlight == Region(100, 450, 100, 40)
    page.surface.grab().save(str(tmp_path / "pdf-zoom-highlight.png"))
    page.send("next_slide")
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    assert page.counter.text() == "Slide 2 of 3"
    assert page.surface.highlight is None
    page.send("go_to_slide", {"target_slide_id": page.executor.manifest.slides[2].slide_id})
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    assert page.counter.text() == "Slide 3 of 3"
    page.send("previous_slide")
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    page.send("reset_view")
    assert page.surface.region == page.surface.slide.full
    window._private_presentation = True
    window._clear_private_pdf()
    assert page.executor.manifest is None and page.surface.slide is None


@pytest.mark.parametrize("content", [b"not a PDF", b"%PDF-1.7\nbroken"])
def test_invalid_pdf_leaves_previous_committed_presentation(desktop_window, qtbot, content):
    page = desktop_window().present
    page.load_pdf(FIXTURE)
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    previous = page.surface.slide
    page.load_pdf(content)
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    assert page.surface.slide == previous
    assert page.executor.manifest is not None


def test_pdf_byte_limit():
    with pytest.raises(ContractError):
        render(b"%PDF-" + b" " * (64 * 1024 * 1024))


def test_finished_pdf_worker_before_monitor_attachment_is_accepted(monkeypatch):
    import buddy_desktop.pdf_document as module

    def finished(pid):
        raise module.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(module.psutil, "Process", finished)
    manifest, image = open_pdf(FIXTURE)
    assert len(manifest.slides) == 3 and not image.isNull()


def test_native_fullscreen_navigation_highlight_zoom_and_restore(
    desktop_window, qtbot, monkeypatch
):
    window = desktop_window()
    window.navigation.setCurrentRow(2)
    page = window.present
    assert not page.fullscreen_button.isEnabled()
    page.load_pdf(FIXTURE)
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    original_surface = page.surface
    original_index = page.body.indexOf(original_surface)
    page.fullscreen_button.click()
    full = page.fullscreen
    qtbot.waitUntil(lambda: full.isFullScreen() and QApplication.activeWindow() == full)
    assert full.centralWidget() is original_surface
    assert not full.menuBar().isVisible()
    qtbot.keyClick(full, Qt.Key.Key_Right)
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    assert page.counter.text() == "Slide 2 of 3"
    monkeypatch.setattr(QInputDialog, "getInt", lambda *args: (3, True))
    qtbot.keyClick(full, Qt.Key.Key_G)
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    assert page.counter.text() == "Slide 3 of 3"
    qtbot.keyClick(full, Qt.Key.Key_Left)
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    qtbot.keyClick(full, Qt.Key.Key_Left)
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    for name, value in {"x": 96, "y": 410, "width": 480, "height": 240}.items():
        page.fields[name].setValue(value)
    qtbot.keyClick(full, Qt.Key.Key_H)
    assert page.surface.highlight == Region(96, 410, 480, 240)
    qtbot.keyClick(full, Qt.Key.Key_Z)
    assert page.surface.region == Region(96, 410, 480, 240)
    qtbot.keyClick(full, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: page.fullscreen is None)
    assert page.body.indexOf(original_surface) == original_index
    assert page.surface.region == Region(96, 410, 480, 240)
    assert window.isVisible()
    page.enter_fullscreen()
    assert page.fullscreen.isFullScreen()
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert page.fullscreen is None


def test_failed_local_open_does_not_remove_private_account_boundary(desktop_window, qtbot):
    window = desktop_window()
    page = window.present
    page.load_pdf(FIXTURE, deck_id="private-deck", revision="private-v1")
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    assert page.executor.manifest is not None, page.feedback.text()
    assert window._private_presentation
    page.load_pdf(b"not a valid PDF")
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    assert window._private_presentation
    window.account.session_changed.emit()
    assert page.executor.manifest is None and page.surface.slide is None
