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


def test_powerpoint_busy_waits_and_recovers_historical_results(migrated_database):
    from buddy_api.validation.powerpoint import POWERPOINT_BUSY_REASON

    async def scenario():
        content, _ = source()
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db, content=content, source_format="pptx")
            provider = Provider(actions=("draw_rectangle",))
            worker, queue, _ = processor(db, content, provider=provider)
            calls = []

            class Validator:
                busy = True
                validated = False

                async def reconcile(self):
                    pass

                async def validate(self, owner_id, deck_id, key, request, **kwargs):
                    calls.append(request)
                    return powerpoint_result(
                        deck, request, busy=self.busy, validated=self.validated
                    )

            validator = Validator()
            worker.validation = validator
            await worker.maintain()
            await worker.run_message(queue.messages[-1])
            current = await row(db, job.id)
            assert current.status == "retry_wait" and current.attempts == 0
            assert current.error_code == "powerpoint_busy"
            async with db.sessions() as session, session.begin():
                current = await session.get(ProcessingJob, job.id)
                current.not_before = store.now()
                outbox = await session.get(JobOutbox, job.id)
                outbox.available_at = store.now()
            validator.busy = False
            await worker.maintain()
            await worker.run_message(queue.messages[-1])
            assert (await row(db, job.id)).status == "analysis_ready"
            async with db.sessions() as session, session.begin():
                analyses = Analyses(session)
                record = await analyses.latest_for_deck(deck.id, owner_id=owner.id)
                result = await analyses.read(record.id, owner_id=owner.id)
                first = result.slides[0].actions[0]
                await analyses.record_validation(
                    owner_id=owner.id,
                    result=ValidationInput(
                        source_format="pptx",
                        validation_id=uuid4(),
                        analysis_id=result.id,
                        analysis_sha256=result.analysis_sha256,
                        instance_id=first.action.instance_id,
                        action_sha256=digest(first.action),
                        validator_version="powerpoint-executor-2",
                        renderer_version="desktop-powerpoint-2",
                        display_profile="windows-powerpoint-2",
                        state="needs-review",
                        reasons=(POWERPOINT_BUSY_REASON,),
                    ),
                )
            old_calls = len(calls)
            validator.validated = True
            await worker.maintain()
            await worker.run_message(queue.messages[-1])
            assert (await row(db, job.id)).status == "analysis_ready"
            assert len(calls) == old_calls + 1 and provider.calls == 1
            async with db.sessions() as session:
                result = await Analyses(session).read(record.id, owner_id=owner.id)
                assert result.slides[0].actions[0].usable
                assert result.slides[1].actions[0].state == "needs-review"
            generation = (await row(db, job.id)).generation
            await worker.maintain()
            assert (await row(db, job.id)).generation == generation
        finally:
            await db.close()

    asyncio.run(scenario())


def test_powerpoint_busy_is_not_cached_as_completed_evidence(migrated_database, monkeypatch):
    import sys
    from types import SimpleNamespace

    from backend.tests.support.powerpoint import manifest

    import buddy_api.validation.service as service_module
    from buddy_api.analysis.powerpoint import analysis_document
    from buddy_api.persistence.models import ActionCheck
    from buddy_api.uploads.contracts import UploadError
    from buddy_api.validation.config import ValidatorSettings
    from buddy_api.validation.powerpoint import powerpoint_validation_request
    from buddy_api.validation.service import ValidationService

    async def scenario():
        content, extracted = source()
        db = Database(migrated_database)
        try:
            owner, deck, _job = await seed(db, content=content, source_format="pptx")
            uploads = UploadService(db, UPLOADS, SourceStorage(UPLOADS, S3(content)))
            validator = ValidationService(uploads, ValidatorSettings(python=sys.executable))
            document = analysis_document(
                output(extracted),
                extracted,
                manifest(extracted, deck.id),
                deck.id,
                uuid4(),
                uuid4(),
            )
            request = powerpoint_validation_request(document.slides[0].actions[0], document)
            calls = []

            def run(_content, process_request, _settings, _cancelled):
                calls.append(process_request)
                stored = powerpoint_result(deck, process_request, busy=len(calls) == 1)
                return SimpleNamespace(result=stored.result, artifacts={})

            monkeypatch.setattr(service_module, "run_validation", run)
            key = uuid4()
            with pytest.raises(UploadError) as failure:
                await validator.validate(owner.id, deck.id, key, request, source_content=content)
            assert failure.value.code == "powerpoint_busy"
            async with db.sessions() as session, session.begin():
                cached = await session.scalar(
                    select(ActionCheck).where(ActionCheck.retry_key == key)
                )
                assert (
                    cached.status == "failed"
                    and cached.storage_bytes == 0
                    and cached.result is None
                )
                # Reproduce the old completed busy cache without overwriting it on recheck.
                cached.status = "complete"
                cached.result = powerpoint_result(deck, calls[0], busy=True).result.model_dump(
                    mode="json"
                )
                old_id = cached.id
            checked = await validator.validate(
                owner.id, deck.id, key, request, source_content=content
            )
            assert checked.id != old_id and len(calls) == 2
            again = await validator.validate(
                owner.id, deck.id, key, request, source_content=content
            )
            assert again.id == checked.id and len(calls) == 2
        finally:
            await db.close()

    asyncio.run(scenario())


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


def powerpoint_result(deck, request, *, busy=False, validated=False):
    import sys

    from buddy_api.analysis.contracts import PowerPointContentAction
    from buddy_api.validation.contracts import ArtifactReference, PowerPointStoredResult
    from buddy_api.validation.powerpoint import POWERPOINT_BUSY_REASON, PowerPointProcessResult

    return PowerPointStoredResult(
        id=uuid4(),
        deck_id=deck.id,
        evidence=[ArtifactReference(id=uuid4(), kind="report", sha256="a" * 64, size_bytes=1)]
        if validated
        else [],
        result=PowerPointProcessResult(
            state="validated" if validated else "needs-review",
            reasons=[] if validated else [POWERPOINT_BUSY_REASON if busy else "test_unavailable"],
            runtime_platform=sys.platform,
            source_revision=deck.source_revision,
            source_sha256=deck.source_sha256,
            action_sha256=digest(PowerPointContentAction.model_validate(request.action)),
            office_version="16.0.test" if validated else None,
            before_sha256="1" * 64 if validated else None,
            after_sha256="2" * 64 if validated else None,
        ),
    )


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


def test_large_powerpoint_validation_resumes_before_deadline_without_regeneration(
    migrated_database, monkeypatch
):
    import sys
    from types import SimpleNamespace

    import buddy_api.processing.service as service_module
    from buddy_api.analysis.contracts import PowerPointContentAction
    from buddy_api.validation.contracts import PowerPointStoredResult
    from buddy_api.validation.powerpoint import PowerPointProcessResult

    elapsed = [0.0]
    monkeypatch.setattr(service_module, "time", SimpleNamespace(perf_counter=lambda: elapsed[0]))

    async def scenario():
        content, _ = source()
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db, content=content, source_format="pptx")
            provider = Provider(actions=("highlight_region", "draw_underline"))
            worker, queue, _ = processor(db, content, provider=provider)
            worker.settings = WORKER.model_copy(update={"runtime_seconds": 300})
            calls = []

            class Validator:
                async def reconcile(self):
                    pass

                async def validate(self, owner_id, deck_id, key, request, **kwargs):
                    calls.append(key)
                    elapsed[0] += 250
                    return PowerPointStoredResult(
                        id=uuid4(),
                        deck_id=deck_id,
                        evidence=[],
                        result=PowerPointProcessResult(
                            state="needs-review",
                            reasons=["test_runtime_unavailable"],
                            runtime_platform=sys.platform,
                            source_revision=deck.source_revision,
                            source_sha256=deck.source_sha256,
                            action_sha256=digest(
                                PowerPointContentAction.model_validate(request.action)
                            ),
                        ),
                    )

            worker.validation = Validator()
            for index in range(4):
                if index:
                    async with db.sessions() as session, session.begin():
                        current = await session.get(ProcessingJob, job.id)
                        current.not_before = store.now()
                        outbox = await session.get(JobOutbox, job.id)
                        outbox.available_at = store.now()
                await worker.maintain()
                await worker.run_message(queue.messages[-1])
                current = await row(db, job.id)
                if index < 3:
                    assert current.status == "retry_wait" and current.attempts == 0
                else:
                    assert current.status == "analysis_ready" and current.attempts == 1
            assert provider.calls == 1
            assert len(calls) == len(set(calls)) == 4
            async with db.sessions() as session:
                record = await Analyses(session).latest_for_deck(deck.id, owner_id=owner.id)
                result = await Analyses(session).read(record.id, owner_id=owner.id)
                assert all(a.state != "candidate" for s in result.slides for a in s.actions)
        finally:
            await db.close()

    asyncio.run(scenario())
