"""Real persisted owner/cross-account boundaries with deterministic identity responses."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.exceptions import HTTPException

from buddy_api import authorization
from buddy_api.app import create_app
from buddy_api.auth import CognitoVerifier, Principal, VerifiedIdentity
from buddy_api.config import Environment, Settings
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import (
    AnalysisSlide,
    AnalysisVersion,
    Deck,
    PresentationSession,
    ProcessingJob,
    RenderedAsset,
)
from buddy_api.presentation_sessions import current_ready_analysis
from buddy_identity.config import IdentityConfig, SessionExpired
from tests.api.test_database import database_settings as database_settings
from tests.api.test_database import migrate

CONFIG = IdentityConfig(
    "eu-central-1",
    "eu-central-1_Example",
    "client123",
    "https://buddy.auth.eu-central-1.amazoncognito.com",
)


@pytest.fixture
def private_api(database_settings, monkeypatch):
    asyncio.run(migrate(database_settings))
    database = Database(database_settings)

    async def seed():
        async with database.transaction() as repos:
            owner = await repos.users.create("owner")
            other = await repos.users.create("other")
            deck = await repos.decks.create(
                owner_id=owner.id, title="private", source_key="secret-key"
            )
            job = await repos.jobs.create(owner_id=owner.id, deck_id=deck.id)
            from backend.tests.support.analysis import create

            analysis = await create(
                repos, owner_id=owner.id, deck_id=deck.id, job_id=job.id, version=1
            )
            presentation = await repos.sessions.create(
                owner_id=owner.id, deck_id=deck.id, analysis_version_id=analysis.id
            )
        async with database.sessions() as session, session.begin():
            asset = RenderedAsset(
                owner_id=owner.id,
                deck_id=deck.id,
                storage_key="secret-render",
                content_type="image/png",
                size_bytes=23,
            )
            session.add(asset)
            await session.flush()
        await database.close()
        return (
            owner.id,
            other.id,
            {
                "decks": deck.id,
                "analyses": analysis.id,
                "jobs": job.id,
                "sessions": presentation.id,
                "assets": asset.id,
            },
        )

    owner, other, identifiers = asyncio.run(seed())

    def verify(self, token):
        if token not in ("owner", "other"):
            raise SessionExpired("revoked")
        return VerifiedIdentity(token, f"{token}@example.test")

    monkeypatch.setattr(CognitoVerifier, "verify", verify)
    with TestClient(
        create_app(
            Settings(Environment.TEST, ("testserver",), database=database_settings, identity=CONFIG)
        )
    ) as client:
        yield client, identifiers, owner, other


@pytest.mark.postgres
@pytest.mark.parametrize("kind", ["decks", "analyses", "assets", "sessions", "jobs"])
def test_each_resource_denies_cross_account_reads_writes_and_deletes(private_api, kind):
    client, identifiers, _, _ = private_api
    path = f"/api/v1/resources/{kind}/{identifiers[kind]}"
    for method, kwargs in [("get", {}), ("patch", {"json": {"title": "stolen"}}), ("delete", {})]:
        response = getattr(client, method)(
            path, headers={"Authorization": "Bearer other"}, **kwargs
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        assert "private" not in response.text and "secret" not in response.text
    response = client.get(path, headers={"Authorization": "Bearer owner"})
    assert response.status_code == 200
    assert "secret-key" not in response.text and "secret-render" not in response.text


@pytest.mark.postgres
def test_verified_owner_writes_persist_and_no_owner_override(private_api):
    client, ids, _, other = private_api
    path = f"/api/v1/resources/decks/{ids['decks']}"
    headers = {"Authorization": "Bearer owner"}
    assert (
        client.patch(
            path, headers=headers, json={"title": "renamed", "owner_id": str(other)}
        ).status_code
        == 422
    )
    assert client.patch(path, headers=headers, json={"title": "renamed"}).status_code == 200
    assert client.get(path, headers=headers).json()["data"]["title"] == "renamed"
    assert client.delete(path, headers=headers).status_code == 409
    session_path = f"/api/v1/resources/sessions/{ids['sessions']}"
    assert (
        client.patch(
            session_path, headers=headers, json={"status": "active", "state_version": 0}
        ).status_code
        == 403
    )


@pytest.fixture
def ready_session_api(private_api, database_settings):
    client, identifiers, owner, other = private_api
    database = client.app.state.database

    async def prepare():
        async with database.sessions() as db, db.begin():
            record = await db.get(PresentationSession, identifiers["sessions"])
            deck = await db.get(Deck, identifiers["decks"])
            analysis = await db.get(AnalysisVersion, record.analysis_version_id)
            job = await db.get(ProcessingJob, analysis.job_id)
            slides = list(
                (
                    await db.scalars(
                        select(AnalysisSlide)
                        .where(AnalysisSlide.analysis_id == analysis.id)
                        .order_by(AnalysisSlide.position)
                    )
                ).all()
            )
            revision = (analysis.provenance or {})["source_revision"]
            digest = (analysis.provenance or {})["source_sha256"]
            deck.source_key = "ready-source"
            deck.source_revision = revision
            deck.source_sha256 = digest
            deck.source_size_bytes = 1024
            deck.source_version_id = "version-1"
            deck.source_slide_count = len(slides)
            job.status = "succeeded"
            job.source_revision = revision
            analysis.publication = "published"
            record.status = "active"
            record.state_version = 4
            record.active_slide_id = slides[0].slide_id

    client.portal.call(prepare)
    return client, identifiers, owner, other, database_settings


@pytest.mark.postgres
def test_session_lifecycle_is_durable_owner_scoped_and_versioned(ready_session_api):
    client, ids, owner_id, _, database_settings = ready_session_api
    headers = {"Authorization": "Bearer owner"}
    path = "/api/v1/sessions"
    deck_path = f"{path}"
    started = client.post(deck_path, headers=headers, json={"deck_id": str(ids["decks"])})
    assert started.status_code == 200
    first = started.json()["data"]
    assert first["status"] == "active"
    assert first["state_version"] == 4
    assert first["state"]["focus_view"] == "slide"
    assert first["state"]["autonomy_mode"] == "manual"
    assert "transcript" not in first["state"]

    duplicate = client.post(deck_path, headers=headers, json={"deck_id": str(ids["decks"])})
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["id"] == first["id"]
    assert duplicate.json()["data"]["state_version"] == first["state_version"]

    state_path = f"{path}/{first['id']}/state"
    region = {"x": 20, "y": 30, "width": 300, "height": 180}
    changed = client.patch(
        state_path,
        headers=headers,
        json={
            "expected_state_version": 4,
            "active_slide_id": first["state"]["active_slide_id"],
            "focus_view": "zoom_region",
            "focus_region": region,
            "autonomy_mode": "assisted",
            "last_accepted_action_id": None,
        },
    )
    assert changed.status_code == 200
    assert changed.json()["data"]["state_version"] == 5
    assert changed.json()["data"]["state"]["focus_region"] == region

    stale = client.patch(
        state_path,
        headers=headers,
        json={
            "expected_state_version": 4,
            "active_slide_id": first["state"]["active_slide_id"],
            "focus_view": "slide",
            "focus_region": None,
            "autonomy_mode": "manual",
            "last_accepted_action_id": None,
        },
    )
    assert stale.status_code == 409
    resumed = client.post(f"{path}/{first['id']}/resume", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["data"]["state_version"] == 5
    read = client.get(f"{path}/{first['id']}", headers=headers)
    assert read.status_code == 200
    assert read.json()["data"]["state"]["focus_region"] == region

    # A fresh database engine sees the same state, like a service restart.
    previous_database = client.app.state.database
    client.portal.call(previous_database.close)
    client.app.state.database = Database(database_settings)
    restored = client.get(f"{path}/{first['id']}", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["data"]["state_version"] == 5
    ended = client.post(
        f"{path}/{first['id']}/end",
        headers=headers,
        json={"expected_state_version": 5},
    )
    assert ended.status_code == 200
    assert ended.json()["data"]["status"] == "ended"
    assert ended.json()["data"]["state_version"] == 6
    assert client.post(f"{path}/{first['id']}/resume", headers=headers).status_code == 409
    retained = client.get(f"{path}/{first['id']}", headers=headers)
    assert retained.status_code == 200
    assert retained.json()["data"]["status"] == "ended"

    async def verify_ready_after_end():
        async with client.app.state.database.sessions() as db:
            deck = await db.get(Deck, ids["decks"])
            assert deck is not None
            analysis, slides = await current_ready_analysis(db, deck, owner_id)
            return str(analysis.id), len(slides)

    # Ending one session does not invalidate the ready analysis needed by a new session.
    assert client.portal.call(verify_ready_after_end) == (first["analysis_version_id"], 2)
    restarted = client.post(deck_path, headers=headers, json={"deck_id": str(ids["decks"])})
    assert restarted.status_code == 200, restarted.text
    assert restarted.json()["data"]["id"] != first["id"]
    assert restarted.json()["data"]["state"]["focus_region"] is None
    client.portal.call(client.app.state.database.close)


@pytest.mark.postgres
def test_session_routes_require_owner_ready_deck_and_valid_typed_state(ready_session_api):
    client, ids, _, _, _ = ready_session_api
    base = "/api/v1/sessions"
    foreign = client.post(
        base,
        headers={"Authorization": "Bearer other"},
        json={"deck_id": str(ids["decks"])},
    )
    assert foreign.status_code == 403
    assert client.post(base, json={"deck_id": str(ids["decks"])}).status_code == 401

    database = client.app.state.database

    async def make_unready():
        async with database.sessions() as db, db.begin():
            record = await db.get(PresentationSession, ids["sessions"])
            job = await db.get(
                ProcessingJob, (await db.get(AnalysisVersion, record.analysis_version_id)).job_id
            )
            job.status = "running"

    client.portal.call(make_unready)
    unready = client.post(
        base, headers={"Authorization": "Bearer owner"}, json={"deck_id": str(ids["decks"])}
    )
    assert unready.status_code == 409

    invalid = client.post(
        base,
        headers={"Authorization": "Bearer owner"},
        json={"deck_id": str(ids["decks"]), "transcript": "must not persist"},
    )
    assert invalid.status_code == 422


@pytest.mark.postgres
def test_subscription_rejects_another_account_and_invalid_tokens(private_api):
    client, ids, _, _ = private_api
    path = f"/api/v1/sessions/{ids['sessions']}/events"
    assert client.get(path, headers={"Authorization": "Bearer other"}).status_code == 403
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer expired"}).status_code == 401


@pytest.mark.parametrize("kind", ["decks", "analyses", "assets", "sessions", "jobs"])
def test_missing_identity_cannot_reach_private_handlers(kind):
    with TestClient(create_app(Settings(Environment.TEST, ("testserver",)))) as client:
        path = f"/api/v1/resources/{kind}/{uuid4()}"
        for method, kwargs in [("get", {}), ("patch", {"json": {"title": "x"}}), ("delete", {})]:
            result = getattr(client, method)(path, **kwargs)
            assert result.status_code == 401
            assert result.headers["WWW-Authenticate"] == "Bearer"


def test_stream_rechecks_authentication_and_stops_on_revocation(monkeypatch):
    calls = []

    async def identify(request, header):
        calls.append(header)
        if len(calls) > 1:
            raise HTTPException(401)
        return Principal(uuid4(), "verified@example.test")

    async def snapshot(*args):
        return authorization.ResourceResponse(data={"status": "active"}, request_id="request")

    async def connected():
        return False

    async def immediate(_):
        return None

    monkeypatch.setattr(authorization, "authenticated", identify)
    monkeypatch.setattr(authorization, "operate", snapshot)
    monkeypatch.setattr(authorization.asyncio, "sleep", immediate)
    request = SimpleNamespace(headers={"authorization": "Bearer test"}, is_disconnected=connected)

    async def collect():
        return [item async for item in authorization.session_events(request, uuid4())]

    events = asyncio.run(collect())
    assert len(events) == 2
    assert events[0].startswith("event: snapshot")
    assert events[1] == 'event: access_closed\ndata: {"status":401}\n\n'
    assert calls == ["Bearer test", "Bearer test"]


@pytest.mark.postgres
def test_users_sharing_one_proxy_do_not_share_resource_or_bootstrap_budgets(
    private_api, monkeypatch
):
    client, ids, _, _ = private_api
    path = f"/api/v1/resources/decks/{ids['decks']}"
    # Both identities use the same TestClient peer, as unrelated ALB users do.
    for _ in range(36):
        assert client.get(path, headers={"Authorization": "Bearer owner"}).status_code == 200
        assert client.get(path, headers={"Authorization": "Bearer other"}).status_code == 403
    for _ in range(30):
        assert (
            client.get("/api/v1/auth/me", headers={"Authorization": "Bearer owner"}).status_code
            == 200
        )
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": "Bearer owner"}).status_code == 429
    )
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": "Bearer other"}).status_code == 200
    )
    assert client.get(path, headers={"Authorization": "Bearer owner"}).status_code == 200

    async def connected():
        return False

    async def immediate(_):
        return None

    # Exercise every real authentication/DB snapshot after the bootstrap budget is spent.
    # Replace only this module's clock, not asyncio.sleep throughout the DB event loop.
    monkeypatch.setattr(authorization, "asyncio", SimpleNamespace(sleep=immediate))
    request = SimpleNamespace(
        app=client.app,
        headers={"authorization": "Bearer owner"},
        state=SimpleNamespace(request_id=str(uuid4())),
        is_disconnected=connected,
        client=SimpleNamespace(host="same-proxy"),
    )

    async def collect():
        return [event async for event in authorization.session_events(request, ids["sessions"])]

    events = client.portal.call(collect)
    assert len(events) == 30
    assert all(event.startswith("event: snapshot") for event in events)


@pytest.mark.postgres
def test_validation_schema_matches_actual_body_and_uuid_errors(private_api):
    client, ids, _, other = private_api
    headers = {"Authorization": "Bearer owner"}
    responses = [
        client.patch(
            f"/api/v1/resources/decks/{ids['decks']}",
            headers=headers,
            json={"owner_id": str(other), "title": "spoofed"},
        ),
        client.get("/api/v1/resources/decks/not-a-uuid", headers=headers),
        client.get("/api/v1/sessions/not-a-uuid/events", headers=headers),
    ]
    for response in responses:
        assert response.status_code == 422
        assert ErrorResponse.model_validate(response.json()).error.code == "validation_error"
    paths = client.app.openapi()["paths"]
    for path in ("/api/v1/resources/{kind}/{identifier}", "/api/v1/sessions/{identifier}/events"):
        for operation in paths[path].values():
            schema = operation["responses"]["422"]["content"]["application/json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/ErrorResponse"}
            assert operation["responses"]["422"]["description"] == "Request validation failed."
