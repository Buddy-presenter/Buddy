"""Application smoke and persisted job boundaries using shared lifespan/database fixtures."""

from uuid import uuid4

import pytest

from buddy_api.config import Environment, Settings
from buddy_api.persistence.errors import RecordNotFound


def test_api_lifespan_smoke_and_error_contract(api_client):
    assert api_client.get("/health").json()["status"] == "ok"
    response = api_client.get("/api/v1/ready")
    assert response.status_code == 503  # Never report an unconfigured database as healthy.
    missing = api_client.get("/no-such-endpoint")
    assert missing.status_code == 404
    assert missing.headers["x-request-id"]
    assert api_client.get("/api/v1/health", headers={"host": "foreign.example"}).status_code == 400


@pytest.mark.postgres
def test_job_state_is_shared_by_application_instances_and_rejects_foreign_owner(
    api_client_factory, migrated_database
):
    settings = Settings(Environment.TEST, ("testserver",), database=migrated_database)
    first = api_client_factory(settings)

    async def write():
        async with first.app.state.database.transaction() as repos:
            owner = await repos.users.create("fixture-owner-" + uuid4().hex)
            foreign = await repos.users.create("fixture-foreign-" + uuid4().hex)
            deck = await repos.decks.create(owner_id=owner.id, title="Fixture deck")
            job = await repos.jobs.create(owner_id=owner.id, deck_id=deck.id)
            return owner.id, foreign.id, job.id

    owner, foreign, identifier = first.portal.call(write)
    second = api_client_factory(settings)
    assert second.get("/api/v1/ready").status_code == 200

    async def read():
        async with second.app.state.database.transaction() as repos:
            job = await repos.jobs.get(identifier, owner_id=owner)
            assert job.status == "pending"
            with pytest.raises(RecordNotFound):
                await repos.jobs.get(identifier, owner_id=foreign)

    second.portal.call(read)
