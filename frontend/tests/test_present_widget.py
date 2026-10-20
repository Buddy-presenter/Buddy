"""Manual presentation controls exercise the same production executor as generated actions."""

from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QFileDialog

from buddy_desktop.slide_contract import Region

FIXTURES = Path(__file__).parent / "fixtures" / "slide_actions"


@pytest.fixture
def present(desktop_window, qtbot):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    page.load_manifest(FIXTURES / "manifest.json")
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.counter.text() == "Slide 1 of 3"
    return window, page


def region_fields(page, region):
    for name, value in region.items():
        page.fields[name].setValue(value)


def test_open_dialog_and_all_six_buttons(desktop_window, qtbot, monkeypatch):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    assert not page.next_button.isEnabled() and not page.highlight_button.isEnabled()
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *args: (str(FIXTURES / "manifest.json"), "")
    )
    qtbot.mouseClick(page.open_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not page.executor.busy)
    qtbot.mouseClick(page.next_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.counter.text() == "Slide 2 of 3"
    qtbot.mouseClick(page.previous_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.counter.text() == "Slide 1 of 3"
    page.target.setCurrentIndex(2)
    qtbot.mouseClick(page.go_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.counter.text() == "Slide 3 of 3"
    region = {"x": 80, "y": 64, "width": 800, "height": 200}
    region_fields(page, region)
    qtbot.mouseClick(page.zoom_button, Qt.MouseButton.LeftButton)
    assert page.surface.region == Region(**region)
    region_fields(page, {"x": 100, "y": 100, "width": 100, "height": 20})
    qtbot.mouseClick(page.highlight_button, Qt.MouseButton.LeftButton)
    assert page.surface.highlight == Region(100, 100, 100, 20)
    assert page.surface.region == Region(**region)
    qtbot.mouseClick(page.reset_button, Qt.MouseButton.LeftButton)
    assert page.surface.highlight is None
    assert page.surface.region == page.surface.slide.full
    assert page.surface.selection is None
    assert not page.highlight_button.isEnabled()


def test_drag_reversed_drag_letterbox_and_zoomed_selection(present, qtbot):
    _, page = present
    surface = page.surface
    region_fields(page, {"x": 200, "y": 100, "width": 1200, "height": 600})
    page.zoom_button.click()
    mapping = surface.mapping()
    start = mapping.point(300, 200).toPoint()
    end = mapping.point(500, 300).toPoint()
    qtbot.mousePress(surface, Qt.MouseButton.LeftButton, pos=end)
    qtbot.mouseMove(surface, start)
    qtbot.mouseRelease(surface, Qt.MouseButton.LeftButton, pos=start)
    page.highlight_button.click()
    selected = surface.highlight
    assert abs(selected.x - 300) < 6 and abs(selected.y - 200) < 6
    assert abs(selected.width - 200) < 6 and abs(selected.height - 100) < 6
    page.reset_button.click()
    surface.resize(400, 400)
    # Top corner is outside the wide slide's letterboxed image, so cannot start a selection.
    qtbot.mousePress(surface, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
    qtbot.mouseRelease(surface, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    assert surface.selection is None


def test_empty_click_invalid_fields_boundary_and_failed_open(present, qtbot, tmp_path):
    _, page = present
    qtbot.mouseClick(page.surface, Qt.MouseButton.LeftButton, pos=page.surface.rect().center())
    assert not page.highlight_button.isEnabled()
    region_fields(page, {"x": 1910, "y": 100, "width": 100, "height": 20})
    page.highlight_button.click()
    assert "bounds" in page.feedback.text()
    assert page.surface.highlight is None
    region_fields(page, {"x": 80, "y": 64, "width": 100, "height": 20})
    page.highlight_button.click()
    before = page.surface.highlight
    page.previous_button.click()
    assert page.surface.highlight == before
    page.next_button.click()
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.surface.highlight is None
    assert page.surface.selection is None
    page.load_manifest(tmp_path / "missing.json")
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.reset_button.isEnabled()
    assert page.counter.text() == "Slide 2 of 3"
    assert "JSON" in page.feedback.text()


def test_close_during_slide_read_waits_without_blocking(present, qtbot, monkeypatch):
    import buddy_desktop.slide_executor as module

    window, page = present
    entered, release = Event(), Event()
    real_load = module.load_image

    def delayed(manifest, slide):
        entered.set()
        assert release.wait(4)
        return real_load(manifest, slide)

    monkeypatch.setattr(module, "load_image", delayed)
    page.next_button.click()
    qtbot.waitUntil(entered.is_set)
    assert not page.next_button.isEnabled()
    assert page.reset_button.isEnabled()
    try:
        assert not window.close()
        assert window.isVisible()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.executor.busy)
    qtbot.waitUntil(lambda: not window.isVisible())
    assert page.executor.current_index == 0
