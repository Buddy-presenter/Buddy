"""Native proposal transport and newest-only scheduling without microphone/provider access."""

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QLabel

from buddy_desktop.agent_loop import AgentLoop
from buddy_desktop.agent_service import AgentService, conflict_error
from buddy_desktop.config import AppConfig
from buddy_desktop.presentation_session_service import PresentationSession, SessionState
from buddy_desktop.services import ServiceError
from buddy_desktop.transcript_context import ContextSegment, ContextSnapshot, TranscriptContext


@pytest.mark.parametrize(
    ("code", "visible"),
    [
        ("speech_context_expired", "Speech context expired"),
        ("session_state_changed", "Presentation changed"),
        ("unknown_private_detail", "Operation conflicted"),
    ],
)
def test_conflict_response_is_safe_and_correlated(code, visible):
    reference = str(uuid4())
    headers = Message()
    headers["X-Request-ID"] = reference
    body = json.dumps(
        {
            "error": {"code": code, "message": "private transcript must not be shown"},
            "request_id": reference,
        }
    ).encode()
    error = HTTPError("https://buddy.invalid/agent", 409, "Conflict", headers, BytesIO(body))

    with pytest.raises(ServiceError) as raised:
        raise conflict_error(error)
    assert visible in str(raised.value)
    assert reference in str(raised.value)
    assert "private transcript" not in str(raised.value)


def inputs():
    session = PresentationSession(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        "active",
        3,
        SessionState("slide"),
        datetime.now(UTC),
    )
    now = time.monotonic_ns()
    context = ContextSnapshot(
        session.id,
        3,
        1,
        str(uuid4()),
        "en-US",
        (ContextSegment("a", 1, "Our sales are growing.", True, 0, 0.1, now),),
        now,
        True,
    )
    return session, context


@pytest.mark.parametrize(
    "corrupt",
    [
        None,
        "state",
        "tool",
        "evidence",
        "region",
        "confidence",
        "extra",
        "diagnostics",
        "expired",
        "partial",
        "retry",
        "capture_expired",
        "fallback_action",
        "search",
        "search_unknown",
        "search_with_action",
        "panel",
        "panel_while_closed",
        "panel_and_search",
    ],
)
def test_real_http_validates_proposals_and_context(corrupt, monkeypatch):
    session, context = inputs()
    clock = [context.observed_ns]
    # Server startup/OS trust-store work is not elapsed microphone capture time.
    # Keep this transport fixture deterministic on slower macOS runners.
    monkeypatch.setattr("buddy_desktop.candidate_service.time.monotonic_ns", lambda: clock[0])
    current = [context]
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["Authorization"] == "Bearer private-token"
            seen.append(data)
            answer = {
                "protocol": "presentation-agent.v1",
                "decision_id": str(uuid4()),
                "session_id": session.id,
                "analysis_id": session.analysis_version_id,
                "state_version": 3,
                "active_slide_id": session.state.active_slide_id,
                "context_epoch": 1,
                "stream_id": context.stream_id,
                "sequence": 1,
                "input_sha256": "a" * 64,
                "elapsed_ms": 50,
                "retry_after_ms": None,
                "target": {
                    "tool": "next_slide",
                    "slide_id": "next",
                    "action_id": None,
                    "region": None,
                },
                "reason": "topic",
                "decision_source": "model",
                "confidence": 0.9,
                "evidence": [0],
            }
            if corrupt == "state":
                answer["state_version"] = True
            if corrupt == "tool":
                answer["target"]["tool"] = "shell"
            if corrupt == "region":
                answer["target"]["region"] = {"x": -1}
            if corrupt == "evidence":
                answer["evidence"] = [True]
            if corrupt == "confidence":
                answer["confidence"] = float("nan")
            if corrupt == "extra":
                answer["code"] = "bad"
            if corrupt == "diagnostics":
                answer["diagnostics"] = {"provider_request": {"private": "trace"}}
            if corrupt == "fallback_action":
                answer["decision_source"] = "fallback"
            if corrupt == "retry":
                answer["reason"] = "paced"
                answer["retry_after_ms"] = True
            if corrupt == "expired":
                current[0] = replace(context, fresh=False)
            if corrupt == "capture_expired":
                clock[0] += 5_000_000_001
            if corrupt in {
                "search",
                "search_unknown",
                "panel",
                "panel_while_closed",
                "panel_and_search",
            }:
                # Jev decided to look something up; no slide action.
                answer["target"] = {
                    "tool": "no_action",
                    "slide_id": session.state.active_slide_id,
                    "action_id": None,
                    "region": None,
                }
                answer.update(reason="stay", evidence=[])
            if corrupt == "search":
                answer["search"] = "images"
                # Still valid after the presenter keeps talking.
                current[0] = replace(context, fresh=False)
            if corrupt == "search_unknown":
                answer["search"] = "shell"
            if corrupt == "search_with_action":
                answer["search"] = "web"
            if corrupt in {"panel", "panel_while_closed", "panel_and_search"}:
                answer["panel"] = "scroll_up"
            if corrupt == "panel_and_search":
                answer["search"] = "web"
            if corrupt == "partial":
                current[0] = replace(
                    context,
                    segments=(
                        *context.segments,
                        replace(context.segments[0], sequence=2, final=False),
                    ),
                )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(answer).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        service = AgentService(AppConfig(f"http://127.0.0.1:{server.server_port}"))
        service.search_panel_open = corrupt in {"panel", "panel_and_search"}
        if corrupt in {None, "diagnostics"}:
            proposal = service.propose(
                "private-token", session, context, current_context=lambda: current[0]
            )
            assert proposal["target"]["tool"] == "next_slide"
            assert "diagnostics" not in proposal
        elif corrupt == "search":
            proposal = service.propose(
                "private-token", session, context, current_context=lambda: current[0]
            )
            assert proposal["search"] == "images"
        elif corrupt == "panel":
            proposal = service.propose(
                "private-token", session, context, current_context=lambda: current[0]
            )
            assert proposal["panel"] == "scroll_up"
            assert seen[0]["search_panel_open"] is True
        else:
            with pytest.raises(ServiceError):
                service.propose(
                    "private-token", session, context, current_context=lambda: current[0]
                )
        assert seen[0]["segments"] == [
            {"sequence": 1, "text": "Our sales are growing.", "final": True}
        ]
        assert seen[0]["web_search"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("reason", ["explicit", "topic"])
def test_real_http_accepts_slide_navigation_reasons(monkeypatch, reason):
    session, context = inputs()
    now = [context.observed_ns]
    monkeypatch.setattr("buddy_desktop.candidate_service.time.monotonic_ns", lambda: now[0])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert request_body["focus_view"] == "slide"
            assert request_body["focus_region"] is None
            assert request_body["decision_model"] == "jev"  # Default choice (#245).
            answer = {
                "protocol": "presentation-agent.v1",
                "decision_id": str(uuid4()),
                "session_id": session.id,
                "analysis_id": session.analysis_version_id,
                "state_version": 3,
                "active_slide_id": session.state.active_slide_id,
                "context_epoch": 1,
                "stream_id": context.stream_id,
                "sequence": 1,
                "input_sha256": "a" * 64,
                "elapsed_ms": 1,
                "retry_after_ms": None,
                "target": {
                    "tool": "go_to_slide",
                    "slide_id": "slide-8",
                    "action_id": None,
                    "region": None,
                },
                "reason": reason,
                "decision_source": "navigation",
                "confidence": 1.0,
                "evidence": [0],
            }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(answer).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        service = AgentService(AppConfig(f"http://127.0.0.1:{server.server_port}"))
        answer = service.propose("private-token", session, context, current_context=lambda: context)
        assert answer["decision_source"] == "navigation"
        assert answer["reason"] == reason
        assert answer["target"]["tool"] == "go_to_slide"
        assert answer["target"]["slide_id"] == "slide-8"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Service:
    def __init__(self):
        self.release = threading.Event()
        self.calls = []
        self.prepares = []

    def prepare(self, access, session):
        self.prepares.append(session.id)

    def propose(self, access, session, context, *, current_context):
        self.calls.append(context)
        self.release.wait(2)
        return {"sequence": context.segments[-1].sequence}


def test_bound_local_navigation_callback_does_not_retain_widget_owner():
    import gc
    import weakref

    class Owner:
        def resolve(self, _session, _context):
            return None

    owner = Owner()
    reference = weakref.ref(owner)
    loop = AgentLoop(Service(), local_navigation=owner.resolve)
    del owner
    gc.collect()
    try:
        assert reference() is None
    finally:
        loop.wait_for_shutdown()


def test_loop_coalesces_and_invalidates_corrections_manual_changes_and_disable(qtbot, monkeypatch):
    session, context = inputs()
    clock = [100.0]
    monkeypatch.setattr("buddy_desktop.agent_loop.time.monotonic", lambda: clock[0])
    service = Service()
    current = [context]
    loop = AgentLoop(service, current_context=lambda: current[0])
    received = []
    loop.proposed.connect(received.append)
    try:
        loop.update("token", session, context)
        assert not service.calls and not service.prepares
        loop.set_enabled(True)
        loop.update("token", session, context)
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert service.prepares == [session.id]
        loop.update("token", session, context)
        qtbot.waitUntil(lambda: len(service.calls) == 1)
        # Many newer finals replace pending input; no task queue is created.
        for seq in range(2, 10):
            current[0] = replace(context, segments=(replace(context.segments[0], sequence=seq),))
            loop.update("token", session, current[0])
        service.release.set()
        qtbot.waitUntil(lambda: len(service.calls) == 2 and not loop.runner.busy)
        assert len(service.calls) == 2
        assert service.calls[-1].segments[-1].sequence == 9
        assert received[-1]["sequence"] == 9
        assert [value["sequence"] for value in received if isinstance(value, dict)] == [9]
        loop.update("token", session, current[0])
        assert len(service.calls) == 2
        assert received[-1]["_client_context"] is current[0]
        assert loop._submitted is None
        assert "Our sales are growing" not in repr(loop._last_revision)
        loop.update("token", session, current[0])
        assert len(service.calls) == 2  # Same final is not charged twice.
        current[0] = replace(current[0], epoch=2)
        loop.update("token", session, current[0])
        assert received[-1] is None
        current[0] = replace(current[0], segments=(replace(context.segments[0], final=False),))
        clock[0] += 4
        loop.update("token", session, current[0])
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert len(service.calls) == 3  # New epoch admitted immediately; partial was not sent.
        assert received[-1] is None  # Its now-obsolete completed result was discarded.
        loop.set_enabled(False)
        assert loop._current is None and loop._submitted is None
        loop.update("token", session, context)
        assert len(service.calls) == 3
    finally:
        service.release.set()
        loop.wait_for_shutdown()


def test_loop_submits_stable_interim_and_keeps_result_through_continuation(qtbot):
    session, final = inputs()
    segment = replace(
        final.segments[0],
        final=False,
        text="Let's look at the table",
        stable_text="Let's look at the table",
    )
    partial = replace(final, segments=(segment,))
    current = [partial]
    service = Service()
    loop = AgentLoop(service, current_context=lambda: current[0])
    loop._prepared_for = session.id
    loop.set_enabled(True)
    received = []
    loop.proposed.connect(received.append)
    try:
        loop.update("token", session, partial)
        qtbot.waitUntil(lambda: len(service.calls) == 1)
        assert service.calls[0].decision_segments[-1].text == "Let's look at the table"
        assert not service.calls[0].decision_segments[-1].final
        current[0] = replace(
            partial,
            segments=(
                replace(
                    segment,
                    sequence=2,
                    text="Let's look at the table of contents",
                    stable_text="Let's look at the table of contents",
                ),
            ),
        )
        loop.update("token", session, current[0])
        service.release.set()
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert isinstance(received[-1], dict)
        assert len(service.calls) == 2
        assert received[-1]["_client_context"].decision_segments[-1].text == (
            "Let's look at the table of contents"
        )
    finally:
        service.release.set()
        loop.wait_for_shutdown()


def test_loop_coalesces_latest_stable_word_while_request_is_in_flight(qtbot):
    session, final = inputs()
    initial = "Let's review the market"
    first = replace(final.segments[0], final=False, text=initial, stable_text=initial)
    current = [replace(final, segments=(first,))]
    service = Service()
    loop = AgentLoop(service, current_context=lambda: current[0])
    loop._prepared_for = session.id
    loop.set_enabled(True)
    try:
        loop.update("token", session, current[0])
        qtbot.waitUntil(lambda: len(service.calls) == 1)
        updated = "Let's review the market and then review these numbers"
        current[0] = replace(
            current[0],
            segments=(replace(first, sequence=2, text=updated, stable_text=updated),),
        )
        loop.update("token", session, current[0])
        assert loop._pending_update
        service.release.set()
        qtbot.waitUntil(lambda: len(service.calls) == 2 and not loop.runner.busy)
        assert service.calls[1].decision_segments[-1].text == updated
    finally:
        service.release.set()
        loop.wait_for_shutdown()


def test_local_slide_command_bypasses_slow_contextual_request(qtbot):
    from buddy_desktop.navigation import navigation_intent

    session, context = inputs()
    current = [context]
    service = Service()

    def local_navigation(active_session, snapshot):
        segments = snapshot.decision_segments
        intent = navigation_intent(segments[-1].text) if segments else None
        if intent != ("absolute", 8):
            return None
        return {
            "decision_id": str(uuid4()),
            "session_id": active_session.id,
            "target": {"tool": "go_to_slide", "slide_id": "slide-8"},
        }

    loop = AgentLoop(
        service,
        current_context=lambda: current[0],
        local_navigation=local_navigation,
    )
    loop._prepared_for = session.id
    loop.set_enabled(True)
    received = []
    loop.proposed.connect(received.append)
    try:
        loop.update("token", session, context)
        qtbot.waitUntil(lambda: len(service.calls) == 1)
        newer = replace(
            context,
            segments=(
                replace(
                    context.segments[0],
                    segment_id="b",
                    sequence=2,
                    text="Go to slide 8 and then review the chart",
                    capture_end_ns=time.monotonic_ns(),
                ),
            ),
            observed_ns=time.monotonic_ns(),
        )
        current[0] = newer
        loop.update("token", session, newer)
        assert received[-1]["target"]["tool"] == "go_to_slide"
        assert received[-1]["target"]["slide_id"] == "slide-8"
        assert not loop._pending_update
        service.release.set()
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert len(service.calls) == 1
        assert len(received) == 1
    finally:
        service.release.set()
        loop.wait_for_shutdown()


def test_local_navigation_bypasses_prepare_and_retry_cooldown(qtbot):
    from buddy_desktop.navigation import navigation_intent

    session, context = inputs()
    segment = replace(
        context.segments[-1],
        text="Go to slide 8",
        capture_end_ns=time.monotonic_ns(),
    )
    context = replace(context, segments=(segment,), observed_ns=time.monotonic_ns())
    service = Service()

    def local_navigation(active_session, snapshot):
        if navigation_intent(snapshot.decision_segments[-1].text) != ("absolute", 8):
            return None
        return {
            "decision_id": str(uuid4()),
            "session_id": active_session.id,
            "target": {"tool": "go_to_slide", "slide_id": "slide-8"},
        }

    loop = AgentLoop(service, local_navigation=local_navigation)
    # Simulate initial preparation and a model retry cooldown both being pending.
    loop._not_before = time.monotonic() + 30
    loop.set_enabled(True)
    received = []
    loop.proposed.connect(received.append)
    try:
        loop.update("token", session, context)
        assert received[-1]["target"]["tool"] == "go_to_slide"
        assert received[-1]["target"]["slide_id"] == "slide-8"
        assert not service.prepares
        assert not service.calls
    finally:
        service.release.set()
        loop.wait_for_shutdown()


def test_numbered_navigation_waits_for_destination_then_submits_immediately(qtbot):
    session, final = inputs()
    incomplete = "Go to slide"
    segment = replace(final.segments[0], final=False, text=incomplete, stable_text=incomplete)
    current = [replace(final, segments=(segment,))]
    service = Service()
    loop = AgentLoop(service, current_context=lambda: current[0])
    loop._prepared_for = session.id
    statuses = []
    loop.status.connect(statuses.append)
    loop.set_enabled(True)
    try:
        loop.update("token", session, current[0])
        assert not service.calls
        assert "slide number" in statuses[-1]

        complete = "Go to slide 8"
        current[0] = replace(
            current[0],
            segments=(replace(segment, sequence=2, text=complete, stable_text=complete),),
        )
        loop.update("token", session, current[0])
        qtbot.waitUntil(lambda: len(service.calls) == 1)
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert service.calls[0].decision_segments[-1].text == complete
    finally:
        service.release.set()
        loop.wait_for_shutdown()


def test_loop_discards_interim_result_after_prefix_correction(qtbot):
    session, final = inputs()
    segment = replace(
        final.segments[0],
        final=False,
        text="Let's look at the table",
        stable_text="Let's look at the table",
    )
    partial = replace(final, segments=(segment,))
    current = [partial]
    service = Service()
    loop = AgentLoop(service, current_context=lambda: current[0])
    loop._prepared_for = session.id
    loop.set_enabled(True)
    received = []
    loop.proposed.connect(received.append)
    try:
        loop.update("token", session, partial)
        qtbot.waitUntil(lambda: len(service.calls) == 1)
        current[0] = replace(
            partial,
            segments=(
                replace(
                    segment,
                    sequence=2,
                    text="Let's look at the chart",
                    stable_text="Let's look at the chart",
                ),
            ),
        )
        loop.update("token", session, current[0])
        service.release.set()
        qtbot.waitUntil(lambda: len(service.calls) == 2 and not loop.runner.busy)
        assert received[-1]["_client_context"].decision_segments[-1].text == (
            "Let's look at the chart"
        )
    finally:
        service.release.set()
        loop.wait_for_shutdown()


def test_explicit_interim_correction_bypasses_followup_pacing(qtbot):
    session, final = inputs()
    text = "Let's look at the table"
    segment = replace(final.segments[0], final=False, text=text, stable_text=text)
    current = [replace(final, segments=(segment,))]
    service = Service()
    service.release.set()
    loop = AgentLoop(service, current_context=lambda: current[0])
    loop._prepared_for = session.id
    loop.set_enabled(True)
    try:
        loop.update("token", session, current[0])
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert len(service.calls) == 1

        correction = text + " actually chart"
        current[0] = replace(
            current[0],
            segments=(replace(segment, sequence=2, text=correction, stable_text=correction),),
        )
        assert len(correction) - len(text) < 20
        loop.update("token", session, current[0])
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert len(service.calls) == 2
        assert service.calls[-1].decision_segments[-1].text == correction
    finally:
        loop.wait_for_shutdown()


def test_interim_decision_requests_are_coalesced_and_bounded(qtbot):
    session, final = inputs()
    base = "Let's review this market overview"
    segment = replace(final.segments[0], final=False, text=base, stable_text=base)
    current = [replace(final, segments=(segment,))]
    service = Service()
    service.release.set()
    loop = AgentLoop(service, current_context=lambda: current[0])
    loop._prepared_for = session.id
    loop.set_enabled(True)
    try:
        loop.update("token", session, current[0])
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert len(service.calls) == 1
        for seq, suffix, expected_calls in (
            (2, " with recent growth", 2),
            (3, " with recent growth and customer numbers", 3),
            (
                4,
                " with recent growth and customer numbers we should explain "
                "the whole section fully",
                3,
            ),
            (
                5,
                " with recent growth and customer numbers we should explain "
                "the whole section fully now",
                3,
            ),
        ):
            text = base + suffix
            current[0] = replace(
                current[0],
                segments=(replace(segment, sequence=seq, text=text, stable_text=text),),
            )
            loop.update("token", session, current[0])
            qtbot.waitUntil(lambda: not loop.runner.busy)
            assert len(service.calls) == expected_calls
    finally:
        loop.wait_for_shutdown()


def test_agent_payload_marks_stable_interim_without_changing_candidate_contract():
    from buddy_desktop.candidate_service import completed_payload

    session, final = inputs()
    text = "Let's look at the table of contents"
    segment = replace(final.segments[0], final=False, text=text, stable_text=text)
    partial = replace(final, segments=(segment,))
    payload = completed_payload(session, partial, lambda: partial, allow_provisional=True)
    assert payload["provisional"] is True
    assert payload["latest_utterance"] == text
    assert payload["sequence"] == segment.sequence
    with pytest.raises(ServiceError, match="completed speech"):
        completed_payload(session, partial, lambda: partial)


def test_consumption_rechecks_live_context_before_timer_and_off_on_by_default(
    desktop_window, qtbot
):
    window = desktop_window()
    assert not window.present.agent_enabled.isChecked()
    assert not window.present.agent.enabled
    assert "Cohere" in " ".join(label.text() for label in window.present.findChildren(QLabel))
    session, context = inputs()
    current = [context]
    service = Service()
    service.release.set()
    loop = AgentLoop(service, current_context=lambda: current[0])
    loop._prepared_for = session.id
    received = []
    loop.proposed.connect(received.append)
    loop.set_enabled(True)
    loop.update("token", session, context)
    # Simulate manual action before the next timer publication.
    current[0] = replace(context, epoch=2)
    qtbot.waitUntil(lambda: not loop.runner.busy)
    assert not any(isinstance(value, dict) for value in received)
    loop.wait_for_shutdown()


def test_natural_pause_pronoun_correction_and_topic_change_keep_only_live_work(qtbot, monkeypatch):
    import buddy_desktop.agent_loop as loop_module
    import buddy_desktop.transcript_context as context_module

    session, _ = inputs()
    base = time.monotonic_ns()
    clock = [base]
    monkeypatch.setattr(context_module, "time", SimpleNamespace(monotonic_ns=lambda: clock[0]))
    monkeypatch.setattr(
        loop_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0] / 1e9,
            monotonic_ns=lambda: clock[0],
        ),
    )
    context = TranscriptContext()
    context.bind(session.id, session.state_version, "en-US")
    service = Service()
    service.release.set()
    loop = AgentLoop(service, current_context=context.snapshot)
    loop._prepared_for = session.id
    loop.set_enabled(True)
    stream = str(uuid4())

    def say(seconds, key, sequence, text, *, final=True):
        clock[0] = base + round(seconds * 1e9)
        context.accept(
            {
                "session_id": session.id,
                "stream_id": stream,
                "segment_id": key,
                "sequence": sequence,
                "text": text,
                "final": final,
                "start_seconds": seconds - 1,
                "end_seconds": seconds,
                "capture_end_ns": clock[0],
            }
        )
        loop.update("token", session, context.snapshot())
        qtbot.waitUntil(lambda: not loop.runner.busy)

    try:
        say(2, "intro", 1, "The payment provider sends a confirmation.")
        assert len(service.calls) == 1
        clock[0] = base + 8_000_000_000
        paused = context.snapshot()
        assert not paused.fresh and "confirmation" in paused.text
        loop.update("token", session, paused)
        assert len(service.calls) == 1
        say(9, "reference", 2, "We must verify its signature.")
        assert len(service.calls) == 2
        assert service.calls[-1].text.endswith("confirmation. We must verify its signature.")
        say(9.5, "correction", 3, "Actually, not", final=False)
        assert len(service.calls) == 2
        say(13, "correction", 4, "Actually, I mean refunds, not a completed payment.")
        assert service.calls[-1].candidate_segments[-1].text.startswith("Actually")
        assert service.calls[-1].candidate_segments[-1].sequence == 4
        say(17, "topic", 5, "Now let us turn to cloud budget exhaustion.")
        assert service.calls[-1].candidate_segments[-1].text.startswith("Now let us")
        assert len(service.calls) == 4
        clock[0] = base + 49_000_000_000
        assert not context.snapshot().segments  # Memory has a real time bound.
        say(50, "fresh", 6, "Let us discuss customer retention.")
        assert service.calls[-1].text == "Let us discuss customer retention."
        context.bind(session.id, session.state_version + 1, "en-US")
        loop.update("token", session, context.snapshot())
        assert not context.snapshot().segments
        assert len(service.calls) == 5
    finally:
        loop.wait_for_shutdown()


@pytest.mark.parametrize("expire,always_paced", [(False, False), (True, False), (False, True)])
def test_unspent_admission_is_deferred_without_losing_latest_input(
    qtbot, monkeypatch, expire, always_paced
):
    import buddy_desktop.agent_loop as module

    session, context = inputs()
    clock = [100.0]
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            monotonic_ns=lambda: round(clock[0] * 1e9),
        ),
    )

    class Paced(Service):
        def propose(self, access, session, snapshot, *, current_context):
            self.calls.append(snapshot)
            if always_paced or len(self.calls) == 1:
                return {"reason": "paced", "retry_after_ms": 100}
            return {"reason": "topic", "sequence": snapshot.segments[-1].sequence}

    service = Paced()
    current = [context]
    loop = AgentLoop(service, current_context=lambda: current[0])
    loop._prepared_for = session.id
    loop.set_enabled(True)
    try:
        loop.update("token", session, context)
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert len(service.calls) == 1
        clock[0] += 0.09
        loop.update("token", session, current[0])
        assert len(service.calls) == 1
        if expire:
            current[0] = replace(context, fresh=False)
        clock[0] += 0.02
        loop.update("token", session, current[0])
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert len(service.calls) == (1 if expire else 2)
        if always_paced:
            clock[0] += 0.2
            loop.update("token", session, context)
            qtbot.waitUntil(lambda: not loop.runner.busy)
            assert len(service.calls) == 3
            clock[0] += 10
            loop.update("token", session, context)
            assert len(service.calls) == 3  # At most two unspent admission retries.
    finally:
        loop.wait_for_shutdown()


def test_new_command_does_not_inherit_previous_input_pacing(qtbot, monkeypatch):
    session, context = inputs()
    monkeypatch.setattr("buddy_desktop.agent_loop.time.monotonic", lambda: 100.0)

    class Paced(Service):
        def propose(self, access, session, snapshot, *, current_context):
            self.calls.append(snapshot)
            return {"reason": "paced", "retry_after_ms": 3200}

    service = Paced()
    loop = AgentLoop(service)
    loop._prepared_for = session.id
    loop.set_enabled(True)
    try:
        loop.update("token", session, context)
        qtbot.waitUntil(lambda: not loop.runner.busy)
        loop.update("token", session, context)
        assert len(service.calls) == 1
        command = replace(
            context,
            segments=(
                replace(context.segments[0], sequence=2, text="Let's go to the next slide."),
            ),
        )
        loop.update("token", session, command)
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert len(service.calls) == 2
        assert service.calls[-1].segments[-1].text == "Let's go to the next slide."
    finally:
        loop.wait_for_shutdown()


def test_live_decisions_reuse_one_connection_and_recover_when_it_closes():
    # #237: a new TLS connection per suggestion added about 0.15 s each time.
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.error import HTTPError

    from buddy_desktop.agent_service import KeptConnection

    connections = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            connections.append(self.client_address)

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            status = 409 if self.path.endswith("/conflict") else 200
            body = b'{"ok":true}'
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if self.path.endswith("/close"):
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        kept = KeptConnection(f"http://127.0.0.1:{server.server_port}")
        for _ in range(3):
            assert kept.post("/a", b"{}", {}, timeout=3, limit=100) == b'{"ok":true}'
        assert len(connections) == 1
        kept.post("/close", b"{}", {}, timeout=3, limit=100)
        kept.post("/a", b"{}", {}, timeout=3, limit=100)
        assert len(connections) == 2
        with pytest.raises(HTTPError) as error:
            kept.post("/conflict", b"{}", {}, timeout=3, limit=100)
        assert error.value.code == 409 and error.value.read() == b'{"ok":true}'
        with pytest.raises(ValueError):
            kept.post("/a", b"{}", {}, timeout=3, limit=5)
    finally:
        server.shutdown()
        server.server_close()


def test_region_command_parser_accepts_only_clear_imperatives():
    from buddy_desktop.navigation import region_intent

    assert region_intent("Zoom into the hiring numbers.") == (
        "zoom_region",
        frozenset({"hiring", "number"}),
    )
    assert region_intent("highlight the total") == ("highlight_region", frozenset({"total"}))
    assert region_intent("zoom in") == ("zoom_region", frozenset())
    for text in (
        "Don't zoom into the table",
        "zoom into the table later",
        "we zoomed into the table",
        "Can you zoom into the table?",
        "highlight the total, actually no",
    ):
        assert region_intent(text) is None, text


def test_reset_command_parser_accepts_only_clear_imperatives():
    from buddy_desktop.navigation import reset_intent

    for text in (
        "Remove the highlight",
        "zoom out",
        "Show the whole slide",
        "go back to the full slide",
        "clear the highlight please",
    ):
        assert reset_intent(text), text
    for text in (
        "don't remove the highlight",
        "should we zoom out?",
        "we removed the highlight",
        "next slide",
        "highlight the total",
    ):
        assert not reset_intent(text), text


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("", "https://api.example.com"),
        ("http://127.0.0.1:8765/", "http://127.0.0.1:8765"),
        ("https://agent.example.com", "https://agent.example.com"),
        ("http://evil.example.com", "https://api.example.com"),
        ("http://user@127.0.0.1:8765", "https://api.example.com"),
    ],
)
def test_developer_agent_override_accepts_only_https_or_loopback(monkeypatch, override, expected):
    from buddy_desktop.agent_service import _agent_base

    monkeypatch.setenv("BUDDY_AGENT_URL", override)
    assert _agent_base(AppConfig("https://api.example.com")) == expected
