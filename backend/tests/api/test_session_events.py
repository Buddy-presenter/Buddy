"""Real PostgreSQL event ordering, replay, authorization and safe command delivery."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update

from buddy_api import session_events
from buddy_api.auth import CognitoVerifier
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import SessionEvent
from buddy_api.session_events import PlayerCommand, SpeechEvent
from buddy_identity.config import SessionExpired
from tests.api.test_authorization import (
    private_api as private_api,
)
from tests.api.test_authorization import (
    ready_session_api as ready_session_api,
)
from tests.api.test_database import database_settings as database_settings

HEADERS = {"Authorization": "Bearer owner"}


def command(client, path, *, version=None):
    state = client.get(path, headers=HEADERS).json()["data"]
    return {
        "kind": "player.state",
        "command_id": str(uuid4()),
        "sent_at": datetime.now(UTC).isoformat(),
        "state": {
            **state["state"],
            "expected_state_version": (state["state_version"] if version is None else version),
        },
    }


def events(client, path, monkeypatch, cursor=None, headers=None):
    monkeypatch.setattr(session_events, "STREAM_SECONDS", 0.01)
    monkeypatch.setattr(session_events, "POLL_SECONDS", 0.02)
    supplied = dict(HEADERS if headers is None else headers)
    if cursor is not None:
        supplied["Last-Event-ID"] = str(cursor)
    response = client.get(path + "/sync/events", headers=supplied)
    return response, [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def test_typed_events_reject_unbounded_or_forged_commands():
    with pytest.raises(ValidationError):
        PlayerCommand.model_validate(
            {
                "kind": "speech.transcript",
                "command_id": str(uuid4()),
                "sent_at": datetime.now(UTC).isoformat(),
                "state": {},
            }
        )
    with pytest.raises(ValidationError):
        SpeechEvent(
            stream_id=uuid4(),
            segment_id=f"{uuid4()}:1000000",
            revision=0,
            text="s" * 4097,
            final=True,
            language="en",
            start_ms=0,
            end_ms=2,
        )
    with pytest.raises(ValidationError):
        SpeechEvent(
            stream_id=uuid4(),
            segment_id=f"{uuid4()}:1000000",
            revision=0,
            text="speech",
            final=True,
            language="en",
            start_ms=10,
            end_ms=2,
        )


@pytest.mark.postgres
def test_command_acknowledgment_is_durable_and_idempotent(ready_session_api, monkeypatch):
    client, ids, _, _, settings = ready_session_api
    path = f"/api/v1/sessions/{ids['sessions']}"
    body = command(client, path)
    first = client.post(path + "/sync/commands", headers=HEADERS, json=body)
    assert first.status_code == 200, first.text
    ack = first.json()
    assert ack["sequence"] == 1 and ack["duplicate"] is False
    assert ack["snapshot"]["state_version"] == 5

    # A new connection pool sees the committed event and receipt after a restart.
    client.portal.call(client.app.state.database.close)
    client.app.state.database = Database(settings)
    again = client.post(path + "/sync/commands", headers=HEADERS, json=body)
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["snapshot"]["state_version"] == 5
    assert again.json()["sequence"] == ack["sequence"]
    changed = {**body, "state": {**body["state"], "autonomy_mode": "assisted"}}
    assert client.post(path + "/sync/commands", headers=HEADERS, json=changed).status_code == 409
    assert (
        client.post(
            path + "/sync/commands", headers=HEADERS, json=command(client, path, version=4)
        ).status_code
        == 409
    )
    stale = command(client, path)
    stale["sent_at"] = (datetime.now(UTC) - timedelta(seconds=20)).isoformat()
    assert client.post(path + "/sync/commands", headers=HEADERS, json=stale).status_code == 409
    response, replay = events(client, path, monkeypatch, cursor=0)
    assert response.status_code == 200
    assert [event["sequence"] for event in replay] == [1]
    assert replay[0]["payload"]["snapshot"]["state_version"] == 5


@pytest.mark.postgres
def test_concurrent_duplicate_commands_apply_once(ready_session_api):
    client, ids, *_ = ready_session_api
    path = f"/api/v1/sessions/{ids['sessions']}"
    body = command(client, path)

    def send(_):
        return client.post(path + "/sync/commands", headers=HEADERS, json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()["duplicate"] for response in responses) == [False, True]
    assert client.get(path, headers=HEADERS).json()["data"]["state_version"] == 5


@pytest.mark.postgres
def test_rest_changes_are_ordered_and_reconnect_does_not_repeat(ready_session_api, monkeypatch):
    client, ids, *_ = ready_session_api
    path = f"/api/v1/sessions/{ids['sessions']}"
    for _ in range(3):
        body = command(client, path)
        assert client.patch(path + "/state", headers=HEADERS, json=body["state"]).status_code == 200
    _, replay = events(client, path, monkeypatch, cursor=1)
    assert [event["sequence"] for event in replay] == [2, 3]
    assert [event["state_version"] for event in replay] == [6, 7]
    _, replay = events(client, path, monkeypatch, cursor=3)
    assert replay == []
    assert events(client, path, monkeypatch, cursor=4)[0].status_code == 409
    assert events(client, path, monkeypatch, cursor=-1)[0].status_code == 422

    async def expire():
        async with client.app.state.database.sessions() as db, db.begin():
            await db.execute(
                update(SessionEvent).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )

    client.portal.call(expire)
    _, restored = events(client, path, monkeypatch, cursor=0)
    assert len(restored) == 1
    assert restored[0]["sequence"] == 3
    assert restored[0]["payload"]["snapshot"]["state_version"] == 7
    client.portal.call(session_events.prune_expired_events, client.app.state.database)

    async def retained_count():
        async with client.app.state.database.sessions() as db:
            return await db.scalar(select(func.count()).select_from(SessionEvent))

    assert client.portal.call(retained_count) == 0


@pytest.mark.postgres
def test_bounded_journal_and_slow_consumer_restore_snapshot(ready_session_api, monkeypatch):
    client, ids, *_ = ready_session_api
    path = f"/api/v1/sessions/{ids['sessions']}"
    monkeypatch.setattr(session_events, "RETAIN_EVENTS", 4)
    for _ in range(7):
        body = command(client, path)
        assert client.post(path + "/sync/commands", headers=HEADERS, json=body).status_code == 200

    async def count():
        async with client.app.state.database.sessions() as db:
            return await db.scalar(select(func.count()).select_from(SessionEvent))

    assert client.portal.call(count) == 4
    _, restored = events(client, path, monkeypatch, cursor=0)
    assert [event["sequence"] for event in restored] == [7]
    assert restored[0]["payload"]["snapshot"]["state_version"] == 11


@pytest.mark.postgres
def test_events_deny_other_accounts_forgery_end_and_revocation(ready_session_api, monkeypatch):
    client, ids, *_ = ready_session_api
    path = f"/api/v1/sessions/{ids['sessions']}"
    other = {"Authorization": "Bearer other"}
    assert events(client, path, monkeypatch, headers=other)[0].status_code == 403
    assert events(client, path, monkeypatch, headers={})[0].status_code == 401
    body = command(client, path)
    assert client.post(path + "/sync/commands", headers=other, json=body).status_code == 403
    body["kind"] = "decision.status"
    assert client.post(path + "/sync/commands", headers=HEADERS, json=body).status_code == 422
    body = command(client, path)
    ended = client.post(path + "/end", headers=HEADERS, json={"expected_state_version": 4})
    assert ended.status_code == 200
    assert client.post(path + "/sync/commands", headers=HEADERS, json=body).status_code == 409
    _, final = events(client, path, monkeypatch)
    assert final[0]["payload"]["snapshot"]["status"] == "ended"

    def revoked(self, token):
        raise SessionExpired("private provider error")

    monkeypatch.setattr(CognitoVerifier, "verify", revoked)
    response, _ = events(client, path, monkeypatch)
    assert response.status_code == 401
    assert "private provider error" not in response.text


@pytest.mark.postgres
def test_internal_speech_events_are_bound_to_current_session_and_not_logged(
    ready_session_api, monkeypatch, caplog
):
    from starlette.exceptions import HTTPException

    from buddy_api.auth import Principal

    client, ids, owner, *_ = ready_session_api
    path = f"/api/v1/sessions/{ids['sessions']}"
    speech = SpeechEvent(
        stream_id=uuid4(),
        segment_id=f"{uuid4()}:1000000",
        revision=0,
        text="private spoken material",
        final=True,
        language="en",
        start_ms=0,
        end_ms=20,
    )

    async def publish(version):
        async with client.app.state.database.sessions() as db, db.begin():
            return await session_events.publish_server_event(
                db, Principal(owner, "owner@example.test"), ids["sessions"], version, speech
            )

    with pytest.raises(HTTPException) as exc:
        client.portal.call(publish, 3)
    assert exc.value.status_code == 409
    event = client.portal.call(publish, 4)
    assert event.sequence == 1
    _, replay = events(client, path, monkeypatch, cursor=0)
    assert replay[0]["payload"]["text"] == speech.text
    assert speech.text not in caplog.text
    assert client.get(path, headers=HEADERS).json()["data"]["state_version"] == 4
    ended = client.post(path + "/end", headers=HEADERS, json={"expected_state_version": 4})
    assert ended.status_code == 200

    async def retained():
        async with client.app.state.database.sessions() as db:
            return [row.document for row in (await db.scalars(select(SessionEvent))).all()]

    documents = client.portal.call(retained)
    assert len(documents) == 1
    assert documents[0]["payload"]["kind"] == "player.state"
    assert speech.text not in json.dumps(documents)


def test_command_size_limit_precedes_json_parsing():
    from fastapi.testclient import TestClient

    from buddy_api.app import create_app
    from buddy_api.config import Environment, Settings

    with TestClient(create_app(Settings(Environment.TEST, ("testserver",)))) as client:
        response = client.post(
            f"/api/v1/sessions/{uuid4()}/sync/commands",
            content=b"x" * 8193,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
