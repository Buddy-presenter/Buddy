from PySide6.QtCore import QSettings, Qt, QTimer

from buddy_desktop.app import resolve_config
from buddy_desktop.config import AppConfig
from buddy_desktop.services import Health
from buddy_desktop.window import MainWindow


def make_window(qtbot, tmp_path, api_url="http://127.0.0.1:8000", *, request_timeout=0.5, **kwargs):
    settings = QSettings(str(tmp_path / "buddy.ini"), QSettings.Format.IniFormat)
    window = MainWindow(AppConfig(api_url, request_timeout), settings, **kwargs)
    qtbot.addWidget(window)
    window.show()
    return window


def test_keyboard_navigation(qtbot, qapp, tmp_path):
    window = make_window(qtbot, tmp_path)
    window.resize(800, 600)
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    window.navigation.setFocus()
    # Activation/focus properties may update before native shortcut events are delivered.
    # Drain pending Qt events before injecting keys, including on the Windows platform.
    qapp.processEvents()
    qtbot.keyClick(window.navigation, Qt.Key.Key_Down)
    assert window.pages.currentIndex() == 1
    qtbot.keyClick(window, Qt.Key.Key_4, modifier=Qt.KeyboardModifier.AltModifier)
    assert window.pages.currentIndex() == 3


def test_settings_persist_and_invalid_input_does_not_replace_config(qtbot, tmp_path, monkeypatch):
    monkeypatch.delenv("BUDDY_API_URL", raising=False)
    window = make_window(qtbot, tmp_path)
    window.save_settings("https://buddy.example/api/")
    assert window.config.api_url == "https://buddy.example/api"
    fresh = QSettings(window.settings.fileName(), QSettings.Format.IniFormat)
    assert (
        resolve_config(AppConfig("http://127.0.0.1:8000"), fresh).api_url == window.config.api_url
    )
    window.save_settings("file:///bad")
    assert window.config.api_url == "https://buddy.example/api"
    assert "HTTP(S)" in window.preferences.feedback.text()


def test_environment_override_disables_local_edits(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDY_API_URL", "https://managed.example")
    window = make_window(qtbot, tmp_path, "https://managed.example", environment_override=True)
    assert not window.preferences.save.isEnabled()
    window.save_settings("https://other.example")
    assert window.config.api_url == "https://managed.example"


def test_invalid_saved_config_falls_back_without_exposing_it(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("BUDDY_API_URL", raising=False)
    settings = QSettings(str(tmp_path / "buddy.ini"), QSettings.Format.IniFormat)
    settings.setValue("connection/api_url", "https://user:secret@example.com")
    config = AppConfig("http://127.0.0.1:8000")
    assert resolve_config(config, settings) == config
    assert "secret" not in caplog.text


def test_real_http_check_keeps_ui_responsive_and_returns_through_signals(qtbot, tmp_path, backend):
    window = make_window(qtbot, tmp_path, f"http://127.0.0.1:{backend.server_port}")
    backend.release.clear()
    ticks = []
    timer = QTimer(window)
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    window.check_connection()
    qtbot.waitUntil(backend.entered.is_set)
    qtbot.waitUntil(lambda: len(ticks) >= 3)
    assert not window.home.check.isEnabled()
    window.navigation.setCurrentRow(3)
    assert window.pages.currentIndex() == 3
    assert window.runner.busy
    backend.release.set()
    qtbot.waitUntil(lambda: not window.runner.busy)
    assert window.home.connection.title.text() == "Backend connected"
    assert window.home.check.isEnabled()


def test_backend_failure_can_be_retried(qtbot, tmp_path, backend):
    window = make_window(qtbot, tmp_path, f"http://127.0.0.1:{backend.server_port}")
    backend.response_code = 404
    window.check_connection()
    qtbot.waitUntil(lambda: not window.runner.busy)
    assert window.home.connection.retry.isVisible()
    backend.response_code = 200
    qtbot.mouseClick(window.home.connection.retry, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window.runner.busy)
    assert window.home.connection.title.text() == "Backend connected"


def test_stale_worker_result_does_not_overwrite_state(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    before = window.home.connection.title.text()
    window._health_succeeded(100, Health("ok"))
    assert window.home.connection.title.text() == before


def test_close_during_request_defers_without_blocking(qtbot, tmp_path, backend):
    # Keep the real request pending while native window events are drained under
    # the full suite; the short timeout is tested separately.
    window = make_window(
        qtbot, tmp_path, f"http://127.0.0.1:{backend.server_port}", request_timeout=5
    )
    backend.release.clear()
    window.check_connection()
    qtbot.waitUntil(backend.entered.is_set)
    assert not window.close()
    assert window.isVisible()
    backend.release.set()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.runner.wait_for_shutdown()
    assert window.settings.value("window/geometry") is not None
