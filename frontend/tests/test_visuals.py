"""Native branded UI checks, independent of a live account or paid providers."""

from importlib.resources import files

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QPixmap
from PySide6.QtWidgets import QPushButton, QScrollArea

from buddy_desktop.visuals import BuddyMark, apply_theme, icon


@pytest.fixture
def themed(qapp):
    palette, stylesheet = qapp.palette(), qapp.styleSheet()
    apply_theme(qapp)
    yield qapp
    qapp.setStyleSheet(stylesheet)
    qapp.setPalette(palette)


def test_packaged_character_and_every_lucide_icon(themed):
    asset = QPixmap()
    assert asset.loadFromData(files("buddy_desktop").joinpath("assets", "buddy.png").read_bytes())
    assert asset.hasAlphaChannel() and asset.width() >= 256
    assert asset.toImage().pixelColor(0, 0).alpha() == 0
    assert files("buddy_desktop").joinpath("assets", "icons", "LICENSE").is_file()
    for item in files("buddy_desktop").joinpath("assets", "icons").iterdir():
        if item.name.endswith(".svg"):
            rendered = icon(item.name[:-4]).pixmap(24, 24).toImage()
            assert not rendered.isNull()
            assert any(rendered.pixelColor(x, y).alpha() for x in range(24) for y in range(24))


def contrast(foreground, background):
    def luminance(color):
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_readable_palette_and_semantic_text(themed):
    palette = themed.palette()
    assert (
        contrast(
            palette.color(QPalette.ColorRole.WindowText).name(),
            palette.color(QPalette.ColorRole.Window).name(),
        )
        >= 7
    )
    for foreground, background in [
        ("#A4A4AF", "#0B0C10"),
        ("#20130C", "#FF9F61"),
        ("#FFFFFF", "#A94427"),
        ("#8ED5B6", "#15161C"),
        ("#FF9B94", "#15161C"),
        ("#FFB477", "#15161C"),
        ("#C4B9F3", "#15161C"),
    ]:
        assert contrast(foreground, background) >= 4.5


@pytest.mark.parametrize("size", [(800, 600), (1200, 800)])
def test_routes_fit_and_commands_have_accessible_names(desktop_window, qtbot, themed, size):
    window = desktop_window()
    window.resize(*size)
    for route in range(4):
        window.navigation.setCurrentRow(route)
        themed.processEvents()
        scroll = window.pages.currentWidget()
        assert isinstance(scroll, QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        for button in scroll.findChildren(QPushButton):
            if button.isVisible():
                assert button.accessibleName()
                assert button.toolTip()
                assert button.width() >= button.minimumSizeHint().width()
        assert window.route_title.text() == ("Home", "Library", "Present", "Settings")[route]
    window.navigation.setCurrentRow(0)
    qtbot.mouseClick(window.home.library_action, Qt.MouseButton.LeftButton)
    assert window.pages.currentIndex() == 1
    window.navigation.setCurrentRow(0)
    qtbot.mouseClick(window.home.present_action, Qt.MouseButton.LeftButton)
    assert window.pages.currentIndex() == 2
    assert not window.present.coordinate_panel.isVisible()
    qtbot.mouseClick(window.present.coordinates_button, Qt.MouseButton.LeftButton)
    assert window.present.coordinate_panel.isVisible()
    assert window.present.coordinates_button.isChecked()
    themed.processEvents()
    assert window.pages.currentWidget().horizontalScrollBar().maximum() == 0
    previous_route = window.pages.currentIndex()
    qtbot.mouseClick(window.account_button, Qt.MouseButton.LeftButton)
    assert window.account_popover.isVisible()
    assert window.pages.currentIndex() == previous_route
    qtbot.keyClick(window.account_popover, Qt.Key.Key_Escape)
    assert not window.account_popover.isVisible()


def test_activity_reduced_motion_visibility_and_persistence(desktop_window, qtbot, themed):
    window = desktop_window()
    mark = window.activity_mark
    window._account_activity(True)
    window._library_activity(True)
    window._account_activity(False)
    assert mark.active and mark.timer.isActive()
    window.preferences.reduce_motion.setChecked(True)
    assert mark.active and not mark.timer.isActive()
    window.settings.sync()
    assert window.settings.value("appearance/reduce_motion", type=bool)
    for child in window.findChildren(BuddyMark):
        assert child.reduced_motion and not child.timer.isActive()
    window.preferences.reduce_motion.setChecked(False)
    assert mark.timer.isActive()
    mark.hide()
    assert not mark.timer.isActive()
    mark.show()
    assert mark.timer.isActive()
    window._library_activity(False)
    assert not mark.active and not mark.timer.isActive()


def test_connection_header_tracks_real_request(desktop_window, qtbot, backend, themed):
    backend.release.clear()
    window = desktop_window(f"http://127.0.0.1:{backend.server_port}", request_timeout=5)
    window.check_connection()
    qtbot.waitUntil(backend.entered.is_set)
    assert window.connection_status.text() == "Checking connection"
    assert window.activity_mark.active and window.home.mascot.active
    window.navigation.setCurrentRow(3)
    assert not window.home.mascot.timer.isActive()
    backend.release.set()
    qtbot.waitUntil(lambda: not window.runner.busy)
    assert window.connection_status.text() == "Backend connected"
    assert not window.activity_mark.active
    window.save_settings("http://127.0.0.1:9000")
    assert window.connection_status.text() == "Connection not checked"


def test_long_endpoint_does_not_widen_settings(desktop_window, themed):
    url = "https://buddy.example/" + "x" * 400
    window = desktop_window(url)
    window.navigation.setCurrentRow(3)
    window.resize(800, 600)
    themed.processEvents()
    themed.processEvents()
    assert window.width() == 800
    assert window.pages.currentWidget().horizontalScrollBar().maximum() == 0
    assert window.preferences.endpoint.toolTip() == url
    assert window.preferences.endpoint.accessibleDescription() == url
    assert len(window.preferences.endpoint.text()) < len(url)


@pytest.mark.parametrize("size", [(800, 600), (1200, 800)])
def test_pdf_controls_fit_new_theme_and_fullscreen(
    desktop_window, qtbot, themed, size, monkeypatch
):
    from pathlib import Path

    from PySide6.QtWidgets import QFileDialog

    fixture = Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
    window = desktop_window()
    window.resize(*size)
    window.navigation.setCurrentRow(2)
    page = window.present
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(fixture), ""))
    page.open_button.click()
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    assert page.executor.manifest is not None, page.feedback.text()
    assert not page.surface.empty.isVisible()
    page.coordinates_button.click()
    for name, value in {"x": 96, "y": 410, "width": 480, "height": 240}.items():
        page.fields[name].setValue(value)
    page.highlight_button.click()
    page.zoom_button.click()
    themed.processEvents()
    assert window.width() == size[0]
    assert window.pages.currentWidget().horizontalScrollBar().maximum() == 0
    for control in (page.open_button, page.fullscreen_button, page.coordinates_button):
        assert control.accessibleName() and control.toolTip() and not control.icon().isNull()
    destination = Path("build")
    destination.mkdir(exist_ok=True)
    assert window.grab().save(str(destination / f"pdf-themed-{size[0]}.png"))
    page.fullscreen_button.click()
    qtbot.waitUntil(
        lambda: page.fullscreen.isFullScreen() and themed.activeWindow() == page.fullscreen
    )
    qtbot.keyClick(page.fullscreen, Qt.Key.Key_Right)
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)
    assert page.counter.text() == "Slide 2 of 3"
    qtbot.keyClick(page.fullscreen, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: page.fullscreen is None)
    assert page.surface.parent() is page
