"""Real persisted owner/cross-account boundaries with deterministic identity responses."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from buddy_api import authorization
from buddy_api.app import create_app
from buddy_api.auth import CognitoVerifier, Principal, VerifiedIdentity
from buddy_api.config import Environment, Settings
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import RenderedAsset
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
            from backend.tests.support.analysis import create

            analysis = await create(repos, owner_id=owner.id, deck_id=deck.id, version=1)
            job = await repos.jobs.create(owner_id=owner.id, deck_id=deck.id)
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
        == 200
    )
    assert (
        client.patch(
            session_path, headers=headers, json={"status": "ended", "state_version": 0}
        ).status_code
        == 409
    )
    assert (
        client.patch(
            session_path, headers=headers, json={"status": "ended", "state_version": 1}
        ).status_code
        == 200
    )
    assert client.delete(session_path, headers=headers).status_code == 200
    assert client.get(session_path, headers=headers).status_code == 404


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
