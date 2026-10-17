"""Real extraction, transactions, revision conflicts and owner isolation in PostgreSQL."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from buddy_api.extraction import ExtractionError
from buddy_api.extraction.service import extract_and_store
from buddy_api.persistence.database import Database
from buddy_api.persistence.errors import PersistenceConflict, RecordNotFound
from buddy_api.persistence.models import DeckExtraction

pytestmark = pytest.mark.postgres
FIXTURE = (
    Path(__file__).resolve().parents[3] / "tests/fixtures/slides/Buddy_Extraction_Geometry.pptx"
)


def test_pdf_page_metadata_uses_saved_format_and_persists_idempotently(migrated_database):
    pdf = FIXTURE.with_name("Buddy_PDF_Geometry.pdf")

    async def scenario():
        db = Database(migrated_database)
        try:
            async with db.transaction() as repos:
                owner = await repos.users.create("pdf-owner")
                deck = await repos.decks.create(
                    owner_id=owner.id, title="PDF fixture", source_format="pdf"
                )
            kwargs = {"owner_id": owner.id, "deck_id": deck.id, "source_revision": "pdf-v1"}
            first = await extract_and_store(db, pdf, **kwargs)
            again = await extract_and_store(db, pdf, **kwargs)
            assert first.id == again.id
            assert first.document["source_format"] == "pdf"
            assert all(
                slide["text"] == "" and slide["objects"] == [] for slide in first.document["slides"]
            )
            with pytest.raises(ValueError, match="format"):
                await extract_and_store(db, pdf, source_format="pptx", **kwargs)
            await db.close()
            db = Database(migrated_database)
            async with db.transaction() as repos:
                assert (
                    await repos.extractions.get(first.id, owner_id=owner.id)
                ).document == first.document
        finally:
            await db.close()

    asyncio.run(scenario())


def test_real_extraction_persists_across_restart_with_idempotent_retries(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            async with db.transaction() as repos:
                owner = await repos.users.create("extract-owner")
                other = await repos.users.create("extract-other")
                deck = await repos.decks.create(owner_id=owner.id, title="Extraction fixture")
            kwargs = {"owner_id": owner.id, "deck_id": deck.id, "source_revision": "v1"}
            first = await extract_and_store(db, FIXTURE, **kwargs)
            again = await extract_and_store(db, FIXTURE, **kwargs)
            assert first.id == again.id
            assert first.document["slides"][0]["text"].startswith("Rotated title")
            assert first.document["source_sha256"] == first.source_sha256
            with pytest.raises(RecordNotFound):
                await extract_and_store(
                    db, FIXTURE, owner_id=other.id, deck_id=deck.id, source_revision="other"
                )
            async with db.transaction() as repos:
                with pytest.raises(RecordNotFound):
                    await repos.extractions.get(first.id, owner_id=other.id)
                assert await repos.extractions.list(owner_id=other.id) == []
                assert await repos.analyses.list(owner_id=owner.id) == []
                assert await repos.jobs.list(owner_id=owner.id) == []
            await db.close()
            db = Database(migrated_database)
            await db.check_ready()
            async with db.transaction() as repos:
                stored = await repos.extractions.get(first.id, owner_id=owner.id)
                assert stored.document == first.document
            second = await extract_and_store(
                db, FIXTURE, owner_id=owner.id, deck_id=deck.id, source_revision="v2"
            )
            assert second.id != first.id
        finally:
            await db.close()

    asyncio.run(scenario())


def test_conflicts_and_parser_failures_leave_no_partial_rows(migrated_database):
    import io

    from buddy_api.extraction import extract_pptx

    async def scenario():
        db = Database(migrated_database)
        try:
            async with db.transaction() as repos:
                owner = await repos.users.create("extract-owner")
                deck = await repos.decks.create(owner_id=owner.id, title="Fixture")
            first = await extract_and_store(
                db, FIXTURE, owner_id=owner.id, deck_id=deck.id, source_revision="v1"
            )
            doc = await asyncio.to_thread(extract_pptx, FIXTURE, source_revision="v1")
            conflict = doc.model_copy(update={"source_sha256": "a" * 64})
            with pytest.raises(PersistenceConflict):
                async with db.transaction() as repos:
                    await repos.extractions.save(
                        owner_id=owner.id, deck_id=deck.id, extraction=conflict
                    )
            with pytest.raises(ExtractionError):
                await extract_and_store(
                    db,
                    io.BytesIO(b"private malformed data"),
                    owner_id=owner.id,
                    deck_id=deck.id,
                    source_revision="v2",
                )
            async with db.transaction() as repos:
                values = await repos.extractions.list(owner_id=owner.id)
                assert [r.id for r in values] == [first.id]
                assert values[0].source_sha256 == doc.source_sha256
            async with db.sessions() as session:
                assert len(list(await session.scalars(select(DeckExtraction)))) == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_concurrent_retry_has_one_immutable_record(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            async with db.transaction() as repos:
                owner = await repos.users.create("extract-owner")
                deck = await repos.decks.create(owner_id=owner.id, title="Fixture")
            results = await asyncio.gather(
                *[
                    extract_and_store(
                        db,
                        FIXTURE,
                        owner_id=owner.id,
                        deck_id=deck.id,
                        source_revision="concurrent-v1",
                    )
                    for _ in range(2)
                ]
            )
            assert results[0].id == results[1].id
            async with db.transaction() as repos:
                assert len(await repos.extractions.list(owner_id=owner.id)) == 1
        finally:
            await db.close()

    asyncio.run(scenario())
