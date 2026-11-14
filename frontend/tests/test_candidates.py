"""Native candidate protocol validation over a real local HTTP server."""

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from buddy_desktop.candidate_service import CandidateService
from buddy_desktop.config import AppConfig
from buddy_desktop.presentation_session_service import PresentationSession, SessionState
from buddy_desktop.services import ServiceError
from buddy_desktop.transcript_context import ContextSegment, ContextSnapshot


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
        (
            ContextSegment(
                "a", 1, "private speech", True, 0, 0.1, now - 500_000_000, now - 250_000_000
            ),
        ),
        now,
        True,
    )
    return session, context


@pytest.mark.parametrize(
    "corrupt", [None, "state_version", "tool", "action", "oversize", "expired", "window_expired"]
)
def test_response_is_bound_to_context_and_only_typed_candidates(corrupt, monkeypatch):
    session, context = inputs()
    if corrupt == "window_expired":
        context = replace(context, segments=(replace(context.segments[0], end_seconds=29),))
    # Earlier speech must remain available for topics while the last segment
    # is sent separately for whole-utterance navigation matching.
    if corrupt != "window_expired":
        context = replace(
            context,
            segments=(
                replace(
                    context.segments[0],
                    text="revenue growth",
                    capture_end_ns=context.observed_ns - 500_000_000,
                ),
                replace(
                    context.segments[0],
                    segment_id="b",
                    sequence=2,
                    text="next slide",
                    start_seconds=0.5,
                    end_seconds=0.6,
                ),
            ),
        )
    clock = [context.observed_ns]
    monkeypatch.setattr("buddy_desktop.candidate_service.time.monotonic_ns", lambda: clock[0])
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers.get("Authorization"), data))
            result = {
                "protocol": data["protocol"],
                "resolver_version": "summary-overlap-1",
                "session_id": session.id,
                "analysis_id": session.analysis_version_id,
                "state_version": 3,
                "context_epoch": data["context_epoch"],
                "stream_id": data["stream_id"],
                "sequence": data["sequence"],
                "input_sha256": "a" * 64,
                "candidates": [
                    {
                        "kind": "no_change",
                        "slide_id": "slide",
                        "action_id": None,
                        "reason": "stay",
                        "score": 0,
                    }
                ],
            }
            if corrupt == "state_version":
                result["state_version"] = 2
            elif corrupt == "tool":
                result["candidates"][0]["kind"] = "shell"
            elif corrupt == "action":
                result["candidates"][0]["action_id"] = "arbitrary-action"
            raw = b"x" * 32769 if corrupt == "oversize" else json.dumps(result).encode()
            if corrupt == "expired":
                clock[0] += 6_000_000_000
            elif corrupt == "window_expired":
                clock[0] += 2_000_000_000
            self.send_response(200)
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = CandidateService(AppConfig(f"http://127.0.0.1:{server.server_port}"))
        if corrupt:
            with pytest.raises(ServiceError):
                client.resolve("token", session, context)
        else:
            assert client.resolve("token", session, context)["candidates"][0]["kind"] == "no_change"
        assert seen[0][0] == f"/api/v1/sessions/{session.id}/candidates"
        assert seen[0][1] == "Bearer token"
        assert seen[0][2]["text"] == context.text
        assert seen[0][2]["latest_utterance"] == context.segments[-1].text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_stale_context_rejected_before_network():
    session, context = inputs()
    client = CandidateService(AppConfig("http://127.0.0.1:1"))
    for stale in (
        replace(context, fresh=False),
        replace(context, state_version=2),
        replace(context, observed_ns=context.observed_ns - 2_000_000_000),
    ):
        with pytest.raises(ServiceError, match="changed or expired"):
            client.resolve("token", session, stale)


def test_partial_and_unsettled_final_do_not_make_network_requests(monkeypatch):
    session, context = inputs()
    now = context.observed_ns
    monkeypatch.setattr("buddy_desktop.candidate_service.time.monotonic_ns", lambda: now)
    client = CandidateService(AppConfig("http://127.0.0.1:1"))
    partial = replace(context, segments=(replace(context.segments[0], final=False),))
    with pytest.raises(ServiceError, match="completed"):
        client.resolve("token", session, partial)
    unsettled = replace(
        context, segments=(replace(context.segments[0], received_ns=now - 100_000_000),)
    )
    with pytest.raises(ServiceError, match="settle"):
        client.resolve("token", session, unsettled)
