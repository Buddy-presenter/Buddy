"""Exercise real view rendering, queued results and isolated lifecycle helpers."""

from PySide6.QtCore import QTimer


def test_window_render_navigation_and_settings_isolation(desktop_window, qtbot, signal_recorder):
    first = desktop_window()
    changed = signal_recorder(first.pages.currentChanged)
    first.navigation.setCurrentRow(3)
    assert changed()[0] == [3]
    qtbot.waitUntil(lambda: first.preferences.geometry().width() > 0)
    assert not first.grab().isNull()
    assert first.preferences.url.accessibleName() == "Backend URL"
    first.save_settings("https://isolated.example")
    second = desktop_window()
    assert second.config.api_url == "http://127.0.0.1:8000"
    assert first.settings.fileName() != second.settings.fileName()
    assert first.account.session is None
    assert second.account.session is None


def test_service_error_is_delivered_without_blocking_event_loop(
    desktop_window, qtbot, backend, signal_recorder
):
    backend.response_code = 403
    backend.release.clear()
    # The server deliberately waits for GUI ticks (and releases itself after 3s).
    # Keep transport timeout above that bound, even on a busy native Windows runner.
    window = desktop_window(f"http://127.0.0.1:{backend.server_port}", request_timeout=5)
    failure = signal_recorder(window.runner.failed)
    ticks = []
    timer = QTimer(window)
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    window.check_connection()
    qtbot.waitUntil(backend.entered.is_set)
    qtbot.waitUntil(lambda: len(ticks) >= 3)
    backend.release.set()
    assert "denied access" in failure()[0][1]
    qtbot.waitUntil(lambda: not window.runner.busy)
    assert window.preferences.check.isEnabled()
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
