"""Real PPTX parser, durable PostgreSQL stages and owner APIs; fake cloud transports only."""

import asyncio
import io
from uuid import uuid4

import httpx
import pytest
from backend.tests.processing.test_worker import SQS, UPLOADS, WORKER, row, seed
from backend.tests.support.powerpoint import output, source
from fastapi.testclient import TestClient
from sqlalchemy import select

from buddy_api.analysis.contracts import PowerPointAnalysisResult, ValidationInput, digest
from buddy_api.analysis.powerpoint import PowerPointProvider
from buddy_api.analysis.provider import ProviderFailure
from buddy_api.analysis.store import Analyses
from buddy_api.app import create_app
from buddy_api.auth import Principal, authenticated
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import Database
from buddy_api.persistence.errors import PersistenceConflict
from buddy_api.persistence.models import AnalysisVersion, JobOutbox, ProcessingJob
from buddy_api.processing import store
from buddy_api.processing.contracts import Message, PowerPointSource
from buddy_api.processing.service import Processor
from buddy_api.processing.stage import inspect_source
from buddy_api.uploads.contracts import SOURCE_TYPES
from buddy_api.uploads.service import UploadService
from buddy_api.uploads.storage import SourceStorage

pytestmark = pytest.mark.postgres


class S3:
    def __init__(self, content):
        self.content = content
        self.reads = []

    def get_object(self, **request):
        self.reads.append(request)
        return {
            "Body": io.BytesIO(self.content),
            "ContentLength": len(self.content),
            "ContentType": SOURCE_TYPES["pptx"],
        }

    def close(self):
        pass


class Provider:
    def __init__(self, *, actions=(), fail=False):
        self.calls = 0
        self.actions, self.fail = actions, fail

    async def analyze_async(self, content, extraction, deck_id, manifest):
        self.calls += 1
        assert extraction.source_format == "pptx"
        assert manifest.source_format == "pptx" and manifest.deck_id == deck_id
        if self.fail:
            raise ProviderFailure("provider_access_denied", permanent=True)
        return output(extraction, self.actions)


def processor(db, content, *, provider=None):
    queue = SQS()
    storage = SourceStorage(UPLOADS, S3(content))
    worker = Processor(
        db,
        storage,
        WORKER,
        queue,
        inspector=lambda src: inspect_source(src, storage),
        powerpoint_provider=provider or Provider(),
    )
    return worker, queue, storage


def test_powerpoint_durable_pipeline_restart_owner_reads_and_format_routes(migrated_database):
    content, _ = source()

    async def scenario():
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db, content=content, source_format="pptx")
            provider = Provider()
            worker, queue, _ = processor(db, content, provider=provider)
            await worker.maintain()
            await worker.run_message(queue.messages[0])
            await worker.run_message(queue.messages[0])
            current = await row(db, job.id)
            assert current.status == "analysis_ready" and current.attempts == 1
            assert provider.calls == 1
            async with db.sessions() as session:
                record = await Analyses(session).latest_for_deck(deck.id, owner_id=owner.id)
                result = await Analyses(session).read(record.id, owner_id=owner.id)
                assert isinstance(result, PowerPointAnalysisResult)
                assert result.publication == "published"
                assert result.inputs.source_format == "pptx"
                assert len(result.slides) == 2
                assert result.inputs.manifest.slides[0].source.width == 960
            return owner.id, deck.id, result.model_dump(mode="json")
        finally:
            await db.close()

    owner, deck, saved = asyncio.run(scenario())
    app = create_app(Settings(Environment.TEST, ("testserver",), database=migrated_database))
    app.dependency_overrides[authenticated] = lambda: Principal(owner, "owner@example.test")
    with TestClient(app) as client:
        app.state.worker_settings = WORKER
        app.state.uploads = UploadService(
            app.state.database, UPLOADS, SourceStorage(UPLOADS, S3(content))
        )
        route = f"/api/v1/analyses/powerpoint/by-deck/{deck}"
        response = client.get(route)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert response.json()["data"] == saved
        assert client.get(f"/api/v1/analyses/by-deck/{deck}").status_code == 415
        assert client.get(f"/api/v1/decks/{deck}/source").status_code == 409
        downloaded = client.get(f"/api/v1/decks/{deck}/powerpoint-source")
        assert downloaded.status_code == 200 and downloaded.content == content
        assert downloaded.headers["X-Source-Format"] == "pptx"
        listing = client.get("/api/v1/decks").json()["data"]["items"][0]
        assert listing["processing"]["state"] == "review_ready" and not listing["can_present"]
        app.dependency_overrides[authenticated] = lambda: Principal(uuid4(), "other@example.test")
        assert client.get(route).status_code in {403, 404}
        assert client.get(f"/api/v1/decks/{deck}/powerpoint-source").status_code == 404


def test_powerpoint_cancellation_fences_output_and_retry_preserves_format(migrated_database):
    async def scenario():
        content, _ = source()
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db, content=content, source_format="pptx")
            worker, queue, storage = processor(db, content)
            async with db.sessions() as session, session.begin():
                current = await store.claim(session, Message(job_id=job.id, generation=0), WORKER)
                token = current.lease_id
            metadata = inspect_source(
                PowerPointSource(
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
                current = await session.get(ProcessingJob, job.id)
                await store.cancel(session, current)
                assert await store.save_metadata(session, job.id, token, metadata) is None
                await store.retry(session, current, WORKER)
                assert current.stage == "pptx_metadata.v1"
            await worker.maintain()
            await worker.run_message(queue.messages[0])
            assert (await row(db, job.id)).status == "analysis_ready"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_powerpoint_provider_failure_is_durable_without_fake_summaries(migrated_database):
    async def scenario():
        content, _ = source()
        db = Database(migrated_database)
        try:
            _, _, job = await seed(db, content=content, source_format="pptx")
            worker, queue, _ = processor(db, content, provider=Provider(fail=True))
            await worker.maintain()
            await worker.run_message(queue.messages[0])
            current = await row(db, job.id)
            assert current.status in {"failed", "dead_letter"}
            assert current.error_code == "provider_access_denied" and current.extraction_id
            async with db.sessions() as session:
                assert (
                    await session.scalar(
                        select(AnalysisVersion).where(AnalysisVersion.job_id == job.id)
                    )
                    is None
                )
        finally:
            await db.close()

    asyncio.run(scenario())


def test_malformed_model_response_retries_then_publishes_without_manual_retry(
    migrated_database, monkeypatch
):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-test-secret")
    content, extracted = source()
    calls = []

    def respond(request):
        calls.append(request)
        blocks = (
            []
            if len(calls) == 1
            else [
                {"type": "text", "text": "Analysis ready."},
                {
                    "type": "tool_use",
                    "name": "submit_powerpoint_analysis",
                    "input": output(extracted, ()).model_dump(mode="json"),
                },
            ]
        )
        return httpx.Response(200, json={"stop_reason": "tool_use", "content": blocks})

    async def scenario():
        db = Database(migrated_database)
        try:
            _, _, job = await seed(db, content=content, source_format="pptx")
            provider = PowerPointProvider("eu-central-1", transport=httpx.MockTransport(respond))
            worker, queue, _ = processor(db, content, provider=provider)
            await worker.maintain()
            await worker.run_message(queue.messages[-1])
            current = await row(db, job.id)
            assert current.status == "retry_wait"
            assert current.error_code == "provider_malformed_output"
            async with db.sessions() as session, session.begin():
                current = await session.get(ProcessingJob, job.id)
                current.not_before = store.now()
                outbox = await session.get(JobOutbox, job.id)
                outbox.available_at = store.now()
            await worker.maintain()
            await worker.run_message(queue.messages[-1])
            current = await row(db, job.id)
            assert current.status == "analysis_ready" and current.attempts == 2
            assert len(calls) == 2
        finally:
            await db.close()

    asyncio.run(scenario())


def test_powerpoint_candidates_survive_runtime_failure_and_reject_pdf_evidence(migrated_database):
    async def scenario():
        content, _ = source()
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db, content=content, source_format="pptx")
            worker, queue, _ = processor(
                db, content, provider=Provider(actions=("draw_rectangle",))
            )
            await worker.maintain()
            await worker.run_message(queue.messages[0])
            current = await row(db, job.id)
            assert current.status == "retry_wait" and current.error_code == "validator_unavailable"
            async with db.sessions() as session, session.begin():
                analyses = Analyses(session)
                record = await analyses.latest_for_deck(deck.id, owner_id=owner.id)
                result = await analyses.read(record.id, owner_id=owner.id)
                action = result.slides[0].actions[0]
                assert action.state == "candidate" and not action.usable
                bad = ValidationInput(
                    validation_id=uuid4(),
                    analysis_id=result.id,
                    analysis_sha256=result.analysis_sha256,
                    instance_id=action.action.instance_id,
                    action_sha256=digest(action.action),
                    validator_version="powerpoint-executor-2",
                    renderer_version="desktop-powerpoint-2",
                    display_profile="windows-powerpoint-2",
                    state="needs-review",
                    reasons=("runtime_unavailable",),
                    source_format="pdf",
                )
                with pytest.raises(PersistenceConflict):
                    await analyses.record_validation(owner_id=owner.id, result=bad)
        finally:
            await db.close()

    asyncio.run(scenario())
