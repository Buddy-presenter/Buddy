"""Floating presenter controls preserve the session only while Mini is visible."""

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QPoint, Qt

import buddy_desktop.buddy_mini as mini_module


def test_mini_preserves_pdf_fullscreen_and_stops_on_overlay_close(
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
    window._sync_mini()
    assert mini.isVisible() and "Disconnected" in mini.mascot.accessibleName()
    mini.close()
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
