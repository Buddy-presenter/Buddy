"""Native branded UI checks, independent of a live account or paid providers."""

from importlib.resources import files

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPalette, QPixmap
from PySide6.QtWidgets import QPushButton, QScrollArea

from buddy_desktop.visuals import BuddyMark, apply_theme, icon, mascot_image


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


def test_mascot_edge_is_antialiased_and_is_the_app_icon(desktop_window, themed):
    image = mascot_image(64, 2)
    row = [image.pixelColor(x, 32).alpha() for x in range(64)]
    assert row[0] == 0 and row[32] == 255
    # A smooth disc edge has intermediate coverage, not a hard 0 -> 255 step.
    assert any(0 < alpha < 255 for alpha in row[:8])
    window = desktop_window()
    sizes = window.windowIcon().availableSizes()
    assert QSize(16, 16) in sizes and QSize(256, 256) in sizes


def contrast(foreground, background):
    def luminance(color):
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_applied_palette_text_is_readable(themed):
    palette = themed.palette()

    def ratio(foreground, background):
        return contrast(palette.color(foreground).name(), palette.color(background).name())

    roles = QPalette.ColorRole
    assert ratio(roles.WindowText, roles.Window) >= 7
    assert ratio(roles.Text, roles.Base) >= 7
    assert ratio(roles.HighlightedText, roles.Highlight) >= 4.5
    assert ratio(roles.PlaceholderText, roles.Base) >= 4.5


def test_routes_fit_and_commands_have_accessible_names(desktop_window, qtbot, themed):
    window = desktop_window()
    window.resize(800, 600)  # The minimum window size is the binding layout constraint.
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


def test_loaded_presenter_with_coordinates_fits_minimum_window(
    desktop_window, qtbot, themed, monkeypatch
):
    from pathlib import Path

    from PySide6.QtWidgets import QFileDialog

    fixture = Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
    window = desktop_window()
    window.resize(800, 600)
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
    assert window.width() == 800
    assert window.pages.currentWidget().horizontalScrollBar().maximum() == 0
    assert all(field.isEnabled() for field in page.fields.values())
    for control in (page.open_button, page.fullscreen_button, page.coordinates_button):
        assert control.accessibleName() and control.toolTip() and not control.icon().isNull()
