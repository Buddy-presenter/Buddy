"""Policy-to-player commits, undo, async stale rejection and lifecycle fencing."""

import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from buddy_desktop.presentation_session_service import PresentationSession, SessionState
from buddy_desktop.transcript_context import ContextSegment, ContextSnapshot

FIXTURES = Path(__file__).parent / "fixtures/slide_actions"


@pytest.fixture
def player(desktop_window, qtbot, monkeypatch):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    page._agent_tick.stop()
    page.load_manifest(FIXTURES / "manifest.json")
    qtbot.waitUntil(lambda: not page.executor.busy)
    manifest = page.executor.manifest
    session = PresentationSession(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        "active",
        4,
        SessionState(manifest.slides[0].slide_id, autonomy_mode="autonomous"),
        datetime.now(UTC),
    )
    page._session = session
    page._session_access = "owner"
    page._agent_session_id = session.id
    page._context_connected = True
    page.microphone._enabled = True  # Only the external microphone device boundary is substituted.
    monkeypatch.setattr(page.session_events, "bind", lambda *args: None)
    persisted = []

    def save(access, previous, state):
        assert access == "owner"
        persisted.append(state)
        return replace(previous, state=state, state_version=previous.state_version + 1)

    monkeypatch.setattr(page.session_service, "update", save)
    page.agent_enabled.setChecked(True)
    page.policy_enabled.setChecked(True)
    context = ContextSnapshot(
        session.id,
        4,
        2,
        str(uuid4()),
        "en-US",
        (ContextSegment("a", 1, "Please go to the next slide.", True, 0, 1, time.monotonic_ns()),),
        time.monotonic_ns(),
        True,
    )
    current = [context]
    monkeypatch.setattr(page.transcript_context, "snapshot", lambda: current[0])
    proposal = {
        "decision_id": str(uuid4()),
        "session_id": session.id,
        "analysis_id": session.analysis_version_id,
        "state_version": 4,
        "context_epoch": 2,
        "stream_id": context.stream_id,
        "sequence": 1,
        "confidence": 1.0,
        "decision_source": "navigation",
        "reason": "explicit",
        "evidence": [0],
        "target": {
            "tool": "next_slide",
            "slide_id": manifest.slides[1].slide_id,
            "action_id": None,
            "region": None,
        },
    }
    checks = []

    def approve(access, snapshot, target):
        checks.append((snapshot, target))
        return {
            "session_id": snapshot.id,
            "analysis_id": snapshot.analysis_version_id,
            "state_version": snapshot.state_version,
            "target": target,
        }

    monkeypatch.setattr(page.agent.service, "check_transition", approve)
    return page, proposal, current, persisted, checks


def test_policy_moves_real_player_persists_only_completion_and_undo_restores(player, qtbot):
    page, proposal, _, persisted, checks = player
    observed = []
    page.transition_observed.connect(observed.append)
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy and bool(persisted))
    assert len(checks) == 1 and len(observed) == 1
    assert observed[0]["animated"] is False
    assert persisted[-1].active_slide_id == proposal["target"]["slide_id"]
    assert persisted[-1].autonomy_mode == "autonomous"
    assert page.policy_undo_button.isEnabled()
    page.policy_undo_button.click()
    qtbot.waitUntil(lambda: page.executor.current_index == 0 and not page.executor.busy)
    qtbot.waitUntil(
        lambda: not page.session_runner.busy and persisted[-1].autonomy_mode == "manual"
    )
    assert not page.policy.enabled
    assert persisted[-1].active_slide_id == page.executor.manifest.slides[0].slide_id


@pytest.mark.parametrize("change", ["partial", "disconnect", "signout", "manual", "disable"])
def test_delayed_check_cannot_move_after_context_or_controller_changes(
    player, qtbot, monkeypatch, change
):
    page, proposal, current, persisted, _ = player
    entered, release = Event(), Event()

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return {}

    monkeypatch.setattr(page.agent.service, "check_transition", delayed)
    page._agent_proposed(proposal)
    qtbot.waitUntil(entered.is_set)
    try:
        if change == "partial":
            current[0] = replace(current[0], fresh=False)
        elif change == "disconnect":
            page._context_connected = False
        elif change == "signout":
            page.clear_session_binding()
        elif change == "manual":
            page.send("reset_view")
        else:
            page.policy_enabled.setChecked(False)
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.policy_runner.busy)
    assert page.executor.current_index == 0
    assert not any(s.active_slide_id == proposal["target"]["slide_id"] for s in persisted)


def test_new_partial_while_loading_cannot_commit_or_report_an_applied_action(
    player, qtbot, monkeypatch
):
    import buddy_desktop.slide_executor as executor_module

    page, proposal, current, persisted, _ = player
    entered, release = Event(), Event()
    real_load = executor_module.load_image

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return real_load(*args)

    monkeypatch.setattr(executor_module, "load_image", delayed)
    observed = []
    page.transition_observed.connect(observed.append)
    page._agent_proposed(proposal)
    qtbot.waitUntil(entered.is_set)
    assert not persisted
    current[0] = replace(current[0], fresh=False)
    release.set()
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.executor.current_index == 0 and not observed
    assert not page.policy.enabled


def test_powerpoint_without_saved_agent_session_cannot_enable_automation(player):
    page, _, _, _, checks = player
    page._powerpoint_info = object()
    page._update_agent()
    assert not page.policy_enabled.isEnabled() and not page.policy.enabled
    assert not checks
