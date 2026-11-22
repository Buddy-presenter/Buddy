"""Floating presenter controls preserve the session only while Mini is visible."""

import ctypes
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QByteArray, QPoint, Qt
from PySide6.QtGui import QGuiApplication
from shiboken6 import isValid

import buddy_desktop.buddy_mini as mini_module


@pytest.mark.parametrize("exit_from", ["fullscreen", "mini", "close"])
@pytest.mark.parametrize("capturing", [False, True])
def test_mini_exit_restores_interactive_presenter(
    desktop_window, qtbot, monkeypatch, exit_from, capturing
):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    pdf = Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
    page.load_pdf(pdf)
    qtbot.waitUntil(lambda: not page.executor.busy and not page._pdf_warming, timeout=10000)
    monkeypatch.setattr(page, "mini_session_id", lambda: "saved-session")
    for _ in range(3):
        window._toggle_mini()
        mini, full = window._mini, page.fullscreen
        assert mini is not None and full is not None
        # Exercise the real stop/status path without opening a physical device.
        page.microphone._enabled = capturing
        if exit_from == "close":
            mini.close()
        else:
            target = full if exit_from == "fullscreen" else mini
            target.activateWindow()
            target.setFocus()
            qtbot.waitUntil(target.isActiveWindow)
            qtbot.keyClick(target, Qt.Key.Key_Escape)
        qtbot.waitUntil(lambda: window.isVisible() and window._mini is None)
        assert page.fullscreen is None
        assert not page.mini_active and not window._mini_poll.isActive()
        assert not page.microphone.enabled
        assert page.body.indexOf(page.surface) >= 0 and page.surface.isVisible()
        # Exercise the returned UI, not just window visibility.
        before = page.executor.current_index
        page.send("next_slide")
        qtbot.waitUntil(lambda: not page.executor.busy)
        assert page.executor.current_index == before + 1
        page.send("previous_slide")
        qtbot.waitUntil(lambda: not page.executor.busy)
        assert page.executor.current_index == before


def test_closing_workspace_with_mini_does_not_restore_it(desktop_window, qtbot, monkeypatch):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    pdf = Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
    page.load_pdf(pdf)
    qtbot.waitUntil(lambda: not page.executor.busy and not page._pdf_warming, timeout=10000)
    monkeypatch.setattr(page, "mini_session_id", lambda: "saved-session")
    window._toggle_mini()
    full = page.fullscreen
    assert full is not None
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible() and window._mini is None)
    qtbot.waitUntil(lambda: not isValid(full))
    assert page.fullscreen is None and not window._mini_poll.isActive()
    assert not window.isVisible()


def test_mini_preserves_pdf_fullscreen_and_restores_on_audience_close(
    desktop_window, qtbot, monkeypatch
):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    pdf = Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
    page.load_pdf(pdf)
    qtbot.waitUntil(lambda: not page.executor.busy)
    monkeypatch.setattr(page, "mini_session_id", lambda: "saved-session")
    page.agent_enabled.setChecked(True)
    page._agent_tick.stop()
    window._toggle_mini()
    mini = window._mini
    assert mini is not None and mini.isWindow() and mini.parent() is None
    assert mini.isVisible() and not window.isVisible()
    assert page.fullscreen is not None and page.fullscreen.isVisible()
    assert page.agent_enabled.isChecked()
    qtbot.mouseClick(mini.mascot, Qt.MouseButton.LeftButton)
    assert all(button.isVisible() and not button.text() for button in mini.buttons)
    window._mini_completed({"target": {"tool": "next_slide"}})
    assert mini._reaction > 0
    page.fullscreen.close()
    assert window.isVisible() and window._mini is None
    assert not page.mini_active and not page.agent_enabled.isChecked()


def test_mini_stays_visible_when_session_drops_and_recovers(desktop_window, monkeypatch):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    page._agent_tick.stop()
    session = "first"
    monkeypatch.setattr(page, "mini_session_id", lambda: session)
    page.fullscreen = SimpleNamespace(screen=window.screen, close=lambda: None)
    window._toggle_mini()
    mini = window._mini
    assert mini is not None
    page.agent_enabled.setChecked(True)
    session = None
    window._sync_mini()
    assert mini.isVisible() and not window.isVisible()
    assert not page.agent_enabled.isChecked() and not mini.buttons[0].isEnabled()
    session = "second"
    window._sync_mini()
    assert mini.isVisible() and window._mini_session == "second"
    assert not page.agent_enabled.isChecked()
    window._leave_mini()
    page.fullscreen = None


def test_mini_can_open_without_a_session(desktop_window):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    window.present.mini_button.click()
    mini = window._mini
    assert mini is not None and mini.isVisible()
    assert "Disconnected" in mini.mascot.accessibleName()
    assert not mini.buttons[0].isEnabled() and not mini.buttons[1].isEnabled()
    mini.close()
    assert window.isVisible()


def test_mini_reappears_after_a_temporary_hide(desktop_window):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    window.present.mini_button.click()
    mini = window._mini
    assert mini is not None
    mini.hide()
    window._sync_mini()
    assert mini.isVisible() and window._mini is mini
    window._leave_mini()


def test_hover_crosses_the_gap_to_radial_controls(desktop_window, monkeypatch):
    window = desktop_window()
    monkeypatch.setattr(window.present, "mini_session_id", lambda: "saved-session")
    window._toggle_mini()
    mini = window._mini
    assert mini is not None
    mini._set_controls(True)
    cursor = mini.mapToGlobal(QPoint(90, 72))
    monkeypatch.setattr(mini_module, "QCursor", SimpleNamespace(pos=lambda: cursor))
    mini._close_if_outside()
    assert all(button.isVisible() for button in mini.buttons)
    cursor = mini.mapToGlobal(QPoint(-30, -30))
    mini._close_if_outside()
    assert not any(button.isVisible() for button in mini.buttons)
    window._leave_mini()


def test_auto_mini_waits_for_the_requested_saved_deck(desktop_window, monkeypatch):
    window = desktop_window()
    launched = []
    monkeypatch.setattr(window, "_toggle_mini", lambda: launched.append(True))
    monkeypatch.setattr(window.present, "mini_session_id", lambda: "active")
    window.preferences.mini_auto_start.setChecked(True)
    window._schedule_auto_mini("new-deck")
    window.present._session = SimpleNamespace(deck_id="old-deck")
    window._maybe_auto_mini()
    assert not launched
    window.present._session = SimpleNamespace(deck_id="new-deck")
    window._maybe_auto_mini()
    assert launched == [True] and not window._mini_auto_poll.isActive()
    window.present._session = None


def test_completion_chime_pauses_capture_until_the_sound_finishes(desktop_window, monkeypatch):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    page._agent_tick.stop()
    monkeypatch.setattr(page, "mini_session_id", lambda: "saved-session")
    page.fullscreen = SimpleNamespace(screen=window.screen, close=lambda: None)
    window._toggle_mini()
    mini = window._mini
    assert mini is not None
    assert not page.mini_chime.isChecked()
    page.mini_chime.setChecked(True)
    monkeypatch.setattr(type(page.microphone), "enabled", property(lambda _: True))
    events = []
    monkeypatch.setattr(page.microphone, "set_paused", events.append)
    monkeypatch.setattr(mini, "prepare_chime", lambda: True)
    monkeypatch.setattr(mini, "play_chime", lambda: events.append("play"))
    window._mini_completed({"target": {"tool": "next_slide"}})
    assert events == [True, "play"]
    mini.chime_finished.emit()
    assert events == [True, "play", False]
    window._leave_mini()
    page.fullscreen = None


def test_mini_automation_icon_toggles_suggestions_and_moves(desktop_window, monkeypatch):
    window = desktop_window()
    page = window.present
    page._agent_tick.stop()
    monkeypatch.setattr(page, "mini_session_id", lambda: "saved-session")
    page.fullscreen = SimpleNamespace(screen=window.screen, close=lambda: None)
    monkeypatch.setattr(page, "_queue_session_state", lambda: None)
    monkeypatch.setattr(page, "_update_agent", lambda: page.policy_enabled.setEnabled(True))
    page.agent_enabled.setEnabled(True)
    window._toggle_mini()
    mini = window._mini
    assert mini is not None
    mini.buttons[1].click()
    assert page.agent_enabled.isChecked() and page.policy_enabled.isChecked()
    mini.buttons[1].click()
    assert not page.agent_enabled.isChecked() and not page.policy_enabled.isChecked()
    window._leave_mini()
    page.fullscreen = None


def test_presentation_access_failure_rechecks_account_without_signing_out(
    desktop_window, monkeypatch
):
    window = desktop_window()
    account = window.account
    account.account_id = "signed-in-account"
    account.session = SimpleNamespace(tokens=object())
    checks = []
    monkeypatch.setattr(account, "start", lambda operation: checks.append(operation) or 1)
    window.present.session_access_expired.emit("Presentation session access ended.")
    window.present.session_access_expired.emit("Presentation session access ended.")
    assert checks == ["check"]
    assert account.account_id == "signed-in-account"
    account.session = None


@pytest.mark.parametrize("power_event", [0x0004, 0x0012, 0x0007])
def test_windows_sleep_restores_workspace_and_stops_capture(
    desktop_window, qtbot, monkeypatch, power_event
):
    window = desktop_window()
    page = window.present
    page._agent_tick.stop()
    monkeypatch.setattr(page, "mini_session_id", lambda: "saved-session")
    fullscreen = SimpleNamespace(screen=window.screen, close=lambda: None)
    page.fullscreen = fullscreen
    window._toggle_mini()
    mini = window._mini
    assert mini is not None and mini.isVisible()
    page.agent_enabled.setChecked(True)
    stopped = []
    monkeypatch.setattr(page.microphone, "stop", lambda: stopped.append(True))

    if QGuiApplication.platformName() == "windows":
        send_message = ctypes.windll.user32.SendMessageW
        send_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        send_message(int(mini.winId()), 0x0218, power_event, 0)
    else:
        message = wintypes.MSG(message=0x0218, wParam=power_event)
        assert mini.nativeEvent(QByteArray(b"windows_generic_MSG"), ctypes.addressof(message)) == (
            True,
            1,
        )

    qtbot.waitUntil(window.isVisible)
    assert stopped and window._mini is None and not window._mini_poll.isActive()
    qtbot.waitUntil(lambda: not isValid(mini))
    assert not page.mini_active and not page.agent_enabled.isChecked()
    assert page.fullscreen is fullscreen
    page.fullscreen = None


def test_live_caption_shows_speech_dots_for_silence_and_hides_when_not_listening():
    caption = mini_module.mini_caption
    assert caption("Mic off", ("hello", 10.0), 10.5) is None
    assert caption("Muted", ("hello", 10.0), 10.5) is None
    assert caption("Listening", None, 10.0) == ""
    assert caption("Listening", ("our revenue", 10.0), 11.0) == "our revenue"
    assert caption("Listening", ("our revenue", 10.0), 14.0) == ""


def test_mini_caption_is_one_short_line_and_lifts_buddy_only_while_shown(qtbot):
    from PySide6.QtWidgets import QPushButton

    mini = mini_module.BuddyMini(QPushButton("Stop microphone"), QPushButton("Undo"))
    qtbot.addWidget(mini)
    mini.set_top_corner(False)
    resting = mini.mascot.y()
    assert mini.caption_rect() is None
    mini.set_caption("")
    assert mini.mascot.y() == resting - mini._caption_lift
    assert mini.caption_text() == "• • •"
    dots = mini.caption_rect()
    assert dots is not None and dots.bottom() < mini.height()
    long = "this year our revenue reached forty eight million dollars across every region"
    mini.set_caption(long)
    shown = mini.caption_text()
    assert long.endswith(shown) and shown != long and "\n" not in shown
    rect = mini.caption_rect()
    assert rect is not None and rect.width() <= mini._caption_max_width
    assert rect.height() == mini._caption_height and rect.top() > mini.mascot.geometry().bottom()
    # The caption stays visible while the hover controls are open.
    mini._set_controls(True)
    assert mini.caption_rect() == rect
    # In a top corner the controls fan out below Buddy, so the caption moves under them.
    mini.set_top_corner(True)
    below = mini.caption_rect()
    assert below is not None and below.bottom() < mini.height()
    assert all(below.top() > button.geometry().bottom() for button in mini.buttons)
    mini.set_top_corner(False)
    mini._set_controls(False)
    mini.set_caption(None)
    assert mini.caption_rect() is None and mini.mascot.y() == resting
