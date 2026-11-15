"""Policy-to-player commits, undo, async stale rejection and lifecycle fencing."""

import time
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace
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
    qtbot.waitUntil(lambda: not page.session_runner.busy and page._session_pending_state is None)
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
    qtbot.waitUntil(lambda: not page.session_runner.busy and page._session_pending_state is None)


def test_powerpoint_without_saved_agent_session_cannot_enable_automation(player, qtbot):
    from buddy_desktop.powerpoint_contract import PresentationInfo

    page, _, _, _, checks = player
    page._powerpoint_info = PresentationInfo("local.pptx", "Local deck", 1, 3)
    page._update_agent()
    assert not page.policy_enabled.isEnabled() and not page.policy.enabled
    assert not checks
    qtbot.waitUntil(lambda: not page.session_runner.busy and page._session_pending_state is None)


@pytest.mark.parametrize("fail_at", [1, 2])
def test_guard_exception_fails_closed_with_one_terminal_completion(player, qtbot, fail_at):
    from buddy_desktop.transition_policy import Target

    page, proposal, _, persisted, _ = player
    calls, completions = [], []
    page.executor.completed.connect(completions.append)

    def guard():
        calls.append(True)
        if len(calls) == fail_at:
            raise RuntimeError("private callback details")
        return True

    page.executor.dispatch(
        page._policy_command(Target.parse(proposal["target"])), commit_guard=guard
    )
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.executor.current_index == 0
    assert len(completions) == 1 and completions[0].status == "rejected"
    assert "private callback details" not in completions[0].message
    assert not persisted


def test_provider_failure_breaks_a_model_confirmation_chain(player):
    from buddy_desktop.transition_policy import Calibration

    page, proposal, current, _, checks = player
    page.policy.calibration = Calibration("pdf", 0.9, 200, 200, "a" * 64, date(2099, 1, 1), True)
    proposal.update(decision_source="model", reason="topic", confidence=0.99)
    page._agent_proposed(proposal)
    assert not checks
    page.agent._request_generation = page.agent._generation
    page.agent._failed(0, "Suggestions unavailable")
    segment = replace(current[0].segments[0], segment_id="b", sequence=2)
    current[0] = replace(current[0], segments=(segment,))
    proposal.update(decision_id=str(uuid4()), sequence=2)
    page._agent_proposed(proposal)
    assert not checks and page.executor.current_index == 0


def test_final_partial_agreeing_final_restarts_model_confirmation_before_scheduler_tick(
    player, monkeypatch
):
    from buddy_desktop.transition_policy import Calibration

    page, proposal, current, persisted, checks = player
    page.policy.calibration = Calibration("pdf", 0.9, 200, 200, "a" * 64, date(2099, 1, 1), True)
    proposal.update(decision_source="model", reason="topic", confidence=0.99)
    page._agent_proposed(proposal)
    assert not checks
    # Both events arrive before AgentLoop.update can emit an invalidated proposal.
    # Only the ASR event boundary is substituted; use the real transcript slot/policy.
    monkeypatch.setattr(page.transcript_context, "accept", lambda event: None)
    partial = replace(current[0].segments[0], segment_id="b", sequence=2, final=False)
    current[0] = replace(current[0], segments=(*current[0].segments, partial))
    event = {
        "final": False,
        "start_seconds": 1.0,
        "end_seconds": 2.0,
        "latency_ms": 0,
        "text": "Explain the sales comparison.",
    }
    page._speech_transcript(event)
    # Give the agreeing final enough elapsed persistence time to expose the bug.
    import buddy_desktop.present_widget as present_module

    later = current[0].segments[0].capture_end_ns + 900_000_000
    monkeypatch.setattr(present_module, "time", SimpleNamespace(monotonic_ns=lambda: later))
    final = replace(partial, sequence=3, final=True, capture_end_ns=later)
    current[0] = replace(current[0], segments=(final,))
    page._speech_transcript({**event, "final": True})
    proposal.update(decision_id=str(uuid4()), sequence=3)
    page._agent_proposed(proposal)
    assert not checks and not page.policy_runner.busy
    assert page.executor.current_index == 0 and not persisted
    assert page.policy_status.text() == "Waiting for a sustained, consistent reference."


@pytest.fixture
def native_player(player, tmp_path, monkeypatch):
    from frontend.tests.test_powerpoint_session_binding import example

    from buddy_desktop.powerpoint_contract import PowerPointActionResult
    from buddy_desktop.powerpoint_present import PowerPointPresent
    from buddy_desktop.powerpoint_session_binding import PowerPointSessionBinding

    page, proposal, context, persisted, checks = player
    analysis, content, info = example()
    path = tmp_path / "saved.pptx"
    path.write_bytes(content)
    info = replace(info, path=str(path))
    binding = PowerPointSessionBinding.create(analysis, content, info)
    page._powerpoint_binding = binding
    page._native_present = PowerPointPresent(binding)
    page._powerpoint_info = info
    page._powerpoint_verified = True
    page._session = replace(
        page._session,
        deck_id=binding.deck_id,
        analysis_version_id=binding.analysis_id,
        state=SessionState("slide-1", autonomy_mode="autonomous"),
    )
    proposal["analysis_id"] = binding.analysis_id
    proposal["target"]["slide_id"] = "slide-2"
    calls = []

    def execute(command, **kwargs):
        guard = kwargs.get("commit_guard")
        if guard is not None:
            assert guard()
        calls.append(command.kind)
        index = command.slide_number or (
            2 if command.kind == "next_slide" else command.target.slide_index
        )
        return PowerPointActionResult(
            command, replace(info, slide_index=index, slide_id=100 + index)
        )

    monkeypatch.setattr(page.powerpoint, "execute", execute)
    monkeypatch.setattr(page, "_powerpoint_idle", lambda: None)
    # Do not request a preview/status from real Office after the substituted native boundary.
    monkeypatch.setattr(page._powerpoint_poll, "start", lambda: None)
    return page, proposal, context, persisted, checks, calls, path


def test_native_policy_observes_completion_persists_and_undoes(native_player, qtbot):
    page, proposal, _, persisted, checks, calls, _ = native_player
    observed = []
    page.transition_observed.connect(observed.append)
    page._agent_proposed(proposal)
    qtbot.waitUntil(
        lambda: not page.policy_runner.busy and not page.powerpoint_runner.busy and bool(observed)
    )
    qtbot.waitUntil(lambda: not page.session_runner.busy and bool(persisted))
    assert calls == ["next_slide"] and len(checks) == 1
    assert persisted[-1].active_slide_id == "slide-2"
    assert observed[0]["source_format"] == "pptx"
    assert observed[0]["animated"] is None  # Office animation has not been measured.
    page.policy_undo_button.click()
    qtbot.waitUntil(lambda: not page.powerpoint_runner.busy and "reset_zoom" in calls)
    qtbot.waitUntil(
        lambda: not page.session_runner.busy and persisted[-1].active_slide_id == "slide-1"
    )
    assert not page.policy.enabled
    assert persisted[-1].autonomy_mode == "manual"


@pytest.mark.parametrize("change", ["source", "partial", "manual", "disconnect"])
def test_native_policy_rechecks_delayed_admission(native_player, qtbot, monkeypatch, change):
    page, proposal, context, persisted, _, calls, path = native_player
    entered, release = Event(), Event()

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return {}

    monkeypatch.setattr(page.agent.service, "check_transition", delayed)
    page._agent_proposed(proposal)
    qtbot.waitUntil(entered.is_set)
    try:
        if change == "source":
            path.write_bytes(b"changed")
        elif change == "partial":
            context[0] = replace(context[0], fresh=False)
        elif change == "manual":
            page._native_manual_intent()
        else:
            page._context_connected = False
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.policy_runner.busy and not page.powerpoint_runner.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy and page._session_pending_state is None)
    assert not calls
    assert not any(s.active_slide_id == "slide-2" for s in persisted)


@pytest.mark.parametrize("manual_override", [False, True])
def test_native_undo_waits_for_preview_without_losing_saved_state(
    native_player, qtbot, monkeypatch, manual_override
):
    page, proposal, _, persisted, _, calls, _ = native_player
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: "next_slide" in calls and not page.powerpoint_runner.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy and bool(persisted))
    # This fixture suppresses normal preview dispatch. Restore the production idle
    # handler for this regression and substitute only the external Office preview.
    page._queued_powerpoint_preview = None
    monkeypatch.setattr(page, "_powerpoint_idle", type(page)._powerpoint_idle.__get__(page))
    monkeypatch.setattr(page.powerpoint, "preview", lambda _info: None)
    entered, release = Event(), Event()

    def preview():
        entered.set()
        assert release.wait(5)

    page.powerpoint_runner.submit(preview)
    qtbot.waitUntil(entered.is_set)
    try:
        page.policy_undo_button.click()
        assert page._pending_native_undo and page._policy_undo is not None
        assert not page.policy.enabled
        if manual_override:
            page._native_manual_intent()
    finally:
        release.set()
    if manual_override:
        qtbot.waitUntil(lambda: not page.powerpoint_runner.busy and not page.session_runner.busy)
        assert calls == ["next_slide"]
    else:
        qtbot.waitUntil(lambda: not page.powerpoint_runner.busy and "reset_zoom" in calls)
        qtbot.waitUntil(
            lambda: not page.session_runner.busy and persisted[-1].active_slide_id == "slide-1"
        )
    assert not page._pending_native_undo and page._policy_undo is None
