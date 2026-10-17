import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from buddy_api.auth import CognitoVerifier
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import AnalysisVersion, ProcessingJob, RenderedAsset
from buddy_api.persistence.repositories import Repositories
from buddy_identity.config import IdentityConfig, SessionExpired

CONFIG = IdentityConfig(
    "eu-central-1",
    "eu-central-1_test",
    "publicclient",
    "https://buddy.auth.eu-central-1.amazoncognito.com",
)


@pytest.fixture
def library_api(migrated_database, monkeypatch, api_client_factory):
    async def seed():
        db = Database(migrated_database)
        try:
            async with db.sessions() as session, session.begin():
                repos = Repositories.for_session(session)
                owner = await repos.users.create("owner")
                other = await repos.users.create("other")
                outsider = await repos.decks.create(owner_id=other.id, title="PRIVATE")
                outsider.source_key = "private-key"
                decks = []
                for index in range(25):
                    title = f"Deck {index:02}" if index else "Budget 50%_final"
                    deck = await repos.decks.create(owner_id=owner.id, title=title)
                    deck.created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
                    decks.append(deck.id)
                    if index < 5:
                        job = await repos.jobs.create(owner_id=owner.id, deck_id=deck.id)
                        job.status = ["pending", "running", "failed", "succeeded", "succeeded"][
                            index
                        ]
                        job.attempts = 2
                        if index == 4:
                            from backend.tests.support.analysis import create

                            await create(
                                repos,
                                owner_id=owner.id,
                                deck_id=deck.id,
                                version=1,
                                job_id=job.id,
                            )
                            session.add(
                                RenderedAsset(
                                    owner_id=owner.id,
                                    deck_id=deck.id,
                                    storage_key="private-render",
                                    content_type="image/png",
                                    size_bytes=10,
                                )
                            )
            return decks, owner.id
        finally:
            await db.close()

    decks, owner = asyncio.run(seed())

    def verify(self, token):
        if token not in {"owner", "other"}:
            raise SessionExpired("invalid")
        return token

    monkeypatch.setattr(CognitoVerifier, "verify", verify)
    settings = Settings(
        Environment.TEST, ("testserver",), database=migrated_database, identity=CONFIG
    )
    return api_client_factory(settings), decks, owner, settings


HEADERS = {"Authorization": "Bearer owner"}


@pytest.mark.postgres
def test_owner_scope_paging_sort_search_literal_patterns(library_api):
    client, ids, _, _ = library_api
    first = client.get("/api/v1/decks", headers=HEADERS)
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    data = first.json()["data"]
    assert data["total"] == 25 and len(data["items"]) == 20
    assert data["items"][0]["id"] == str(ids[-1])
    assert "PRIVATE" not in first.text and "private" not in first.text
    second = client.get("/api/v1/decks?page=2", headers=HEADERS).json()["data"]
    assert len(second["items"]) == 5
    assert not ({r["id"] for r in data["items"]} & {r["id"] for r in second["items"]})
    oldest = client.get("/api/v1/decks?sort=oldest", headers=HEADERS).json()["data"]
    assert oldest["items"][0]["id"] == str(ids[0])
    for sort, expected in [("title_asc", ids[0]), ("title_desc", ids[-1])]:
        result = client.get("/api/v1/decks", params={"sort": sort}, headers=HEADERS)
        assert result.json()["data"]["items"][0]["id"] == str(expected)
    for term in ["50%_", "budget", "  BUDGET  "]:
        result = client.get("/api/v1/decks", params={"search": term}, headers=HEADERS)
        assert result.json()["data"]["total"] == 1
    for term in ["PRIVATE", "\\", "nonexistent"]:
        result = client.get("/api/v1/decks", params={"search": term}, headers=HEADERS)
        assert result.json()["data"]["total"] == 0
    assert client.get("/api/v1/decks?page=9", headers=HEADERS).json()["data"]["items"] == []
    other = client.get("/api/v1/decks", headers={"Authorization": "Bearer other"})
    assert other.json()["data"]["total"] == 1
    assert other.json()["data"]["items"][0]["title"] == "PRIVATE"
    assert "private-key" not in other.text


@pytest.mark.postgres
def test_durable_status_latest_attempt_and_reload_across_app_instances(
    library_api, api_client_factory
):
    client, ids, owner, settings = library_api
    data = client.get("/api/v1/decks?sort=oldest", headers=HEADERS).json()["data"]
    assert [row["status"] for row in data["items"][:6]] == [
        "queued",
        "processing",
        "failed",
        "incomplete",
        "incomplete",
        "uploaded",
    ]
    assert data["items"][2]["attempts"] == 2

    async def change():
        db = Database(settings.database)
        try:
            async with db.sessions() as session, session.begin():
                job = await session.scalar(
                    select(ProcessingJob).where(ProcessingJob.deck_id == ids[1])
                )
                job.status = "failed"
                # An old successful job must not hide a newly queued retry.
                retry = ProcessingJob(owner_id=owner, deck_id=ids[4], status="pending")
                retry.created_at = datetime.now(UTC) + timedelta(days=1)
                session.add(retry)
        finally:
            await db.close()

    asyncio.run(change())
    fresh = api_client_factory(settings)
    data = fresh.get("/api/v1/decks?sort=oldest", headers=HEADERS).json()["data"]
    assert data["items"][1]["status"] == "failed"
    assert data["items"][4]["status"] == "queued"


@pytest.mark.postgres
def test_invalid_or_revoked_credentials_and_bounded_queries(library_api):
    client, _, _, _ = library_api
    for headers in [{}, {"Authorization": "Bearer invalid"}]:
        response = client.get("/api/v1/decks", headers=headers)
        assert response.status_code == 401 and "Deck" not in response.text
    for query in [
        "page=0",
        "page=100001",
        "page_size=0",
        "page_size=51",
        "sort=arbitrary",
        "search=" + "x" * 257,
    ]:
        result = client.get("/api/v1/decks?" + query, headers=HEADERS)
        assert result.status_code == 422
        assert result.json()["error"]["code"] == "validation_error"


def test_unconfigured_and_unsigned_library_fail_closed(api_client):
    assert api_client.get("/api/v1/decks").status_code == 401
    assert api_client.get("/api/v1/decks", headers=HEADERS).status_code == 503


@pytest.mark.postgres
def test_database_errors_recover_without_private_diagnostics(library_api, monkeypatch):
    client, _, _, _ = library_api
    # Inject a listing read failure; account resolution must still pass its own DB boundary.
    from sqlalchemy.ext.asyncio import AsyncSession

    scalar = AsyncSession.scalar

    async def broken(self, statement, *args, **kwargs):
        if "count" in str(statement):
            raise SQLAlchemyError("private-password")
        return await scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", broken)
    response = client.get("/api/v1/decks", headers=HEADERS)
    assert response.status_code == 503 and "private-password" not in response.text


@pytest.mark.postgres
@pytest.mark.parametrize("asset_count", [0, 1, 2])
def test_published_analysis_requires_complete_revision_bound_render_proof(library_api, asset_count):
    client, ids, owner, settings = library_api

    async def seed():
        db = Database(settings.database)
        try:
            async with db.sessions() as session, session.begin():
                repos = Repositories.for_session(session)
                from backend.tests.support.analysis import create

                job = await repos.jobs.create(owner_id=owner, deck_id=ids[5])
                job.status = "succeeded"
                analysis = await create(repos, owner_id=owner, deck_id=ids[5], job_id=job.id)
                assert analysis.publication == "published"
                for position in range(asset_count):
                    session.add(RenderedAsset(
                        owner_id=owner, deck_id=ids[5],
                        storage_key=f"unrelated-old-render-{position}",
                        content_type="image/png", size_bytes=1,
                    ))
        finally:
            await db.close()

    asyncio.run(seed())
    response = client.get("/api/v1/decks?sort=oldest", headers=HEADERS)
    assert response.status_code == 200
    # Even two rows (the manifest slide count) cannot prove slide order/revision/hashes.
    assert response.json()["data"]["items"][5]["status"] == "incomplete"


@pytest.mark.postgres
def test_new_candidate_revision_is_not_hidden_by_old_published_revision(library_api):
    client, ids, owner, settings = library_api

    async def rerun():
        db = Database(settings.database)
        try:
            async with db.sessions() as session, session.begin():
                repos = Repositories.for_session(session)
                from backend.tests.support.analysis import create

                job = await session.scalar(select(ProcessingJob).where(ProcessingJob.deck_id == ids[4]))
                old = await session.scalar(select(AnalysisVersion).where(AnalysisVersion.deck_id == ids[4]))
                assert old.publication == "published"
                current = await create(repos, owner_id=owner, deck_id=ids[4], job_id=job.id,
                                       version=2, actions=True)
                assert current.publication == "awaiting_validation"
                return old.id, current.id
        finally:
            await db.close()

    old_id, current_id = asyncio.run(rerun())
    response = client.get("/api/v1/decks?sort=oldest", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["data"]["items"][4]["status"] == "incomplete"
    for identifier, publication in [(old_id, "published"), (current_id, "awaiting_validation")]:
        historical = client.get(f"/api/v1/analyses/{identifier}", headers=HEADERS)
        assert historical.status_code == 200
        assert historical.json()["data"]["publication"] == publication
