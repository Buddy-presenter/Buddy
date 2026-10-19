"""Use real transactions/parser; substitute only S3/SQS transport."""

import asyncio
import hashlib
import io
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from buddy_api.app import create_app
from buddy_api.auth import Principal, authenticated
from buddy_api.config import Environment, Settings
from buddy_api.extraction.runner import extract_pdf
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import DeckExtraction, JobOutbox, ProcessingJob
from buddy_api.processing import store
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.contracts import Message, MetadataOutput
from buddy_api.processing.service import Processor
from buddy_api.processing.stage import inspect_source
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.storage import SourceStorage

PDF = Path(__file__).resolve().parents[3] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
WORKER = WorkerSettings(
    queue_url="https://sqs.eu-central-1.amazonaws.com/123456789012/test", region="eu-central-1"
)
UPLOADS = UploadSettings(
    bucket="buddy-test",
    region="eu-central-1",
    kms_key="arn:aws:kms:eu-central-1:123456789012:key/00000000-0000-0000-0000-000000000000",
)
pytestmark = pytest.mark.postgres


class S3:
    def __init__(self, content):
        self.content, self.requests = content, []

    def get_object(self, **kwargs):
        self.requests.append(kwargs)
        return {
            "Body": io.BytesIO(self.content),
            "ContentLength": len(self.content),
            "ContentType": "application/pdf",
        }

    def close(self):
        pass


class SQS:
    def __init__(self):
        self.messages, self.acked = [], []
        self.fail = False

    def publish(self, message):
        if self.fail:
            raise OSError("private provider diagnostic")
        self.messages.append({"Body": message.model_dump_json(), "ReceiptHandle": str(uuid4())})

    def acknowledge(self, receipt):
        self.acked.append(receipt)


def make_processor(db, storage, settings, queue):
    return Processor(
        db, storage, settings, queue, inspector=lambda source: inspect_source(source, storage)
    )


async def seed(db, *, subject="worker-owner", content=None, source_format="pdf"):
    content = PDF.read_bytes() if content is None else content
    async with db.transaction() as repos:
        owner = await repos.users.create(subject)
        deck = await repos.decks.create(
            owner_id=owner.id, title="PDF worker", source_format=source_format
        )
        deck.source_revision, deck.source_version_id = str(uuid4()), "pinned-version"
        deck.source_sha256 = hashlib.sha256(content).hexdigest()
        deck.source_key, deck.source_size_bytes = f"sources/{owner.id}/original.pdf", len(content)
    async with db.sessions() as session, session.begin():
        job = await store.schedule(session, deck)
    return owner, deck, job


async def row(db, identifier):
    async with db.sessions() as session:
        return await session.get(ProcessingJob, identifier)


def test_real_pdf_worker_pinned_source_duplicate_and_restart(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db)
            sqs, s3 = SQS(), S3(PDF.read_bytes())
            processor = make_processor(db, SourceStorage(UPLOADS, s3), WORKER, sqs)
            await processor.maintain()
            assert len(sqs.messages) == 1
            await processor.run_message(sqs.messages[0])
            await processor.run_message(sqs.messages[0])
            result = await row(db, job.id)
            assert (result.status, result.attempts, result.progress) == ("metadata_ready", 1, 100)
            assert s3.requests[0]["VersionId"] == "pinned-version" and len(s3.requests) == 1
            async with db.transaction() as repos:
                extraction = await repos.extractions.get(result.extraction_id, owner_id=owner.id)
                assert len(extraction.document["slides"]) == 3
                assert [page["index"] for page in extraction.document["slides"]] == list(range(3))
                assert all(
                    page["width"] > 0 and page["height"] > 0
                    for page in extraction.document["slides"]
                )
                assert len({page["id"] for page in extraction.document["slides"]}) == 3
                assert extraction.document["warnings"]
                assert await repos.analyses.list(owner_id=owner.id) == []
            await db.close()
            db = Database(migrated_database)
            assert (await row(db, job.id)).extraction_id == extraction.id
            async with db.sessions() as session, session.begin():
                assert (await store.schedule(session, deck)).id == job.id
            await processor.run_message({"Body": "not-json", "ReceiptHandle": "poison"})
            assert "poison" not in sqs.acked
        finally:
            await db.close()

    asyncio.run(scenario())


def test_publish_failure_and_send_commit_loss_are_reconciled(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            _, _, job = await seed(db)
            queue = SQS()
            processor = make_processor(
                db, SourceStorage(UPLOADS, S3(PDF.read_bytes())), WORKER, queue
            )
            queue.fail = True
            await processor.maintain()
            assert (await row(db, job.id)).status == "queued"
            async with db.sessions() as session, session.begin():
                outbox = await session.get(JobOutbox, job.id)
                assert outbox.publish_attempts == 1
                outbox.available_at = store.now() - timedelta(seconds=1)
            queue.fail = False
            await processor.maintain()
            # Successful send followed by a failed commit must leave a republishable intent.
            async with db.sessions() as session, session.begin():
                outbox = await session.get(JobOutbox, job.id)
                outbox.available_at = store.now() - timedelta(seconds=1)
            await processor.maintain()
            assert len(queue.messages) == 2
            await asyncio.gather(*(processor.run_message(m) for m in queue.messages))
            assert (await row(db, job.id)).attempts == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_expired_lease_rejects_old_output_and_recovers_after_restart(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            _, _, job = await seed(db)
            async with db.sessions() as session, session.begin():
                old = await store.claim(session, Message(job_id=job.id, generation=0), WORKER)
                token = old.lease_id
                old.lease_until = store.now() - timedelta(seconds=1)
            await db.close()
            db = Database(migrated_database)
            queue = SQS()
            processor = make_processor(
                db, SourceStorage(UPLOADS, S3(PDF.read_bytes())), WORKER, queue
            )
            await processor.maintain()
            recovered = await row(db, job.id)
            assert recovered.status == "retry_wait" and recovered.generation == 1
            async with db.sessions() as session, session.begin():
                assert not await store.heartbeat(session, job.id, token, WORKER)
                assert (
                    await store.claim(session, Message(job_id=job.id, generation=0), WORKER) is None
                )
                (await session.get(JobOutbox, job.id)).available_at = store.now()
                (await session.get(ProcessingJob, job.id)).not_before = store.now()
            await processor.maintain()
            await processor.run_message(queue.messages[-1])
            assert (await row(db, job.id)).status == "metadata_ready"
            assert (await row(db, job.id)).attempts == 2
        finally:
            await db.close()

    asyncio.run(scenario())


def test_cancel_retry_race_fences_outputs_and_partial_persistence_rolls_back(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db)
            output = MetadataOutput(
                metadata=await asyncio.to_thread(
                    extract_pdf, PDF, source_revision=deck.source_revision
                )
            )
            async with db.sessions() as session, session.begin():
                claimed = await store.claim(session, Message(job_id=job.id, generation=0), WORKER)
                token = claimed.lease_id
            async with db.sessions() as session, session.begin():
                await store.cancel(session, await store.owned(session, owner.id, job.id))
            async with db.sessions() as session, session.begin():
                assert not await store.complete(session, job.id, token, output)
                await store.retry(session, await store.owned(session, owner.id, job.id), WORKER)
            async with db.sessions() as session, session.begin():
                assert not await store.complete(session, job.id, token, output)
                claimed = await store.claim(session, Message(job_id=job.id, generation=2), WORKER)
                token = claimed.lease_id
            with pytest.raises(RuntimeError):
                async with db.sessions() as session, session.begin():
                    assert await store.complete(session, job.id, token, output)
                    await session.flush()
                    raise RuntimeError("simulate failure before commit")
            async with db.sessions() as session:
                assert (await session.scalars(select(DeckExtraction))).all() == []
            assert (await row(db, job.id)).status == "running"
            async with db.sessions() as session, session.begin():
                assert await store.complete(session, job.id, token, output)
            assert (await row(db, job.id)).status == "metadata_ready"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_invalid_pdf_dead_letter_safe_error_and_bounded_retry(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            content = b"PRIVATE broken PDF"
            owner, _, job = await seed(db, content=content)
            queue = SQS()
            processor = make_processor(db, SourceStorage(UPLOADS, S3(content)), WORKER, queue)
            for attempt in range(3):
                current = await row(db, job.id)
                await processor.run_message(
                    {
                        "Body": Message(
                            job_id=job.id, generation=current.generation
                        ).model_dump_json(),
                        "ReceiptHandle": "failure",
                    }
                )
                current = await row(db, job.id)
                assert (
                    current.status == "dead_letter"
                    and current.error_code == "pdf_inspection_failed"
                )
                async with db.sessions() as session, session.begin():
                    current = await store.owned(session, owner.id, job.id)
                    if attempt < 2:
                        await store.retry(session, current, WORKER)
                    else:
                        with pytest.raises(UploadError, match="attempts_exhausted"):
                            await store.retry(session, current, WORKER)
            assert (await row(db, job.id)).attempts == 3
        finally:
            await db.close()

    asyncio.run(scenario())


def test_pdf_only_quota_and_global_concurrency(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            _, _, legacy = await seed(db, source_format="pptx", subject="pptx")
            assert legacy is None
            owner, deck, first = await seed(db)
            _, _, second = await seed(db, subject="second")
            async with db.sessions() as session, session.begin():
                assert await store.claim(
                    session,
                    Message(job_id=first.id, generation=0),
                    WORKER.model_copy(update={"concurrency": 1}),
                )
            async with db.sessions() as session, session.begin():
                assert (
                    await store.claim(
                        session,
                        Message(job_id=second.id, generation=0),
                        WORKER.model_copy(update={"concurrency": 1}),
                    )
                    is None
                )
                with pytest.raises(UploadError):
                    await store.quota(session, owner.id, 1)
                assert (await store.schedule(session, deck, 1)).id == first.id
        finally:
            await db.close()

    asyncio.run(scenario())


def test_owner_api_and_legacy_library_mapping(migrated_database):
    async def seed_all():
        db = Database(migrated_database)
        try:
            owner, _, job = await seed(db)
            async with db.transaction() as repos:
                for status in (
                    "pending",
                    "running",
                    "succeeded",
                    "failed",
                    "queued",
                    "retry_wait",
                    "metadata_ready",
                    "cancelled",
                    "dead_letter",
                ):
                    deck = await repos.decks.create(owner_id=owner.id, title=status)
                    record = await repos.jobs.create(owner_id=owner.id, deck_id=deck.id)
                    record.status = status
            return owner.id, job.id
        finally:
            await db.close()

    owner, job = asyncio.run(seed_all())
    app = create_app(Settings(Environment.TEST, ("testserver",), database=migrated_database))
    app.state.worker_settings = WORKER
    app.dependency_overrides[authenticated] = lambda: Principal(owner, "owner@example.test")
    with TestClient(app) as client:
        response = client.get(f"/api/v1/jobs/{job}")
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert response.json()["data"]["analysis_ready"] is False
        assert "source_key" not in response.text and "owner_id" not in response.text
        assert client.post(f"/api/v1/jobs/{job}/cancel").json()["data"]["status"] == "cancelled"
        assert client.post(f"/api/v1/jobs/{job}/retry").json()["data"]["status"] == "queued"
        assert client.post(f"/api/v1/jobs/{job}/retry").status_code == 409
        decks = client.get("/api/v1/decks").json()["data"]["items"]
        mapping = {item["title"]: item["status"] for item in decks}
        assert mapping["metadata_ready"] == mapping["succeeded"] == "incomplete"
        assert mapping["dead_letter"] == mapping["failed"] == "failed"
        app.dependency_overrides[authenticated] = lambda: Principal(uuid4(), "other@example.test")
        for path in ("", "/retry", "/cancel"):
            response = (
                client.get(f"/api/v1/jobs/{job}")
                if not path
                else client.post(f"/api/v1/jobs/{job}{path}")
            )
            assert response.status_code == 404
        app.dependency_overrides.clear()
        assert client.get(f"/api/v1/jobs/{job}").status_code == 401


def test_worker_process_fails_closed_without_config(tmp_path):
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("BUDDY_", "AWS_"))
    }
    env["TMP"] = env["TEMP"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "buddy_api.processing"], capture_output=True, env=env, timeout=15
    )
    assert result.returncode == 1
    assert b"Worker stopped" in result.stderr and b"Traceback" not in result.stderr
    result = subprocess.run(
        [sys.executable, "-m", "buddy_api.processing", "--health"],
        capture_output=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 1


def test_pre_worker_records_survive_forward_migration_and_schema_matches(database_settings):
    from alembic import command
    from sqlalchemy import text

    from buddy_api.persistence.engine import create_engine
    from buddy_api.persistence.migration_config import migration_config

    async def scenario():
        engine = create_engine(database_settings)
        owner, deck = uuid4(), uuid4()
        try:
            async with engine.connect() as connection:
                await connection.run_sync(
                    lambda sync: command.upgrade(
                        migration_config(database_settings, sync), "0008_rollback_compatibility"
                    )
                )
            async with engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO users(id,subject) VALUES (:owner,'legacy-worker-owner')"),
                    {"owner": owner},
                )
                await connection.execute(
                    text("INSERT INTO decks(id,owner_id,title) VALUES (:deck,:owner,'legacy')"),
                    {"owner": owner, "deck": deck},
                )
                for status in ("pending", "running", "succeeded", "failed"):
                    await connection.execute(
                        text(
                            "INSERT INTO processing_jobs(id,owner_id,deck_id,status,attempts) "
                            "VALUES (:id,:owner,:deck,:status,2)"
                        ),
                        {"id": uuid4(), "owner": owner, "deck": deck, "status": status},
                    )
            async with engine.connect() as connection:
                await connection.run_sync(
                    lambda sync: command.upgrade(migration_config(database_settings, sync), "head")
                )
                await connection.run_sync(
                    lambda sync: command.check(migration_config(database_settings, sync))
                )
            db = Database(database_settings)
            try:
                async with db.sessions() as session, session.begin():
                    await store.reconcile(session, WORKER)
                    jobs = (await session.scalars(select(ProcessingJob))).all()
                    assert {j.status for j in jobs} == {"pending", "running", "succeeded", "failed"}
                    assert all(j.source_revision is None and j.attempts == 2 for j in jobs)
            finally:
                await db.close()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
