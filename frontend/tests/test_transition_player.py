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
    page._session_access = lambda: "owner"
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
    assert not checks and len(observed) == 1
    assert observed[0]["animated"] is False
    # Session writes persist explicit/manual state only; automatic navigation stays local.
    assert all(
        state.active_slide_id == page.executor.manifest.slides[0].slide_id for state in persisted
    )
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
    assert not checks
    assert not any(
        state.active_slide_id != page.executor.manifest.slides[0].slide_id for state in persisted
    )


@pytest.mark.parametrize("change", ["partial", "disconnect", "signout", "manual", "disable"])
def test_local_navigation_still_obeys_context_and_controller_guards(player, qtbot, change):
    page, proposal, current, persisted, _ = player
    if change == "partial":
        current[0] = replace(current[0], fresh=False)
    elif change == "disconnect":
        page._context_connected = False
    elif change == "signout":
        page.clear_session_binding()
    elif change == "manual":
        current[0] = replace(current[0], fresh=False)
        page._invalidate_transcript_context()
    else:
        page.policy_enabled.setChecked(False)
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: not page.session_runner.busy and page._session_pending_state is None)
    assert page.executor.current_index == 0
    assert not page.policy_runner.busy
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
    page, _, current, _, _ = player
    current[0] = replace(
        current[0],
        segments=(replace(current[0].segments[-1], text="We are reviewing the agenda."),),
    )
    page._update_agent()
    assert page.policy.enabled and page.policy_enabled.isChecked()
    page._session_pending_state = page._current_local_state()
    page._update_agent()
    assert page.policy.enabled and page.policy_enabled.isChecked()
    assert "automatic transitions paused" not in page.policy_status.text().lower()


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
    assert not checks
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
    assert not checks
    assert page.policy.enabled and page.policy_enabled.isChecked()


def test_navigation_bypasses_remote_preflight_during_position_sync(player, qtbot):
    page, proposal, current, persisted, _ = player
    segment = replace(current[0].segments[-1], capture_end_ns=time.monotonic_ns())
    current[0] = replace(current[0], segments=(segment,), observed_ns=segment.capture_end_ns)
    proposal["_client_context"] = current[0]
    page._session_pending_state = page._current_local_state()
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy)
    assert page._deferred_transition_check is None
    assert not page.policy_runner.busy
    assert page.policy.enabled and page.policy_enabled.isChecked()


def test_navigation_checks_local_slide_state_without_remote_preflight(player, qtbot):
    page, proposal, _, _, _ = player
    target_slide = page.executor.manifest.slides[2].slide_id
    proposal.update(
        decision_source="navigation",
        reason="explicit",
        target={"tool": "go_to_slide", "slide_id": target_slide, "action_id": None, "region": None},
    )
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: page.executor.current_index == 2 and not page.executor.busy)
    assert page.policy.enabled
    assert not page.policy_runner.busy


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
    assert not checks
    assert page.policy.enabled and page.policy_enabled.isChecked()


def test_local_navigation_resolves_destination_from_loaded_deck(player):
    page, _, current, _, _ = player
    segment = replace(
        current[0].segments[-1],
        text="Let's go back to the third slide",
        capture_end_ns=time.monotonic_ns(),
    )
    context = replace(current[0], segments=(segment,), observed_ns=time.monotonic_ns())
    proposal = page._local_navigation_proposal(page._session, context)
    assert proposal is not None
    assert proposal["target"] == {
        "tool": "go_to_slide",
        "slide_id": page.executor.manifest.slides[2].slide_id,
        "action_id": None,
        "region": None,
    }


def test_local_navigation_uses_current_player_position_after_prior_move(player, qtbot):
    page, proposal, current, _, checks = player
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)
    assert page._session.state.active_slide_id == page.executor.manifest.slides[0].slide_id

    segment = replace(
        current[0].segments[-1],
        sequence=2,
        text="Go to the next slide",
        capture_end_ns=time.monotonic_ns(),
    )
    context = replace(current[0], segments=(segment,), observed_ns=time.monotonic_ns())
    next_proposal = page._local_navigation_proposal(page._session, context)

    assert next_proposal is not None
    assert next_proposal["active_slide_id"] == page.executor.manifest.slides[1].slide_id
    assert next_proposal["target"]["slide_id"] == page.executor.manifest.slides[2].slide_id
    assert not checks


def saved_analysis(page, **action_changes):
    """A loaded saved analysis with one verified highlight on the first slide."""
    from types import SimpleNamespace

    action = SimpleNamespace(
        instance_id="validated-highlight",
        action_type="highlight_region",
        region=(0.1, 0.1, 0.2, 0.2),
        purpose="Draw attention to the revenue figure",
        usable=True,
        state="validated",
        semantic_verified=True,
        reasons=(),
    )
    for key, value in action_changes.items():
        setattr(action, key, value)
    slide = SimpleNamespace(slide_id=page.executor.manifest.slides[0].slide_id, actions=(action,))
    return SimpleNamespace(
        id=page._session.analysis_version_id,
        deck_id=page._session.deck_id,
        source_format="pdf",
        slides=(slide,),
    )


def highlight_proposal(page, proposal, *, region=(0.1, 0.1, 0.2, 0.2)):
    proposal.update(
        decision_source="model",
        reason="topic",
        confidence=0.99,
        target={
            "tool": "highlight_region",
            "slide_id": page.executor.manifest.slides[0].slide_id,
            "action_id": "validated-highlight",
            "region": dict(zip(("x", "y", "width", "height"), region, strict=True)),
        },
    )


def test_zoom_highlight_retains_server_preflight(player, qtbot, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="buddy_desktop.present_widget")
    page, proposal, _, _, checks = player
    page._generated_analysis = saved_analysis(page)
    highlight_proposal(page, proposal)
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: not page.policy_runner.busy and bool(page.surface.highlight))
    assert len(checks) == 1
    assert checks[0][1]["tool"] == "highlight_region"
    # #202: the executed command is the exact saved, verified action, and the
    # presenter is told which one ran.
    assert page._accepted_action_id == "validated-highlight"
    assert page.policy_status.text() == (
        "Highlighted: Draw attention to the revenue figure. Undo is available."
    )
    # Sanitized evidence: action ID and timing, never purpose or transcript text.
    finished = [m for m in (r.getMessage() for r in caplog.records) if "action_finished" in m]
    assert finished and "action=validated-highlight" in finished[-1]
    assert "revenue" not in caplog.text.lower()


@pytest.mark.parametrize(
    "case",
    ["not_loaded", "not_verified", "not_usable", "region_mismatch", "type_mismatch"],
)
def test_speech_never_executes_an_unverified_or_mismatched_saved_action(player, qtbot, case):
    """#202: action existence alone never executes; the saved action must match."""
    page, proposal, _, _, checks = player
    changes = {
        "not_verified": {"semantic_verified": False},
        "not_usable": {"usable": False},
        "type_mismatch": {"action_type": "zoom_region"},
    }.get(case, {})
    page._generated_analysis = None if case == "not_loaded" else saved_analysis(page, **changes)
    if case == "not_loaded":
        page._generated_autoload = page._session.analysis_version_id  # no reload in test
    region = (0.3, 0.1, 0.2, 0.2) if case == "region_mismatch" else (0.1, 0.1, 0.2, 0.2)
    highlight_proposal(page, proposal, region=region)
    page._agent_proposed(proposal)
    qtbot.wait(300)
    assert not page.surface.highlight and checks == []
    assert page._accepted_action_id is None
    assert (
        "still loading" if case == "not_loaded" else "not verified"
    ) in page.policy_status.text()


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
    assert calls == ["next_slide"] and not checks
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
    assert calls == ["next_slide"] and not checks
    assert page.policy.enabled and page.policy_enabled.isChecked()


@pytest.mark.parametrize("change", ["partial", "manual", "disconnect", "disable"])
def test_native_local_navigation_keeps_speech_and_controller_guards(native_player, qtbot, change):
    page, proposal, context, persisted, checks, calls, _ = native_player
    if change == "partial":
        context[0] = replace(context[0], fresh=False)
    elif change == "manual":
        context[0] = replace(context[0], fresh=False)
        page._native_manual_intent()
    elif change == "disconnect":
        page._context_connected = False
    else:
        page.policy_enabled.setChecked(False)
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: not page.policy_runner.busy and not page.powerpoint_runner.busy)
    qtbot.waitUntil(lambda: not page.session_runner.busy and page._session_pending_state is None)
    assert not calls and not checks
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


def test_dropped_proposals_always_log_and_show_a_reason(player, qtbot, caplog):
    # #187: a valid suggestion that cannot run must never vanish without a reason.
    page, proposal, _, _, _ = player
    manifest = page.executor.manifest
    zoom = {
        **proposal,
        "decision_id": str(uuid4()),
        "decision_source": "agent",
        "reason": "topic",
        "target": {
            "tool": "zoom_region",
            "slide_id": manifest.slides[1].slide_id,
            "action_id": "chart",
            "region": {"x": 0.1, "y": 0.1, "width": 0.3, "height": 0.3},
        },
    }
    with caplog.at_level("WARNING"):
        page._consider_policy(zoom)
    assert f"decision_id={zoom['decision_id']} reason=action_not_on_current_slide" in caplog.text
    assert page.policy_status.text() == "That action is on another slide; go to that slide first."

    caplog.clear()
    page.policy_enabled.setChecked(False)
    assert not page.policy.enabled
    with caplog.at_level("WARNING"):
        page._consider_policy(zoom)
    assert "reason=automation_off" in caplog.text
    assert "automatic actions are off" in page.policy_status.text()

    caplog.clear()
    stay = {**zoom, "target": {**zoom["target"], "tool": "no_action", "region": None}}
    with caplog.at_level("WARNING"):
        page._consider_policy(stay)
    assert "proposal_dropped" not in caplog.text  # Nothing was going to run.
