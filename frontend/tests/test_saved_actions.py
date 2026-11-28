"""Saved-action controls use the existing PDF and native PowerPoint paths (#153)."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from frontend.tests.test_powerpoint_review import powerpoint_payload

from buddy_desktop.powerpoint_contract import (
    PowerPointAction,
    PowerPointActionResult,
    PowerPointError,
    PresentationInfo,
)
from buddy_desktop.powerpoint_generated import SAVED_ACTION_TYPES
from buddy_desktop.powerpoint_present import PowerPointPresent
from buddy_desktop.powerpoint_session_binding import PowerPointSessionBinding
from buddy_desktop.presentation_session_service import PresentationSession, SessionState
from buddy_desktop.review_service import ReviewService, parse_analysis
from buddy_desktop.services import ServiceError


@pytest.fixture(params=["pdf", "pptx"])
def saved_player(request, desktop_window, qtbot, monkeypatch, tmp_path):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    page._agent_tick.stop()
    monkeypatch.setattr(page.session_events, "bind", lambda *args: None)
    monkeypatch.setattr(page, "_ensure_session", lambda **kwargs: None)
    deck, payload = powerpoint_payload()
    analysis = parse_analysis(payload, deck)
    if request.param == "pdf":
        fixture = (
            Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
        )
        page.load_pdf(fixture)
        qtbot.waitUntil(lambda: not page.executor.busy, timeout=20000)
        manifest = page.executor.manifest
        slide = replace(analysis.slides[0], slide_id=manifest.slides[0].slide_id)
        analysis = replace(
            analysis,
            source_format="pdf",
            source_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
            slides=(slide,),
        )
    else:
        content = b"PK-synthetic-source"
        source = tmp_path / "saved.pptx"
        source.write_bytes(content)
        info = PresentationInfo(str(source), source.name, 1, 1, 77, 256, 960, 540)
        binding = PowerPointSessionBinding.create(analysis, content, info)
        page._powerpoint_binding = binding
        page._powerpoint_info = info
        page._powerpoint_verified = True
        page._native_present = PowerPointPresent(binding)
        monkeypatch.setattr(page.powerpoint, "release_zoom", lambda: None)
    page._session = PresentationSession(
        str(uuid4()),
        str(uuid4()),
        str(deck),
        str(analysis.id),
        "active",
        1,
        SessionState(analysis.slides[0].slide_id),
        datetime.now(UTC),
    )
    page._session_access = lambda: "synthetic-owner"
    page._generated_autoload = str(analysis.id)
    responses, reads, saves, native_calls = [analysis], [], [], []

    def read(_service, identifier, source_format):
        reads.append((identifier, source_format))
        return responses[0]

    def save(access, previous, state):
        saves.append(state)
        return replace(previous, state=state, state_version=previous.state_version + 1)

    def execute(command, **kwargs):
        if kwargs.get("commit_guard"):
            assert kwargs["commit_guard"]()
        native_calls.append(command)
        return PowerPointActionResult(command, command.target)

    monkeypatch.setattr(ReviewService, "analysis", read)
    monkeypatch.setattr(page.session_service, "update", save)
    monkeypatch.setattr(page.powerpoint, "execute", execute)
    page._refresh_generated()
    return page, responses, reads, saves, native_calls


def load(player, qtbot):
    page = player[0]
    page.generated_reload.click()
    qtbot.waitUntil(lambda: not page.generated_runner.busy)


def test_load_and_apply_rechecks_current_analysis_and_uses_existing_executor(saved_player, qtbot):
    page, responses, reads, saves, native = saved_player
    load(saved_player, qtbot)
    assert page.generated_actions.count() == 1 and page.generated_apply.isEnabled()
    page.generated_apply.click()
    qtbot.waitUntil(lambda: not page.generated_runner.busy and not page.powerpoint_runner.busy)
    qtbot.waitUntil(lambda: bool(saves))
    assert len(reads) == 2 and all(fmt == responses[0].source_format for _, fmt in reads)
    assert saves[-1].last_accepted_action_id == "action-1"
    if responses[0].source_format == "pptx":
        assert len(native) == 1 and native[0].kind == "highlight_region"
    else:
        assert page.surface.highlight is not None


@pytest.mark.parametrize("state", ["empty", "unsupported", "execution", "stale", "failed"])
def test_unavailable_actions_are_clear_and_never_enabled(saved_player, qtbot, state):
    page, responses, *_ = saved_player
    analysis = responses[0]
    action = analysis.slides[0].actions[0]
    changes = {
        "unsupported": {"action_type": "run_shell"},
        "execution": {"validation_kind": "execution", "semantic_verified": False},
        "stale": {"reasons": ("stale_source",), "usable": False},
        "failed": {"state": "needs-review", "usable": False, "reasons": ("private_code",)},
    }
    actions = () if state == "empty" else (replace(action, **changes[state]),)
    responses[0] = replace(analysis, slides=(replace(analysis.slides[0], actions=actions),))
    load(saved_player, qtbot)
    assert not page.generated_apply.isEnabled()
    assert "No eligible" in page.generated_status.text()
    if actions:
        assert not page.generated_actions.model().item(0).isEnabled()
        assert "private_code" not in page.generated_actions.itemText(0)
        assert "run_shell" not in page.generated_actions.itemText(0)


@pytest.mark.parametrize("change", ["analysis", "source", "deck"])
def test_apply_refuses_changed_analysis_source_or_deck(saved_player, qtbot, change):
    page, responses, _, saves, native = saved_player
    load(saved_player, qtbot)
    changes = {
        "analysis": {"id": uuid4()},
        "source": {"source_sha256": "0" * 64},
        "deck": {"deck_id": uuid4()},
    }
    responses[0] = replace(responses[0], **changes[change])
    page.generated_apply.click()
    qtbot.waitUntil(
        lambda: (
            not page.generated_runner.busy
            and not page.powerpoint_runner.busy
            and not page.session_runner.busy
        )
    )
    assert not native and not saves
    assert "changed" in page.generated_status.text()
    assert not page.generated_apply.isEnabled()


@pytest.mark.parametrize("finish", ["signout", "end", "slide"])
def test_late_result_cannot_apply_after_session_or_slide_change(
    saved_player,
    qtbot,
    monkeypatch,
    finish,
):
    page, responses, reads, saves, native = saved_player
    load(saved_player, qtbot)
    entered, release = Event(), Event()

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return responses[0]

    monkeypatch.setattr(ReviewService, "analysis", delayed)
    page.generated_apply.click()
    try:
        qtbot.waitUntil(entered.is_set)
        if finish == "signout":
            page.clear_session_binding()
        elif finish == "end":
            page._session = replace(page._session, status="ended")
        else:
            monkeypatch.setattr(page, "_current_local_state", lambda: SessionState("other-slide"))
    finally:
        release.set()
    qtbot.waitUntil(
        lambda: (
            not page.generated_runner.busy
            and not page.powerpoint_runner.busy
            and not page.session_runner.busy
        )
    )
    assert not native and not saves


def test_duplicate_load_and_apply_clicks_do_not_queue_more_work(saved_player, qtbot, monkeypatch):
    page, responses, reads, *_ = saved_player
    entered, release = Event(), Event()

    def delayed(*args):
        reads.append(1)
        entered.set()
        assert release.wait(5)
        return responses[0]

    monkeypatch.setattr(ReviewService, "analysis", delayed)
    page.generated_reload.click()
    try:
        qtbot.waitUntil(entered.is_set)
        for _ in range(3):
            page.generated_reload.click()
            page.generated_apply.click()
        assert not page.generated_reload.isEnabled() and not page.generated_apply.isEnabled()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.generated_runner.busy)
    assert len(reads) == 1


def test_failed_load_can_retry_without_disabling_manual_controls(saved_player, qtbot, monkeypatch):
    page, responses, *_ = saved_player
    monkeypatch.setattr(
        ReviewService,
        "analysis",
        lambda *args: (_ for _ in ()).throw(ServiceError("Saved actions unavailable. Retry.")),
    )
    load(saved_player, qtbot)
    assert "Retry" in page.generated_status.text()
    assert page.generated_reload.isEnabled()
    monkeypatch.setattr(ReviewService, "analysis", lambda *args: responses[0])
    load(saved_player, qtbot)
    assert page.generated_apply.isEnabled()


def test_late_failure_after_signout_does_not_replace_cleared_status(
    saved_player,
    qtbot,
    monkeypatch,
):
    page = saved_player[0]
    entered, release = Event(), Event()

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        raise ServiceError("Old account failure")

    monkeypatch.setattr(ReviewService, "analysis", delayed)
    page.generated_reload.click()
    try:
        qtbot.waitUntil(entered.is_set)
        page.clear_session_binding()
        message = page.generated_status.text()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.generated_runner.busy)
    assert page.generated_status.text() == message
    assert page.generated_actions.count() == 0
    assert page._generated_autoload is None


def test_supported_powerpoint_live_types_match_the_native_catalog():
    catalog = json.loads(
        files("buddy_desktop").joinpath("powerpoint_contracts", "action-v2.schema.json").read_text()
    )
    assert set(catalog["properties"]["type"]["enum"]) == SAVED_ACTION_TYPES


def test_native_saved_command_requires_visual_verification(saved_player):
    page, responses, *_ = saved_player
    if responses[0].source_format != "pptx":
        return
    binding = page._powerpoint_binding
    slide = binding.slides[0]
    action = replace(slide.actions[0], semantic_verified=False, validation_kind="execution")
    page._native_present.binding = replace(binding, slides=(replace(slide, actions=(action,)),))
    with pytest.raises(PowerPointError, match="unavailable"):
        page._native_present.saved_command(action.instance_id, page._powerpoint_info)


def test_executor_failure_does_not_persist_an_accepted_action(saved_player, qtbot, monkeypatch):
    page, responses, _, saves, native = saved_player
    load(saved_player, qtbot)
    original_dispatch, original_execute = page.executor.dispatch, page.powerpoint.execute
    if responses[0].source_format == "pdf":
        from buddy_desktop.slide_executor import Completion

        def fail(value, **kwargs):
            page._completed(Completion(1, value["instance_id"], "failed", "Render failed", None))

        monkeypatch.setattr(page.executor, "dispatch", fail)
    else:

        def fail(*args, **kwargs):
            raise PowerPointError("Office is busy. Retry.")

        monkeypatch.setattr(page.powerpoint, "execute", fail)
    page.generated_apply.click()
    qtbot.waitUntil(
        lambda: (
            not page.generated_runner.busy
            and not page.powerpoint_runner.busy
            and not page.session_runner.busy
        )
    )
    assert not saves and page._accepted_action_id is None
    assert "failed" in page.generated_status.text() or "Retry" in page.generated_status.text()
    assert page.generated_reload.isEnabled()
    monkeypatch.setattr(page.executor, "dispatch", original_dispatch)
    monkeypatch.setattr(page.powerpoint, "execute", original_execute)
    if responses[0].source_format == "pdf":
        page.send("next_slide")
        qtbot.waitUntil(lambda: not page.executor.busy)
        assert page.executor.current_index == 1
        page.send("reset_view")
        assert page.surface.highlight is None and page.surface.region == page.surface.slide.full
    else:
        for kind in ("go_to_slide", "reset_view"):
            page._queue_powerpoint_action(
                PowerPointAction(
                    kind, page._powerpoint_info, slide_number=1 if kind == "go_to_slide" else None
                )
            )
            qtbot.waitUntil(lambda: not page.powerpoint_runner.busy)
        assert [command.kind for command in native][-2:] == ["go_to_slide", "reset_view"]
