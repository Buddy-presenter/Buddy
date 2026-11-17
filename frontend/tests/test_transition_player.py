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
        "active_slide_id": manifest.slides[0].slide_id,
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


def test_policy_moves_real_player_without_position_sync_and_undo_restores(player, qtbot):
    page, proposal, _, persisted, checks = player
    observed = []
    page.transition_observed.connect(observed.append)
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy)
    assert len(checks) == 1 and len(observed) == 1
    assert observed[0]["animated"] is False
    # Session writes persist explicit/manual state only; automatic navigation stays local.
    assert all(
        state.active_slide_id == page.executor.manifest.slides[0].slide_id for state in persisted
    )
    assert checks[0][1]["slide_id"] == proposal["target"]["slide_id"]
    assert page.policy_undo_button.isEnabled()
    page.policy_undo_button.click()
    qtbot.waitUntil(lambda: page.executor.current_index == 0 and not page.executor.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy)
    assert not page.policy.enabled


def test_back_to_back_local_moves_use_latest_slide_and_do_not_sync_position(player, qtbot):
    page, proposal, current, persisted, checks = player
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)

    next_proposal = dict(proposal)
    next_proposal.update(
        decision_id=str(uuid4()),
        active_slide_id=page.executor.manifest.slides[1].slide_id,
        sequence=2,
        evidence=[1],
        target={**proposal["target"], "slide_id": page.executor.manifest.slides[2].slide_id},
    )
    current[0] = replace(
        current[0],
        segments=(
            *current[0].segments,
            ContextSegment("b", 2, "Go to the next slide.", True, 2, 3, time.monotonic_ns()),
        ),
        observed_ns=time.monotonic_ns(),
    )
    page._agent_proposed(next_proposal)
    qtbot.waitUntil(lambda: page.executor.current_index == 2 and not page.executor.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy)
    assert len(checks) == 2
    assert checks[0][0].state.active_slide_id == page.executor.manifest.slides[0].slide_id
    assert checks[1][0].state.active_slide_id == page.executor.manifest.slides[1].slide_id
    assert not any(
        state.active_slide_id != page.executor.manifest.slides[0].slide_id for state in persisted
    )


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
    assert page.policy.enabled and page.policy_enabled.isChecked()
    qtbot.waitUntil(lambda: not page.session_runner.busy and page._session_pending_state is None)


def test_powerpoint_without_saved_agent_session_cannot_enable_automation(player, qtbot):
    from buddy_desktop.powerpoint_contract import PresentationInfo

    page, _, _, _, checks = player
    page._powerpoint_info = PresentationInfo("local.pptx", "Local deck", 1, 3)
    page._update_agent()
    assert not page.policy_enabled.isEnabled() and not page.policy.enabled
    assert not checks
    qtbot.waitUntil(lambda: not page.session_runner.busy and page._session_pending_state is None)


def test_presenter_opt_in_stays_checked_during_temporary_session_sync_gap(player):
    page, _, _, _, _ = player
    page._update_agent()
    assert page.policy.enabled and page.policy_enabled.isChecked()
    page._session_pending_state = page._current_local_state()
    page._update_agent()
    assert page.policy.enabled and page.policy_enabled.isChecked()
    assert "automatic actions on" in page.policy_status.text().lower()


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


def test_model_proposal_executes_without_a_calibration_file(player, qtbot):
    page, proposal, current, _, checks = player
    proposal.update(decision_source="model", reason="topic", confidence=0.99)
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy)
    assert len(checks) == 1
    assert page.policy.enabled and page.policy_enabled.isChecked()


def test_navigation_runs_while_position_sync_is_pending(player, qtbot):
    page, proposal, current, persisted, checks = player
    segment = replace(current[0].segments[-1], capture_end_ns=time.monotonic_ns())
    current[0] = replace(current[0], segments=(segment,), observed_ns=segment.capture_end_ns)
    proposal["_client_context"] = current[0]
    page._session_pending_state = page._current_local_state()

    page._agent_proposed(proposal)

    qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy)
    assert page._deferred_agent_proposal is None
    assert len(checks) == 1
    assert page.policy.enabled and page.policy_enabled.isChecked()


def test_preflight_result_is_rechecked_after_transient_sync(player, qtbot, monkeypatch):
    page, proposal, current, persisted, _ = player
    segment = replace(current[0].segments[-1], capture_end_ns=time.monotonic_ns())
    current[0] = replace(current[0], segments=(segment,), observed_ns=segment.capture_end_ns)
    proposal["_client_context"] = current[0]
    entered, release = Event(), Event()
    checks = []

    def delayed_first_check(*args):
        checks.append(args)
        if len(checks) == 1:
            entered.set()
            assert release.wait(5)
        return {}

    monkeypatch.setattr(page.agent.service, "check_transition", delayed_first_check)
    page._agent_proposed(proposal)
    qtbot.waitUntil(entered.is_set)
    page._session_pending_state = page._current_local_state()
    release.set()
    qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy)
    assert page._deferred_transition_check is None
    assert len(checks) == 1
    assert page.policy.enabled and page.policy_enabled.isChecked()


def test_manual_slide_change_during_preflight_rejects_old_absolute_target(
    player, qtbot, monkeypatch
):
    from buddy_desktop.transition_policy import Target

    page, proposal, _, _, _ = player
    target_slide = page.executor.manifest.slides[2].slide_id
    proposal.update(
        decision_source="navigation",
        reason="explicit",
        target={"tool": "go_to_slide", "slide_id": target_slide, "action_id": None, "region": None},
    )
    entered, release = Event(), Event()

    def delayed_check(*_args):
        entered.set()
        assert release.wait(5)
        return {}

    monkeypatch.setattr(page.agent.service, "check_transition", delayed_check)
    page._agent_proposed(proposal)
    qtbot.waitUntil(entered.is_set)

    manual = page._policy_command(Target("next_slide", page.executor.manifest.slides[1].slide_id))
    manual["instance_id"] = "manual:next"
    page.executor.dispatch(manual)
    qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)
    release.set()
    qtbot.waitUntil(lambda: not page.policy_runner.busy)
    assert page.executor.current_index == 1
    assert page.policy.enabled
    assert "state changed" in page.policy_status.text().lower()


def test_numbered_navigation_flows_through_policy_and_executes(player, qtbot):
    page, proposal, _, persisted, checks = player
    target = page.executor.manifest.slides[2].slide_id
    proposal.update(
        decision_source="navigation",
        reason="explicit",
        target={"tool": "go_to_slide", "slide_id": target, "action_id": None, "region": None},
    )
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: page.executor.current_index == 2 and not page.executor.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy)
    assert not any(state.active_slide_id == target for state in persisted)
    assert len(checks) == 1
    assert page.policy.enabled and page.policy_enabled.isChecked()


def test_recoverable_preflight_failure_keeps_automatic_actions_enabled(player):
    from buddy_desktop.transition_policy import Prediction, Target, View

    page, proposal, current, _, _ = player
    prediction = Prediction.from_validated(proposal, current[0])
    manifest = page.executor.manifest
    target = prediction.target
    view = View(
        page._session.id,
        page._session.analysis_version_id,
        manifest.deck_id + ":" + manifest.render_id,
        "pdf",
        page._session.state_version,
        Target("slide", manifest.slides[0].slide_id),
        (target,),
    )
    assert (
        page.policy.consider(
            prediction, current[0], view, now_ns=time.monotonic_ns(), today=datetime.now(UTC).date()
        )
        == "accepted"
    )
    page._policy_check = (prediction, current[0], manifest.render_id)
    page._policy_failed(1, "Temporary preflight failure")
    assert page.policy.enabled and page.policy_enabled.isChecked()
    assert page._policy_check is None


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
    proposal["active_slide_id"] = "slide-1"
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


def test_native_policy_observes_completion_without_position_sync_and_undoes(native_player, qtbot):
    page, proposal, _, persisted, checks, calls, _ = native_player
    observed = []
    page.transition_observed.connect(observed.append)
    page._agent_proposed(proposal)
    qtbot.waitUntil(
        lambda: not page.policy_runner.busy and not page.powerpoint_runner.busy and bool(observed)
    )
    qtbot.waitUntil(lambda: not page.session_runner.busy)
    assert calls == ["next_slide"] and len(checks) == 1
    assert not any(state.active_slide_id == "slide-2" for state in persisted)
    assert observed[0]["source_format"] == "pptx"
    assert observed[0]["animated"] is None  # Office animation has not been measured.
    page.policy_undo_button.click()
    qtbot.waitUntil(lambda: not page.powerpoint_runner.busy and "reset_zoom" in calls)
    qtbot.waitUntil(
        lambda: not page.session_runner.busy and persisted[-1].active_slide_id == "slide-1"
    )
    assert not page.policy.enabled
    assert persisted[-1].autonomy_mode == "manual"


def test_native_model_action_runs_without_a_calibration_file(native_player, qtbot):
    page, proposal, _, persisted, checks, calls, _ = native_player
    proposal.update(decision_source="model", reason="topic", confidence=0.99)
    page._agent_proposed(proposal)
    qtbot.waitUntil(
        lambda: not page.policy_runner.busy and not page.powerpoint_runner.busy and bool(calls)
    )
    qtbot.waitUntil(lambda: not page.session_runner.busy)
    assert calls == ["next_slide"] and len(checks) == 1
    assert page.policy.enabled and page.policy_enabled.isChecked()


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
    qtbot.waitUntil(lambda: not page.session_runner.busy)
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
