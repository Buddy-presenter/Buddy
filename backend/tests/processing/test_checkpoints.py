"""Real PostgreSQL and PDF subprocesses: interrupted work resumes without regeneration."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from backend.tests.processing.test_worker import (
    PDF,
    S3,
    SQS,
    UPLOADS,
    WORKER,
    SyntheticProvider,
    make_processor,
    row,
    seed,
)
from fastapi.testclient import TestClient

from buddy_api.analysis.provider import ProviderFailure
from buddy_api.app import create_app
from buddy_api.auth import Principal, authenticated
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import Deck, PDFProcessingCheckpoint, ProcessingJob
from buddy_api.processing import checkpoints, service, store
from buddy_api.processing.contracts import Message, PDFSource
from buddy_api.processing.stage import inspect_source
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.service import UploadService
from buddy_api.uploads.storage import SourceStorage

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("mode", ["timeout", "yield"])
def test_timeout_saves_provider_pages_and_resumes_after_restart(
    migrated_database, monkeypatch, mode
):
    calls, visited = [], []
    real_grounding = service.execute_grounding
    clock = [0.0]
    if mode == "yield":
        monkeypatch.setattr(service, "time", SimpleNamespace(perf_counter=lambda: clock[0]))

    class Provider(SyntheticProvider):
        def analyze(self, *args):
            calls.append(1)
            return super().analyze(*args)

    def interrupt(*args, page_number=None):
        visited.append(page_number)
        if mode == "timeout" and visited == [1, 2]:
            raise TimeoutError("private child diagnostic")
        result = real_grounding(*args, page_number=page_number)
        if mode == "yield" and visited == [1]:
            clock[0] = 800.0
        return result

    monkeypatch.setattr(service, "execute_grounding", interrupt)

    async def first_delivery():
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db)
            queue = SQS()
            worker = make_processor(
                db, SourceStorage(UPLOADS, S3(PDF.read_bytes())), WORKER, queue, provider=Provider()
            )
            await worker.maintain()
            await worker.run_message(queue.messages[-1])
            failed = await row(db, job.id)
            assert (failed.status, failed.stage, failed.error_code) == (
                "retry_wait",
                "grounding",
                "grounding_timeout" if mode == "timeout" else None,
            )
            assert failed.attempts == (1 if mode == "timeout" else 0)
            async with db.sessions() as session:
                checkpoint = await session.get(PDFProcessingCheckpoint, job.id)
                assert set(checkpoint.pages) == {"1"}
                assert len(checkpoint.generated["slides"]) == 3
            return owner.id, deck.id, job.id
        finally:
            await db.close()

    owner, deck, identifier = asyncio.run(first_delivery())
    app = create_app(Settings(Environment.TEST, ("testserver",), database=migrated_database))
    app.dependency_overrides[authenticated] = lambda: Principal(owner, "owner@example.test")
    with TestClient(app) as client:
        saved = client.get(f"/api/v1/jobs/{identifier}/pdf-summaries")
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert saved.json()["grounded_pages"] == [1]
        assert len(saved.json()["slides"]) == 3
        assert "actions" not in saved.text and "private child" not in saved.text
        app.dependency_overrides[authenticated] = lambda: Principal(uuid4(), "foreign@example.test")
        assert client.get(f"/api/v1/jobs/{identifier}/pdf-summaries").status_code == 404

    async def restarted_delivery():
        db = Database(migrated_database)
        try:
            async with db.sessions() as session, session.begin():
                job = await session.get(ProcessingJob, identifier)
                job.not_before = store.now() - timedelta(seconds=1)
                message = Message(job_id=job.id, generation=job.generation)
            queue = SQS()
            worker = make_processor(
                db, SourceStorage(UPLOADS, S3(PDF.read_bytes())), WORKER, queue, provider=Provider()
            )
            await worker.run_message(
                {"Body": message.model_dump_json(), "ReceiptHandle": "resumed"}
            )
            completed = await row(db, identifier)
            assert completed.status == "analysis_ready"
            assert calls == [1] and visited == ([1, 2, 2, 3] if mode == "timeout" else [1, 2, 3])
            async with db.sessions() as session:
                assert await session.get(PDFProcessingCheckpoint, identifier) is None
            completed_app = create_app(
                Settings(Environment.TEST, ("testserver",), database=migrated_database)
            )
            completed_app.dependency_overrides[authenticated] = lambda: Principal(
                owner, "owner@example.test"
            )
            with TestClient(completed_app) as client:
                final_summaries = client.get(f"/api/v1/jobs/{identifier}/pdf-summaries")
                assert final_summaries.status_code == 200, final_summaries.text
                assert final_summaries.json()["grounded_pages"] == [1, 2, 3]
            async with db.sessions() as session, session.begin():
                stale_write = await checkpoints.locked_job(session, identifier, uuid4())
                assert stale_write is None
            # Tombstone deletion removes private intermediate provider text, too.
            await UploadService(db, UPLOADS, worker.storage).request_deck_deletion(owner, deck)
            async with db.sessions() as session:
                assert await session.get(PDFProcessingCheckpoint, identifier) is None
        finally:
            await db.close()

    asyncio.run(restarted_delivery())


@pytest.mark.parametrize("change", ["cancelled", "source", "manifest"])
def test_checkpoint_rejects_cancelled_or_changed_work(migrated_database, change):
    async def scenario():
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db)
            storage = SourceStorage(UPLOADS, S3(PDF.read_bytes()))
            metadata = inspect_source(
                PDFSource(
                    key=deck.source_key,
                    version=deck.source_version_id,
                    size=deck.source_size_bytes,
                    sha256=deck.source_sha256,
                    revision=deck.source_revision,
                    deck_id=deck.id,
                ),
                storage,
            )
            async with db.sessions() as session, session.begin():
                claimed = await store.claim(
                    session, Message(job_id=job.id, generation=job.generation), WORKER
                )
                token = claimed.lease_id
                await store.save_metadata(session, job.id, token, metadata)
            generated = SyntheticProvider().analyze(
                PDF.read_bytes(), metadata.metadata, str(deck.id), metadata.manifest
            )
            async with db.sessions() as session, session.begin():
                assert await checkpoints.save_provider(
                    session, job.id, token, metadata.metadata, metadata.manifest, generated
                )
            async with db.sessions() as session, session.begin():
                if change == "cancelled":
                    current = await session.get(ProcessingJob, job.id)
                    await store.cancel(session, current)
                    assert not await checkpoints.save_page(
                        session,
                        job.id,
                        token,
                        metadata.metadata,
                        metadata.manifest,
                        1,
                        generated.model_copy(update={"slides": generated.slides[:1]}),
                    )
                elif change == "source":
                    current = await session.get(Deck, deck.id)
                    current.source_revision = str(uuid4())
                    await session.flush()
                    with pytest.raises(UploadError, match="source_changed"):
                        await checkpoints.load(
                            session, job.id, token, metadata.metadata, metadata.manifest
                        )
                else:
                    changed = metadata.manifest.model_copy(
                        update={"render_id": "different-renderer"}
                    )
                    with pytest.raises(ProviderFailure, match="grounding_checkpoint_stale"):
                        await checkpoints.load(session, job.id, token, metadata.metadata, changed)
            async with db.sessions() as session:
                assert (await session.get(PDFProcessingCheckpoint, job.id)).pages == {}
        finally:
            await db.close()

    asyncio.run(scenario())
