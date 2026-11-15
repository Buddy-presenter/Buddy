"""Native proposal transport and newest-only scheduling without microphone/provider access."""

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QLabel

from buddy_desktop.agent_loop import AgentLoop
from buddy_desktop.agent_service import AgentService
from buddy_desktop.config import AppConfig
from buddy_desktop.presentation_session_service import PresentationSession, SessionState
from buddy_desktop.services import ServiceError
from buddy_desktop.transcript_context import ContextSegment, ContextSnapshot, TranscriptContext


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
        "expired",
        "partial",
        "retry",
        "capture_expired",
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
            if corrupt == "retry":
                answer["reason"] = "paced"
                answer["retry_after_ms"] = True
            if corrupt == "expired":
                current[0] = replace(context, fresh=False)
            if corrupt == "capture_expired":
                clock[0] += 5_000_000_001
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
        if corrupt is None:
            assert (
                service.propose(
                    "private-token", session, context, current_context=lambda: current[0]
                )["target"]["tool"]
                == "next_slide"
            )
        else:
            with pytest.raises(ServiceError):
                service.propose(
                    "private-token", session, context, current_context=lambda: current[0]
                )
        assert seen[0]["segments"] == [{"sequence": 1, "text": "Our sales are growing."}]
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
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert len(service.calls) == 1
        assert not any(isinstance(value, dict) for value in received)
        clock[0] += 3.3
        loop.update("token", session, current[0])
        qtbot.waitUntil(lambda: not loop.runner.busy)
        assert service.calls[-1].segments[-1].sequence == 9
        assert received[-1] == {"sequence": 9}
        loop.update("token", session, current[0])
        assert len(service.calls) == 2  # Same final is not charged twice.
        current[0] = replace(current[0], epoch=2)
        loop.update("token", session, current[0])
        assert received[-1] is None
        current[0] = replace(current[0], segments=(replace(context.segments[0], final=False),))
        clock[0] += 4
        loop.update("token", session, current[0])
        assert len(service.calls) == 2
        loop.set_enabled(False)
        loop.update("token", session, context)
        assert len(service.calls) == 2
    finally:
        service.release.set()
        loop.wait_for_shutdown()


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
    monkeypatch.setattr(loop_module, "time", SimpleNamespace(monotonic=lambda: clock[0] / 1e9))
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
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

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
