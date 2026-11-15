"""Production navigation -> policy -> eligibility HTTP -> Qt player -> PostgreSQL.

Only identity/provider/microphone and authored rendered-slide boundaries are substituted.
"""

import time
from contextlib import suppress
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

import pytest
from backend.tests.api.test_authorization import private_api as private_api
from backend.tests.api.test_authorization import ready_session_api as ready_session_api
from backend.tests.api.test_presentation_agent import agent_api as agent_api
from backend.tests.support.database import database_settings as database_settings

from buddy_desktop.presentation_session_service import parse_snapshot
from buddy_desktop.transcript_context import ContextSegment, ContextSnapshot

FIXTURES = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/slide_actions"


@pytest.mark.postgres
def test_real_policy_player_completion_is_saved_and_undo_is_versioned(
    agent_api, desktop_window, qtbot, monkeypatch
):
    client, ids, provider = agent_api
    headers = {"Authorization": "Bearer owner"}
    path = f"/api/v1/sessions/{ids['sessions']}"
    original = client.get(path, headers=headers).json()["data"]
    opted_in = client.patch(
        path + "/state",
        headers=headers,
        json={
            **original["state"],
            "expected_state_version": 4,
            "autonomy_mode": "autonomous",
        },
    )
    assert opted_in.status_code == 200, opted_in.text
    session = parse_snapshot(opted_in.json())

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            response = client.post(
                self.path,
                content=self.rfile.read(int(self.headers["Content-Length"])),
                headers={k: v for k, v in self.headers.items() if k.lower() != "host"},
            )
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(response.content)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        window = desktop_window(f"http://127.0.0.1:{server.server_port}", request_timeout=5)
        window.navigation.setCurrentRow(3)
        page = window.present
        page._agent_tick.stop()
        page.load_manifest(FIXTURES / "manifest.json")
        qtbot.waitUntil(lambda: not page.executor.busy)
        source = page.executor.manifest
        # Authored PNGs stand in for rendering only. Native slide/session/source IDs
        # and all decision/eligibility/state operations use the real saved contracts.
        slides = tuple(replace(source.slides[0], slide_id=s) for s in ("slide-a", "slide-b"))
        page.executor.manifest = replace(
            source,
            deck_id=session.deck_id,
            render_id="fixture-render-v1",
            slides=slides,
            account_bound=True,
        )
        page.surface.commit(slides[0], page.surface._image, slides[0].full)
        monkeypatch.setattr(page.session_events, "bind", lambda *args: None)
        page._session, page._session_access = session, "owner"
        page._agent_session_id = session.id
        page._context_connected = True
        page.microphone._enabled = True
        page.agent_enabled.setChecked(True)
        page.policy_enabled.setChecked(True)
        context = ContextSnapshot(
            session.id,
            session.state_version,
            1,
            str(uuid4()),
            "en-US",
            (
                ContextSegment(
                    "final", 1, "Please go to the next slide.", True, 0, 1, time.monotonic_ns()
                ),
            ),
            time.monotonic_ns(),
            True,
        )
        monkeypatch.setattr(page.transcript_context, "snapshot", lambda: context)
        proposal = page.agent.service.propose(
            "owner", session, context, current_context=lambda: context
        )
        assert proposal["decision_source"] == "navigation" and not provider.calls
        events = []
        page.transition_observed.connect(events.append)
        page._agent_proposed(proposal)
        qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)
        qtbot.waitUntil(
            lambda: (
                page._session.state_version == session.state_version + 1
                and not page.session_runner.busy
            )
        )
        saved = client.get(path, headers=headers).json()["data"]
        assert saved["state"]["active_slide_id"] == "slide-b"
        assert saved["state"]["autonomy_mode"] == "autonomous"
        assert len(events) == 1 and events[0]["animated"] is False
        page._agent_proposed(proposal)
        assert len(events) == 1  # Same speech/decision cannot apply a second time.
        page.policy_undo_button.click()
        qtbot.waitUntil(lambda: page.executor.current_index == 0 and not page.executor.busy)
        qtbot.waitUntil(
            lambda: (
                page._session.state_version > saved["state_version"]
                and not page.session_runner.busy
            )
        )
        restored = client.get(path, headers=headers).json()["data"]
        assert restored["state"]["active_slide_id"] == "slide-a"
        assert restored["state"]["autonomy_mode"] == "manual"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
