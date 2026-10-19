"""Contract rejection and real PostgreSQL immutable publication/evidence boundaries."""

import asyncio
import json
from uuid import uuid4

import pytest
from alembic import command
from backend.tests.support.analysis import FIXTURE, create, prepare
from pydantic import ValidationError
from sqlalchemy import select, text

from buddy_api.analysis.catalog import CATALOG, general_controls, generation_schema
from buddy_api.analysis.contracts import AnalysisInput, ValidationInput, digest
from buddy_api.auth import CognitoVerifier, VerifiedIdentity
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import Database
from buddy_api.persistence.errors import PersistenceConflict, RecordNotFound
from buddy_api.persistence.migration_config import migration_config
from buddy_api.persistence.models import (
    ActionValidation,
    AnalysisSlide,
    AnalysisVersion,
    LegacyAnalysisPayload,
    SlideAction,
)


def sample():
    return json.loads(FIXTURE.read_text())


@pytest.mark.parametrize(
    "kind", ["next_slide", "previous_slide", "go_to_slide", "reset_view", "shell"]
)
def test_generation_and_storage_exclude_general_controls(kind):
    value = sample()
    value["slides"][0]["actions"][0].update(type=kind, params={})
    with pytest.raises(ValidationError):
        AnalysisInput.model_validate(value)
    assert {entry["properties"]["type"]["const"] for entry in generation_schema()["oneOf"]} == {
        "zoom_region",
        "highlight_region",
    }
    assert len(CATALOG) == 6


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "order",
        "action_id",
        "stale",
        "unknown_field",
        "purpose",
        "pending_text",
        "unavailable_reason",
        "aspect",
        "oversize",
        "boolean",
        "nan",
        "extra_params",
    ],
)
def test_invalid_analysis_is_rejected_before_persistence(change):
    value = sample()
    action = value["slides"][0]["actions"][0]
    if change == "missing":
        value["slides"].pop()
    elif change == "duplicate":
        value["slides"][1]["slide_id"] = "slide-a"
    elif change == "order":
        value["slides"].reverse()
    elif change == "action_id":
        value["slides"][1]["actions"][0]["instance_id"] = action["instance_id"]
    elif change == "stale":
        action["render_id"] = "old-render"
    elif change == "unknown_field":
        value["graph"] = {}
    elif change == "purpose":
        action["purpose"] = " "
    elif change == "pending_text":
        value["presentation_summary"]["status"] = "pending"
    elif change == "unavailable_reason":
        value["presentation_summary"] = {"status": "unavailable"}
    elif change == "aspect":
        value["manifest"]["slides"][0]["raster"]["height"] = 500
    elif change == "oversize":
        value["presentation_summary"]["text"] = "x" * 16001
    elif change == "extra_params":
        action["params"]["command"] = "no"
    else:
        action["params"]["region"]["width"] = {
            "boolean": True,
            "nan": float("nan"),
        }[change]
    with pytest.raises(ValidationError):
        AnalysisInput.model_validate(value)


def test_general_controls_come_from_order_not_stored_analysis():
    assert general_controls(("first", "last"), "first") == {
        "next_slide": True,
        "previous_slide": False,
        "go_to_slide": ("first", "last"),
        "reset_view": True,
    }
    assert general_controls(("first", "last"), "last")["next_slide"] is False
    assert general_controls(("only",), "only")["previous_slide"] is False
    for slides, current in [((), "first"), (("a", "a"), "a"), (("a",), "unknown")]:
        with pytest.raises(ValueError):
            general_controls(slides, current)


@pytest.mark.parametrize(
    "change",
    [
        {"evidence": []},
        {"reasons": ["failed"]},
        {"state": "rejected", "reasons": []},
        {"state": "needs-review", "reasons": []},
        {"state": "candidate"},
    ],
)
def test_invalid_validation_claims_are_rejected(change):
    value = json.loads((FIXTURE.parent / "validation-v1.json").read_text())
    value.update(change)
    with pytest.raises(ValidationError):
        ValidationInput.model_validate(value)


def test_published_schemas_and_evidence_fixture_match_versioned_contracts():
    from buddy_api.analysis.catalog import generation_schema
    from buddy_api.analysis.contracts import ValidationInput

    for name, contract in [("analysis-v1", AnalysisInput), ("validation-v1", ValidationInput)]:
        assert (
            json.loads((FIXTURE.parent.parent / (name + ".schema.json")).read_text())
            == contract.model_json_schema()
        )
    assert (
        json.loads(
            (FIXTURE.parent.parent / "generated-content-action-v1.1.schema.json").read_text()
        )
        == generation_schema()
    )
    inputs = AnalysisInput.model_validate(sample())
    result = ValidationInput.model_validate_json(
        (FIXTURE.parent / "validation-v1.json").read_text()
    )
    assert result.analysis_sha256 == digest(inputs)
    assert result.action_sha256 == digest(inputs.slides[0].actions[0])


@pytest.mark.postgres
@pytest.mark.parametrize("status", ["pending", "unavailable"])
def test_partial_summaries_and_legacy_analysis_never_make_library_ready(
    migrated_database, status, monkeypatch, api_client_factory
):
    async def scenario():
        db = Database(migrated_database)
        try:
            async with db.transaction() as repos:
                owner = await repos.users.create("owner")
                deck = await repos.decks.create(owner_id=owner.id, title="Partial fixture")
                inputs = await prepare(repos, owner_id=owner.id, deck_id=deck.id, actions=False)
                data = inputs.model_dump(mode="json")
                data["slides"][1]["summary"] = {
                    "status": status,
                    "reason": "Not readable" if status == "unavailable" else None,
                }
                saved = await repos.analyses.create(
                    owner_id=owner.id,
                    deck_id=deck.id,
                    version=1,
                    document=AnalysisInput.model_validate(data),
                )
                assert saved.publication == "partial"
                result = await repos.analyses.read(saved.id, owner_id=owner.id)
                assert result.slides[0].summary.status == "available"
                job = await repos.jobs.get(inputs.job_id, owner_id=owner.id)
                job.status = "succeeded"
                from buddy_api.persistence.models import RenderedAsset

                repos.analyses.session.add(
                    RenderedAsset(
                        owner_id=owner.id,
                        deck_id=deck.id,
                        storage_key="synthetic-asset",
                        content_type="image/png",
                        size_bytes=1,
                    )
                )
        finally:
            await db.close()

    asyncio.run(scenario())
    from backend.tests.api.test_library import CONFIG

    monkeypatch.setattr(
        CognitoVerifier,
        "verify",
        lambda self, token: VerifiedIdentity(token, "verified@example.test"),
    )
    client = api_client_factory(
        Settings(Environment.TEST, ("testserver",), database=migrated_database, identity=CONFIG)
    )
    response = client.get("/api/v1/decks", headers={"Authorization": "Bearer owner"})
    assert response.json()["data"]["items"][0]["status"] == "incomplete"


def validation(result, action, *, state="validated", **changes):
    values = {
        "validation_id": uuid4(),
        "analysis_id": result.id,
        "analysis_sha256": result.analysis_sha256,
        "instance_id": action.instance_id,
        "action_sha256": digest(action),
        "validator_version": result.inputs.validator_version,
        "renderer_version": result.inputs.renderer_version,
        "display_profile": result.inputs.display_profile,
        "state": state,
        "evidence": [{"id": uuid4(), "kind": "executor_result", "sha256": "d" * 64}],
        "reasons": () if state == "validated" else ("Synthetic rejected fixture",),
    }
    values.update(changes)
    return ValidationInput.model_validate(values)


@pytest.mark.postgres
def test_round_trip_restart_evidence_publication_and_revision_isolation(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            async with db.transaction() as repos:
                owner = await repos.users.create("analysis-owner")
                deck = await repos.decks.create(owner_id=owner.id, title="Fixture")
                inputs = await prepare(repos, owner_id=owner.id, deck_id=deck.id)
                saved = await repos.analyses.create(
                    owner_id=owner.id, deck_id=deck.id, version=1, document=inputs
                )
                result = await repos.analyses.read(saved.id, owner_id=owner.id)
                assert result.publication == "awaiting_validation"
                assert all(not a.usable for s in result.slides for a in s.actions)
                presentation = await repos.sessions.create(
                    owner_id=owner.id, deck_id=deck.id, analysis_version_id=saved.id
                )
            await db.close()
            db = Database(migrated_database)
            async with db.transaction() as repos:
                resumed = await repos.analyses.read(saved.id, owner_id=owner.id)
                assert resumed == result
                assert (
                    await repos.sessions.get(presentation.id, owner_id=owner.id)
                ).analysis_version_id == saved.id
                first = validation(result, inputs.slides[0].actions[0])
                current = await repos.analyses.record_validation(owner_id=owner.id, result=first)
                assert current.slides[0].actions[0].usable
                assert current.publication == "awaiting_validation"
                assert (
                    await repos.analyses.record_validation(owner_id=owner.id, result=first)
                    == current
                )
                rejected = validation(result, inputs.slides[1].actions[0], state="rejected")
                published = await repos.analyses.record_validation(
                    owner_id=owner.id, result=rejected
                )
                assert published.publication == "published"
                assert not published.slides[1].actions[0].usable
                assert published.slides[1].summary.status == "available"
                second = await create(repos, owner_id=owner.id, deck_id=deck.id, version=2)
                assert second.id != saved.id
                assert await repos.analyses.read(saved.id, owner_id=owner.id) == published
                assert (
                    await repos.sessions.get(presentation.id, owner_id=owner.id)
                ).analysis_version_id == saved.id
            await db.close()
            db = Database(migrated_database)
            async with db.transaction() as repos:
                assert await repos.analyses.read(saved.id, owner_id=owner.id) == published
        finally:
            await db.close()

    asyncio.run(scenario())


@pytest.mark.postgres
def test_owner_lineage_stale_evidence_and_immutable_database_rows(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            async with db.transaction() as repos:
                owner = await repos.users.create("owner")
                other = await repos.users.create("other")
                deck = await repos.decks.create(owner_id=owner.id, title="Fixture")
                inputs = await prepare(repos, owner_id=owner.id, deck_id=deck.id)
                saved = await repos.analyses.create(
                    owner_id=owner.id, deck_id=deck.id, version=1, document=inputs
                )
                result = await repos.analyses.read(saved.id, owner_id=owner.id)
            with pytest.raises(RecordNotFound):
                async with db.transaction() as repos:
                    await repos.analyses.read(saved.id, owner_id=other.id)
            for change in [
                {"renderer_version": "old"},
                {"display_profile": "other-profile"},
                {"validator_version": "old"},
                {"action_sha256": "0" * 64},
                {"analysis_sha256": "0" * 64},
            ]:
                with pytest.raises(PersistenceConflict):
                    async with db.transaction() as repos:
                        await repos.analyses.record_validation(
                            owner_id=owner.id,
                            result=validation(result, inputs.slides[0].actions[0], **change),
                        )
            with pytest.raises(RecordNotFound):
                async with db.transaction() as repos:
                    await repos.analyses.record_validation(
                        owner_id=other.id, result=validation(result, inputs.slides[0].actions[0])
                    )
            for field, new in [("source_revision", "stale"), ("source_sha256", "0" * 64)]:
                data = inputs.model_dump(mode="json")
                data[field] = new
                with pytest.raises(PersistenceConflict):
                    async with db.transaction() as repos:
                        await repos.analyses.create(
                            owner_id=owner.id,
                            deck_id=deck.id,
                            version=2,
                            document=AnalysisInput.model_validate(data),
                        )
            for model, column, replacement in [
                (AnalysisVersion, "summary_text", "tamper"),
                (AnalysisSlide, "summary_text", "tamper"),
                (SlideAction, "purpose", "tamper"),
                (ActionValidation, "state", "validated"),
            ]:
                with pytest.raises(PersistenceConflict):
                    async with db.transaction() as repos:
                        # Use the actual transaction's session, never a mock database.
                        record = await repos.analyses.session.scalar(select(model).limit(1))
                        setattr(record, column, replacement)
                        await repos.analyses.session.flush()
            async with db.transaction() as repos:
                assert (
                    await repos.analyses.read(saved.id, owner_id=owner.id)
                ).publication == "awaiting_validation"
            with pytest.raises(PersistenceConflict):
                async with db.transaction() as repos:
                    record = await repos.analyses.session.scalar(select(ActionValidation).limit(1))
                    await repos.analyses.session.delete(record)
                    await repos.analyses.session.flush()
            with pytest.raises(ValidationError):
                async with db.transaction() as repos:
                    await repos.analyses.create(
                        owner_id=owner.id,
                        deck_id=deck.id,
                        version=2,
                        document=inputs.model_copy(
                            update={"source_revision": "invalid / revision"}
                        ),
                    )
        finally:
            await db.close()

    asyncio.run(scenario())


@pytest.mark.postgres
def test_concurrent_idempotent_creation_and_conflicting_rerun(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            async with db.transaction() as repos:
                owner = await repos.users.create("owner")
                deck = await repos.decks.create(owner_id=owner.id, title="Fixture")
                inputs = await prepare(repos, owner_id=owner.id, deck_id=deck.id, actions=False)

            async def save():
                async with db.transaction() as repos:
                    return await repos.analyses.create(
                        owner_id=owner.id, deck_id=deck.id, version=1, document=inputs
                    )

            records = await asyncio.gather(save(), save())
            assert records[0].id == records[1].id
            data = inputs.model_dump(mode="json")
            data["presentation_summary"]["text"] = "Changed summary"
            with pytest.raises(PersistenceConflict):
                async with db.transaction() as repos:
                    await repos.analyses.create(
                        owner_id=owner.id,
                        deck_id=deck.id,
                        version=1,
                        document=AnalysisInput.model_validate(data),
                    )
            async with db.transaction() as repos:
                assert len(await repos.analyses.list(owner_id=owner.id)) == 1
        finally:
            await db.close()

    asyncio.run(scenario())


@pytest.mark.postgres
def test_migration_archives_legacy_payload_and_preserves_session_fk(database_settings):
    async def scenario():
        db = Database(database_settings)
        owner, deck, analysis, session_id = (uuid4() for _ in range(4))
        try:
            async with db.engine.connect() as conn:

                def old_schema(sync):
                    command.upgrade(migration_config(database_settings, sync), "0003_extractions")

                await conn.run_sync(old_schema)
                await conn.execute(
                    text("INSERT INTO users(id,subject) VALUES (:id,'legacy-owner')"), {"id": owner}
                )
                await conn.execute(
                    text("INSERT INTO decks(id,owner_id,title) VALUES (:id,:owner,'Legacy')"),
                    {"id": deck, "owner": owner},
                )
                await conn.execute(
                    text(
                        "INSERT INTO analysis_versions(id,owner_id,deck_id,version,graph) "
                        "VALUES (:id,:owner,:deck,1,CAST(:payload AS jsonb))"
                    ),
                    {
                        "id": analysis,
                        "owner": owner,
                        "deck": deck,
                        "payload": '{"untyped":"historical"}',
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO presentation_sessions"
                        "(id,owner_id,deck_id,analysis_version_id) "
                        "VALUES (:id,:owner,:deck,:analysis)"
                    ),
                    {"id": session_id, "owner": owner, "deck": deck, "analysis": analysis},
                )
                await conn.commit()

                def upgrade(sync):
                    command.upgrade(migration_config(database_settings, sync), "head")

                await conn.run_sync(upgrade)
            await db.check_ready()
            async with db.transaction() as repos:
                result = await repos.analyses.read(analysis, owner_id=owner)
                assert result.publication == "legacy" and result.inputs is None
                assert result.slides == ()
                assert (
                    await repos.sessions.get(session_id, owner_id=owner)
                ).analysis_version_id == analysis
                archived = await repos.analyses.session.get(LegacyAnalysisPayload, analysis)
                assert archived.payload == {"untyped": "historical"}
        finally:
            await db.close()

    asyncio.run(scenario())


@pytest.mark.postgres
def test_typed_api_auth_restart_no_public_validation_writes(
    migrated_database, monkeypatch, api_client_factory
):
    async def seed():
        db = Database(migrated_database)
        try:
            async with db.transaction() as repos:
                owner = await repos.users.create("owner")
                await repos.users.create("other")
                deck = await repos.decks.create(owner_id=owner.id, title="Fixture")
                saved = await create(repos, owner_id=owner.id, deck_id=deck.id, actions=True)
                return saved.id
        finally:
            await db.close()

    identifier = asyncio.run(seed())
    monkeypatch.setattr(
        CognitoVerifier,
        "verify",
        lambda self, token: VerifiedIdentity(token, "verified@example.test"),
    )
    from backend.tests.api.test_library import CONFIG

    settings = Settings(
        Environment.TEST, ("testserver",), database=migrated_database, identity=CONFIG
    )
    path = f"/api/v1/analyses/{identifier}"
    client = api_client_factory(settings)
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer other"}).status_code == 403
    response = client.get(path, headers={"Authorization": "Bearer owner"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "graph" not in response.text
    assert response.json()["data"]["publication"] == "awaiting_validation"
    fresh = api_client_factory(settings)
    assert (
        fresh.get(path, headers={"Authorization": "Bearer owner"}).json()["data"]
        == response.json()["data"]
    )
    assert (
        client.post(
            path, json={"state": "validated"}, headers={"Authorization": "Bearer owner"}
        ).status_code
        == 405
    )
