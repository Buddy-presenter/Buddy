"""Strict transport and state contract tests for saved presentation sessions."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.presentation_session_service import (
    PresentationSession,
    PresentationSessionService,
    SessionConflict,
    SessionState,
    parse_snapshot,
)
from buddy_desktop.services import ServiceError


def snapshot(*, version: int = 4) -> dict[str, object]:
    return {
        "request_id": str(uuid4()),
        "data": {
            "id": str(uuid4()),
            "owner_id": str(uuid4()),
            "deck_id": str(uuid4()),
            "analysis_version_id": str(uuid4()),
            "status": "active",
            "state_version": version,
            "state": {
                "active_slide_id": str(uuid4()),
                "focus_view": "slide",
                "focus_region": None,
                "autonomy_mode": "manual",
                "last_accepted_action_id": None,
            },
            "created_at": datetime.now(UTC).isoformat(),
        },
    }


def test_parse_session_snapshot_and_reject_unknown_transcript():
    payload = snapshot()
    parsed = parse_snapshot(payload)
    assert isinstance(parsed, PresentationSession)
    assert parsed.state_version == 4
    assert parsed.state is not None and parsed.state.focus_view == "slide"

    payload["data"]["transcript"] = "must never be accepted"  # type: ignore[index]
    with pytest.raises(ValueError):
        parse_snapshot(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(state_version=True),
        lambda data: data["state"].update(
            focus_view="slide", focus_region={"x": 1, "y": 1, "width": 2, "height": 2}
        ),
        lambda data: data["state"].update(active_slide_id=""),
    ],
)
def test_parse_session_snapshot_rejects_malformed_state(mutate):
    payload = snapshot()
    mutate(payload["data"])
    with pytest.raises((ValueError, TypeError)):
        parse_snapshot(payload)


def test_session_start_uses_bearer_and_strict_response(backend):
    result = snapshot()
    backend.body = json.dumps(result).encode()
    service = PresentationSessionService(AppConfig(f"http://127.0.0.1:{backend.server_port}"))
    deck_id = result["data"]["deck_id"]
    session = service.start("test-access", deck_id)
    assert session.id == result["data"]["id"]
    assert backend.method == "POST"
    assert backend.path == "/api/v1/sessions"
    assert backend.authorization == "Bearer test-access"
    assert json.loads(backend.request_body) == {"deck_id": deck_id}


def test_session_state_update_uses_compare_and_set(backend, monkeypatch):
    raw = snapshot(version=4)
    session = parse_snapshot(raw)
    state = SessionState(active_slide_id=session.state.active_slide_id)
    response = snapshot(version=5)
    response["data"]["id"] = session.id  # type: ignore[index]
    response["data"]["deck_id"] = session.deck_id  # type: ignore[index]
    response["data"]["analysis_version_id"] = session.analysis_version_id  # type: ignore[index]
    response["data"]["state"] = {
        "active_slide_id": state.active_slide_id,
        "focus_view": state.focus_view,
        "focus_region": None,
        "autonomy_mode": state.autonomy_mode,
        "last_accepted_action_id": None,
    }  # type: ignore[index]
    command_id = uuid4()
    monkeypatch.setattr("buddy_desktop.presentation_session_service.uuid4", lambda: command_id)
    backend.body = json.dumps(
        {
            "command_id": str(command_id),
            "sequence": 9,
            "duplicate": False,
            "snapshot": response["data"],
        }
    ).encode()
    service = PresentationSessionService(AppConfig(f"http://127.0.0.1:{backend.server_port}"))
    updated = service.update("test-access", session, state)
    assert updated.state_version == session.state_version + 1
    assert backend.method == "POST"
    assert backend.path == f"/api/v1/sessions/{session.id}/sync/commands"
    payload = json.loads(backend.request_body)
    assert payload["kind"] == "player.state"
    assert payload["command_id"] == str(command_id)
    assert datetime.fromisoformat(payload["sent_at"]).tzinfo is not None
    assert payload["state"]["expected_state_version"] == session.state_version
    assert payload["state"]["active_slide_id"] == state.active_slide_id


def test_session_service_distinguishes_expiration_and_state_conflict(backend):
    service = PresentationSessionService(AppConfig(f"http://127.0.0.1:{backend.server_port}"))
    backend.response_code = 401
    with pytest.raises(LibraryAccessExpired):
        service.request("test-access", "/api/v1/sessions")
    backend.response_code = 409
    with pytest.raises(SessionConflict):
        service.request(
            "test-access",
            "/api/v1/sessions/00000000-0000-0000-0000-000000000001/state",
            method="PATCH",
            body={},
        )
    with pytest.raises(ServiceError) as error:
        service.request("test-access", "/api/v1/sessions", method="POST", body={})
    assert not isinstance(error.value, SessionConflict)
