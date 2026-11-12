"""Actual HTTP/SSE parsing, reconnect cursors, and bounded Qt delivery."""

import io
import json
from contextlib import suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from uuid import uuid4

import pytest
from test_presentation_sessions import snapshot

from buddy_desktop.config import AppConfig
from buddy_desktop.presentation_session_service import parse_snapshot
from buddy_desktop.session_events import (
    MAX_FRAME,
    SessionEventStream,
    parse_event,
    read_frames,
)


def envelope(raw, sequence=0):
    return {
        "protocol": "session-events.v1",
        "session_id": raw["data"]["id"],
        "sequence": sequence,
        "state_version": raw["data"]["state_version"],
        "created_at": datetime.now(UTC).isoformat(),
        "payload": {"kind": "player.state", "snapshot": raw["data"]},
    }


def wire(event):
    return (
        f"id: {event['sequence']}\nevent: session_event\ndata: " + json.dumps(event) + "\n\n"
    ).encode()


@pytest.fixture
def event_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.requests.append(
                (self.path, self.headers.get("Authorization"), self.headers.get("Last-Event-ID"))
            )
            self.send_response(self.server.code)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            body = self.server.respond(len(self.server.requests))
            with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    server.code = 200
    server.respond = lambda _: b""
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_parser_bounds_schema_session_and_frame_size():
    raw = snapshot()
    event = envelope(raw)
    assert parse_event(event, raw["data"]["id"]).snapshot.state_version == 4
    with pytest.raises(ValueError):
        parse_event(event, str(uuid4()))
    with pytest.raises(ValueError):
        parse_event({**event, "sequence": True}, raw["data"]["id"])
    with pytest.raises(ValueError):
        list(read_frames(io.BytesIO(b"data: " + b"x" * MAX_FRAME), Event()))
    with pytest.raises(ValueError):
        parse_event(
            {**event, "payload": {"kind": "execute", "tool": "anything"}}, raw["data"]["id"]
        )


def test_stream_orders_deduplicates_and_waits_for_gui_ack(qtbot, event_server):
    raw = snapshot()
    first = envelope(raw)
    changed = json.loads(json.dumps(first))
    changed["sequence"] = 1
    changed["state_version"] = 5
    changed["payload"]["snapshot"]["state_version"] = 5
    event_server.respond = lambda _: wire(first) + wire(first) + wire(changed)
    stream = SessionEventStream()
    received = []
    stream.received.connect(lambda generation, event: received.append((generation, event)))
    stream.bind(
        AppConfig(f"http://127.0.0.1:{event_server.server_port}"), "test-token", parse_snapshot(raw)
    )
    try:
        qtbot.waitUntil(lambda: len(received) == 1)
        qtbot.wait(30)
        assert len(received) == 1  # Only one queued frame until the GUI acknowledges.
        stream.acknowledge()
        qtbot.waitUntil(lambda: len(received) == 2)
        assert [item[1].sequence for item in received] == [0, 1]
        assert event_server.requests[0][1] == "Bearer test-token"
        assert "test-token" not in event_server.requests[0][0]
    finally:
        stream.stop()
        stream._thread.join(timeout=3)
    assert not stream._thread.is_alive()


def test_reconnect_uses_last_acknowledged_cursor_and_ends(qtbot, event_server):
    raw = snapshot()
    first = envelope(raw)
    final = json.loads(json.dumps(first))
    final["sequence"] = 1
    final["state_version"] = 5
    final["payload"]["snapshot"].update(status="ended", state_version=5)
    event_server.respond = lambda number: wire(first if number == 1 else final)
    stream = SessionEventStream()
    received = []

    def acknowledge(generation, event):
        received.append(event)
        stream.acknowledge()

    stream.received.connect(acknowledge)
    stream.bind(
        AppConfig(f"http://127.0.0.1:{event_server.server_port}"), "test-token", parse_snapshot(raw)
    )
    try:
        qtbot.waitUntil(lambda: len(received) == 2, timeout=5000)
        assert event_server.requests[1][2] == "0"
        assert received[-1].snapshot.status == "ended"
        qtbot.waitUntil(lambda: not stream._thread.is_alive())
    finally:
        stream.stop()
        stream._thread.join(timeout=3)


@pytest.mark.parametrize("code", [401, 403, 404])
def test_stream_access_failure_is_terminal_and_sanitized(qtbot, event_server, code):
    event_server.code = code
    event_server.respond = lambda _: b"private upstream details"
    stream = SessionEventStream()
    errors = []
    stream.failed.connect(lambda *args: errors.append(args))
    stream.bind(
        AppConfig(f"http://127.0.0.1:{event_server.server_port}"),
        "test-token",
        parse_snapshot(snapshot()),
    )
    try:
        qtbot.waitUntil(lambda: len(errors) == 1)
        assert errors[0][2] is True
        assert "private upstream" not in errors[0][1]
        assert len(event_server.requests) == 1
    finally:
        stream.stop()
        stream._thread.join(timeout=3)
