"""Use real transactions/parser; substitute only S3/SQS transport."""

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from backend.tests.analysis.test_provider import Session, response_for
from fastapi.testclient import TestClient
from sqlalchemy import select

from buddy_api.analysis import provider as provider_module
from buddy_api.analysis.provider import (
    BedrockProvider,
    ModelAction,
    ModelOutput,
    ModelSlide,
    ModelSummary,
)
from buddy_api.analysis.store import Analyses
from buddy_api.app import create_app
from buddy_api.auth import Principal, authenticated
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import ActionCheck, DeckExtraction, JobOutbox, ProcessingJob
from buddy_api.processing import store
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.contracts import Message, PDFSource
from buddy_api.processing.service import Processor
from buddy_api.processing.stage import inspect_source
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.service import UploadService
from buddy_api.uploads.storage import SourceStorage
from buddy_api.validation.config import ValidatorSettings
from buddy_api.validation.contracts import ProcessResult, StoredResult, action_digest
from buddy_api.validation.service import ValidationService

PDF = Path(__file__).resolve().parents[3] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
SCANNED_PDF = Path(__file__).resolve().parents[1] / "fixtures/pdf/scanned-source-anchor.pdf"
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


class SyntheticProvider:
    def analyze(self, content, extraction, deck_id, manifest):
        assert hashlib.sha256(content).hexdigest() == extraction.source_sha256
        assert manifest.deck_id == deck_id
        return ModelOutput(
            presentation=ModelSummary(text="Synthetic presentation summary.", reason=None),
            slides=tuple(
                ModelSlide(
                    number=index + 1,
                    text=f"Synthetic summary for page {index + 1}.",
                    reason=None,
                    actions=(),
                )
                for index, _page in enumerate(extraction.slides)
            ),
        )


def make_processor(db, storage, settings, queue, *, provider=None, validation=None):
    return Processor(
        db,
        storage,
        settings,
        queue,
        inspector=lambda source: inspect_source(source, storage),
        provider=provider or SyntheticProvider(),
        validation=validation,
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
            assert (result.status, result.attempts, result.progress) == ("analysis_ready", 1, 100)
            assert all(request["VersionId"] == "pinned-version" for request in s3.requests)
            assert len(s3.requests) == 2
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
                analyses = await repos.analyses.list(owner_id=owner.id)
                assert len(analyses) == 1 and analyses[0].publication == "published"
                assert analyses[0].job_id == job.id
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


def test_candidate_mode_persists_measured_trace_without_claiming_usable(
    migrated_database, monkeypatch
):
    selected = {}
    monkeypatch.setattr(provider_module, "Session", Session)

    def handler(request):
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"][1]["text"]
        candidates = json.loads(prompt.split("Candidates: ", 1)[1])
        target = next(item for item in candidates if item["page"] == 1)
        selected.update(target)
        return httpx.Response(
            200,
            json=response_for(
                output_text=json.dumps(
                    {
                        "presentation": {"text": "Synthetic selection.", "reason": None},
                        "slides": [
                            {
                                "number": number,
                                "text": "Synthetic page summary.",
                                "reason": None,
                                "actions": [
                                    {
                                        "type": "highlight_region",
                                        "purpose": "Focus on the selected first line.",
                                        "target_id": target["id"],
                                    }
                                ]
                                if number == 1
                                else [],
                            }
                            for number in range(1, 4)
                        ],
                    }
                )
            ),
        )

    async def scenario():
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db)
            queue = SQS()
            processor = make_processor(
                db,
                SourceStorage(UPLOADS, S3(PDF.read_bytes())),
                WORKER.model_copy(update={"text_targeting": "candidate_lines"}),
                queue,
                provider=BedrockProvider("eu-central-1", transport=httpx.MockTransport(handler)),
            )
            await processor.maintain()
            await processor.run_message(queue.messages[0])
            current = await row(db, job.id)
            assert (current.status, current.error_code) == ("retry_wait", "validator_unavailable")
            await db.close()
            db = Database(migrated_database)
            async with db.sessions() as session:
                analyses = await Analyses(session).list(owner_id=owner.id)
                result = await Analyses(session).read(analyses[0].id, owner_id=owner.id)
            trace = result.inputs.grounding[0].trace
            assert trace.method == "pdf_candidate_line" and trace.target_id == selected["id"]
            action = result.slides[0].actions[0]
            assert not action.usable
            assert action.action.params.region == trace.resolved_region
            assert trace.target_text == selected["text"]
        finally:
            await db.close()

    asyncio.run(scenario())


def test_flattened_image_subregion_stays_candidate_and_skips_executor_validation(migrated_database):
    class ImageSubregionProvider:
        def analyze(self, content, extraction, deck_id, manifest):
            return ModelOutput(
                presentation=ModelSummary(text="Scanned slide.", reason=None),
                slides=tuple(
                    ModelSlide(
                        number=index + 1,
                        text=f"Page {index + 1}.",
                        reason=None,
                        actions=(
                            ModelAction(
                                type="zoom_region",
                                purpose="Focus on one section of the scanned page image.",
                                target_text=None,
                                x=200,
                                y=400,
                                width=400,
                                height=200,
                            ),
                        )
                        if index == 0
                        else (),
                    )
                    for index, _page in enumerate(extraction.slides)
                ),
            )

    class MustNotExecute:
        async def reconcile(self):
            return None

        async def validate(self, *args, **kwargs):
            raise AssertionError("Image crop must wait for independent visual QA.")

    async def scenario():
        db = Database(migrated_database)
        try:
            content = SCANNED_PDF.read_bytes()
            owner, deck, job = await seed(db, content=content)
            queue = SQS()
            processor = make_processor(
                db,
                SourceStorage(UPLOADS, S3(content)),
                WORKER,
                queue,
                provider=ImageSubregionProvider(),
                validation=MustNotExecute(),
            )
            await processor.maintain()
            await processor.run_message(queue.messages[0])
            completed = await row(db, job.id)
            assert completed.status == "analysis_partial", completed.error_code
            async with db.sessions() as session:
                record = (await Analyses(session).list(owner_id=owner.id))[0]
                result = await Analyses(session).read(record.id, owner_id=owner.id)
            action = result.slides[0].actions[0]
            assert result.publication == "awaiting_validation"
            assert action.state == "candidate" and not action.usable
            assert action.validation is None
            assert result.visual_qa_pending_instance_ids == (action.action.instance_id,)
            assert "visual_qa_pending_instance_ids" not in result.model_dump(mode="json")
        finally:
            await db.close()

    asyncio.run(scenario())


def test_pending_validation_defers_without_spending_attempt_and_resumes_analysis(
    migrated_database, monkeypatch
):
    class CandidateProvider:
        calls = 0

        def analyze(self, content, extraction, deck_id, manifest):
            self.calls += 1
            return ModelOutput(
                presentation=ModelSummary(text="A focused presentation.", reason=None),
                slides=tuple(
                    ModelSlide(
                        number=index + 1,
                        text=f"Page {index + 1} summary.",
                        reason=None,
                        actions=(
                            ModelAction(
                                type="zoom_region",
                                purpose="Focus on the key detail.",
                                target_text="Buddy PDF presentation",
                                x=1,
                                y=1,
                                width=1280,
                                height=720,
                            ),
                        )
                        if index == 0
                        else (),
                    )
                    for index, _slide in enumerate(extraction.slides)
                ),
            )

    class PendingThenRejected:
        calls = 0

        def __init__(self, revision, source_sha):
            self.revision, self.source_sha = revision, source_sha

        async def reconcile(self):
            return None

        async def validate(
            self,
            owner,
            deck_id,
            key,
            request,
            *,
            source_content=None,
            precision="standard",
            duplicate_candidate=False,
        ):
            self.calls += 1
            if self.calls == 1:
                raise UploadError(
                    409, "validation_pending", "Validation reservation is still pending.", 300
                )
            action = request.action
            result = ProcessResult(
                state="rejected",
                reasons=["invalid_action_parameters"],
                profile=request.profile,
                runtime_platform=sys.platform,
                source_revision=self.revision,
                source_sha256=self.source_sha,
                action_sha256=action_digest(action),
            )
            return StoredResult(id=uuid4(), deck_id=deck_id, result=result, evidence=[])

    async def scenario():
        db = Database(migrated_database)
        try:
            failures = []
            original_fail = store.fail

            async def capture_fail(session, identifier, token, settings, code, *, permanent=False):
                failures.append(code)
                await original_fail(session, identifier, token, settings, code, permanent=permanent)

            monkeypatch.setattr(store, "fail", capture_fail)
            owner, deck, job = await seed(db)
            queue = SQS()
            provider = CandidateProvider()
            validator = PendingThenRejected(deck.source_revision, deck.source_sha256)
            processor = make_processor(
                db,
                SourceStorage(UPLOADS, S3(PDF.read_bytes())),
                WORKER,
                queue,
                provider=provider,
                validation=validator,
            )
            await processor.maintain()
            assert len(queue.messages) == 1
            await processor.run_message(queue.messages[-1])

            waiting = await row(db, job.id)
            assert waiting.status == "retry_wait"
            assert waiting.stage == "validation"
            assert waiting.attempts == 0
            assert waiting.extraction_id is not None
            assert provider.calls == 1 and validator.calls == 1

            async with db.sessions() as session, session.begin():
                current = await session.get(ProcessingJob, job.id)
                current.not_before = store.now()
                outbox = await session.get(JobOutbox, job.id)
                outbox.available_at = store.now()
            await processor.maintain()
            assert len(queue.messages) == 2
            await processor.run_message(queue.messages[-1])
            assert validator.calls == 2
            assert not failures, failures

            finished = await row(db, job.id)
            assert finished.stage == "validation" and finished.stage_state == "succeeded"
            assert (finished.status, finished.error_code, finished.attempts) == (
                "analysis_ready",
                None,
                1,
            )
            assert provider.calls == 1 and validator.calls == 2
            async with db.sessions() as session:
                analyses = await Analyses(session).list(owner_id=owner.id)
                assert len(analyses) == 1
                result = await Analyses(session).read(analyses[0].id, owner_id=owner.id)
                action = result.slides[0].actions[0]
                assert action.state == "rejected"
                assert action.validation is not None
                assert action.validation.reasons == ("invalid_action_parameters",)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_failed_evidence_cleanup_does_not_block_worker_start_or_other_jobs(migrated_database):
    class StorageWithBrokenCleanup(S3):
        def list_object_versions(self, **_kwargs):
            raise OSError("simulated object-store outage")

    async def scenario():
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db)
            storage = SourceStorage(UPLOADS, StorageWithBrokenCleanup(PDF.read_bytes()))
            uploads = UploadService(db, UPLOADS, storage)
            validator = ValidationService(
                uploads, ValidatorSettings(python=sys.executable, seconds=5)
            )
            async with db.sessions() as session, session.begin():
                reservation = ActionCheck(
                    owner_id=owner.id,
                    deck_id=deck.id,
                    retry_key=uuid4(),
                    request_sha256="a" * 64,
                    status="pending",
                    expires_at=store.now() - timedelta(seconds=1),
                    storage_bytes=1024,
                    artifacts=[],
                )
                session.add(reservation)
                await session.flush()
                reservation_id = reservation.id

            queue = SQS()
            processor = make_processor(db, storage, WORKER, queue, validation=validator)
            # This is the worker's startup reconciliation path. Cleanup failure is retained,
            # while the independent accepted PDF remains eligible for consumption.
            await processor.maintain(publish=False)
            async with db.sessions() as session:
                deferred = await session.get(ActionCheck, reservation_id)
                assert deferred.status == "cleanup"
            assert (await row(db, job.id)).status == "queued"

            await processor.maintain()
            assert len(queue.messages) == 1
            await processor.run_message(queue.messages[0])
            finished = await row(db, job.id)
            assert finished.status == "analysis_ready"
            async with db.sessions() as session:
                deferred = await session.get(ActionCheck, reservation_id)
                assert deferred.status == "cleanup"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_successful_publish_reconciles_before_redrive_or_retention(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            _, _, job = await seed(db)
            queue = SQS()
            processor = make_processor(
                db, SourceStorage(UPLOADS, S3(PDF.read_bytes())), WORKER, queue
            )
            started = store.now()
            await processor.maintain()
            assert len(queue.messages) == 1
            assert (await row(db, job.id)).status == "queued"
            async with db.sessions() as session, session.begin():
                outbox = await session.get(JobOutbox, job.id)
                assert outbox.publish_attempts == 1
                assert outbox.available_at >= started + timedelta(
                    seconds=WORKER.queue_reconciliation_seconds
                )
                assert outbox.available_at < started + timedelta(
                    seconds=WORKER.queue_reconciliation_seconds + 5
                )
                assert outbox.available_at < started + timedelta(
                    seconds=WORKER.runtime_seconds * WORKER.queue_redrive_max_receive_count
                )
                assert outbox.available_at < started + timedelta(
                    seconds=WORKER.queue_message_retention_seconds
                )
            await processor.maintain()
            assert len(queue.messages) == 1
            # Reconciliation is bounded by the redrive horizon, not message retention.
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


def test_outbox_republishes_valid_job_after_wakeup_reaches_dlq(migrated_database, monkeypatch):
    async def scenario():
        now = store.now()
        clock = {"now": now}
        monkeypatch.setattr(store, "now", lambda: clock["now"])
        db = Database(migrated_database)
        try:
            _, _, job = await seed(db)
            queue = SQS()
            processor = make_processor(
                db, SourceStorage(UPLOADS, S3(PDF.read_bytes())), WORKER, queue
            )
            first_publish = clock["now"]
            await processor.maintain()
            expired_wakeup = queue.messages[0]

            async with db.sessions() as session:
                outbox = await session.get(JobOutbox, job.id)
                reconcile_at = outbox.available_at
            redrive_at = first_publish + timedelta(
                seconds=WORKER.runtime_seconds * WORKER.queue_redrive_max_receive_count
            )
            retention_at = first_publish + timedelta(seconds=WORKER.queue_message_retention_seconds)
            assert reconcile_at < redrive_at < retention_at

            # The publisher refreshes the wake-up before the original can be redriven.
            clock["now"] = reconcile_at + timedelta(seconds=1)
            await processor.maintain()
            assert len(queue.messages) == 2
            replacement = queue.messages[1]
            assert Message.model_validate_json(replacement["Body"]).generation == job.generation

            # Model SQS moving the original delivery to the DLQ at its redrive horizon.
            clock["now"] = redrive_at
            assert queue.messages.pop(0) is expired_wakeup
            assert queue.messages == [replacement]
            assert (await row(db, job.id)).status == "queued"

            await processor.run_message(replacement)
            assert (await row(db, job.id)).status == "analysis_ready"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_publish_failures_back_off_and_capacity_miss_reschedules_one_wakeup(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            _, _, first = await seed(db, subject="first-worker-owner")
            _, _, waiting = await seed(db, subject="second-worker-owner")
            queue = SQS()
            settings = WORKER.model_copy(update={"concurrency": 1})
            processor = make_processor(
                db, SourceStorage(UPLOADS, S3(PDF.read_bytes())), settings, queue
            )
            queue.fail = True
            await processor.maintain()
            assert len(queue.messages) == 0
            async with db.sessions() as session, session.begin():
                first_outbox = await session.get(JobOutbox, first.id)
                waiting_outbox = await session.get(JobOutbox, waiting.id)
                assert first_outbox.publish_attempts == waiting_outbox.publish_attempts == 1
                assert first_outbox.available_at > store.now()
                first_outbox.available_at = store.now() - timedelta(seconds=1)
            await processor.maintain()
            async with db.sessions() as session, session.begin():
                first_outbox = await session.get(JobOutbox, first.id)
                waiting_outbox = await session.get(JobOutbox, waiting.id)
                assert first_outbox.publish_attempts == 2
                assert first_outbox.available_at >= store.now() + timedelta(seconds=59)
                first_outbox.available_at = store.now() - timedelta(seconds=1)
                waiting_outbox.available_at = store.now() - timedelta(seconds=1)
            queue.fail = False
            await processor.maintain()
            assert len(queue.messages) == 2
            async with db.sessions() as session, session.begin():
                claimed = await store.claim(
                    session, Message(job_id=first.id, generation=0), settings
                )
                assert claimed is not None
            await processor.run_message(queue.messages[1])
            assert queue.messages[1]["ReceiptHandle"] in queue.acked
            assert (await row(db, waiting.id)).status == "queued"
            async with db.sessions() as session, session.begin():
                outbox = await session.get(JobOutbox, waiting.id)
                assert outbox.available_at > store.now()
            await processor.maintain()
            assert len(queue.messages) == 2
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
            assert (await row(db, job.id)).status == "analysis_ready"
            assert (await row(db, job.id)).attempts == 2
        finally:
            await db.close()

    asyncio.run(scenario())


def test_slow_source_download_renews_worker_lease(migrated_database):
    class SlowFirstReadS3(S3):
        def __init__(self, content):
            super().__init__(content)
            self.first_read_started = threading.Event()

        def get_object(self, **kwargs):
            if not self.requests:
                self.first_read_started.set()
                time.sleep(12)
            return super().get_object(**kwargs)

    async def scenario():
        db = Database(migrated_database)
        try:
            _, _, job = await seed(db)
            sqs, s3 = SQS(), SlowFirstReadS3(PDF.read_bytes())
            processor = make_processor(db, SourceStorage(UPLOADS, s3), WORKER, sqs)
            await processor.maintain()
            attempt = asyncio.create_task(processor.run_message(sqs.messages[0]))
            assert await asyncio.to_thread(s3.first_read_started.wait, 5)
            await asyncio.sleep(11)
            current = await row(db, job.id)
            assert current.status == "running"
            assert current.lease_until > store.now() + timedelta(seconds=40)
            await attempt
            assert (await row(db, job.id)).status == "analysis_ready"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_cancel_retry_race_fences_outputs_and_partial_persistence_rolls_back(migrated_database):
    async def scenario():
        db = Database(migrated_database)
        try:
            owner, deck, job = await seed(db)
            source_storage = SourceStorage(UPLOADS, S3(PDF.read_bytes()))
            output = await asyncio.to_thread(
                inspect_source,
                PDFSource(
                    key=deck.source_key,
                    version=deck.source_version_id,
                    size=deck.source_size_bytes,
                    sha256=deck.source_sha256,
                    revision=deck.source_revision,
                    deck_id=deck.id,
                ),
                source_storage,
            )
            source_storage.close()
            async with db.sessions() as session, session.begin():
                claimed = await store.claim(session, Message(job_id=job.id, generation=0), WORKER)
                token = claimed.lease_id
                assert await store.advance_stage(session, job.id, token, "provider", 60)
                claimed = await session.get(ProcessingJob, job.id)
                assert claimed.stage == "provider" and claimed.progress == 60
                assert await store.advance_stage(session, job.id, token, "validation", 80)
                assert claimed.stage == "validation" and claimed.progress == 80
            async with db.sessions() as session, session.begin():
                await store.cancel(session, await store.owned(session, owner.id, job.id))
            async with db.sessions() as session, session.begin():
                assert not await store.advance_stage(session, job.id, token, "provider", 60)
            async with db.sessions() as session, session.begin():
                assert await store.save_metadata(session, job.id, token, output) is None
                await store.retry(session, await store.owned(session, owner.id, job.id), WORKER)
            async with db.sessions() as session, session.begin():
                assert await store.save_metadata(session, job.id, token, output) is None
                claimed = await store.claim(session, Message(job_id=job.id, generation=2), WORKER)
                token = claimed.lease_id
            with pytest.raises(RuntimeError):
                async with db.sessions() as session, session.begin():
                    assert await store.save_metadata(session, job.id, token, output)
                    await session.flush()
                    raise RuntimeError("simulate failure before commit")
            async with db.sessions() as session:
                assert (await session.scalars(select(DeckExtraction))).all() == []
            assert (await row(db, job.id)).status == "running"
            async with db.sessions() as session, session.begin():
                extraction_id = await store.save_metadata(session, job.id, token, output)
            async with db.sessions() as session, session.begin():
                generated = SyntheticProvider().analyze(
                    PDF.read_bytes(), output.metadata, str(deck.id), output.manifest
                )
                document = Processor._analysis_document(
                    generated, output.metadata, output.manifest, deck.id, job.id, extraction_id
                )
                analysis = await Analyses(session).create(
                    owner_id=owner.id, deck_id=deck.id, version=1, document=document
                )
                assert await store.complete(session, job.id, token, analysis.id)
            assert (await row(db, job.id)).status == "analysis_ready"
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
                    "analysis_ready",
                    "analysis_partial",
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
        assert mapping["analysis_ready"] == mapping["analysis_partial"] == "incomplete"
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


def test_preupgrade_metadata_ready_pdf_is_resumed_into_analysis(database_settings):
    from alembic import command

    from buddy_api.persistence.engine import create_engine
    from buddy_api.persistence.migration_config import migration_config

    class CountingProvider(SyntheticProvider):
        calls = 0

        def analyze(self, *args):
            self.calls += 1
            return super().analyze(*args)

    async def migrate_to(revision):
        engine = create_engine(database_settings)
        try:
            async with engine.connect() as connection:
                await connection.run_sync(
                    lambda sync: command.upgrade(
                        migration_config(database_settings, sync), revision
                    )
                )
        finally:
            await engine.dispose()

    async def scenario():
        await migrate_to("0009_pdf_workers")
        db = Database(database_settings)
        storage = SourceStorage(UPLOADS, S3(PDF.read_bytes()))
        owner, deck, job = await seed(db)
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
        async with db.transaction() as repos:
            extraction = await repos.extractions.save(
                owner_id=owner.id, deck_id=deck.id, extraction=metadata.metadata
            )
        async with db.sessions() as session, session.begin():
            old_job = await session.get(ProcessingJob, job.id)
            old_job.status = "metadata_ready"
            old_job.stage, old_job.stage_state, old_job.progress = (
                "pdf_metadata.v1",
                "succeeded",
                100,
            )
            old_job.attempts = 1
            old_job.extraction_id = extraction.id
            old_generation = old_job.generation
        await db.close()

        # This simulates deploying the analysis worker over a real 0009-completed PDF.
        await migrate_to("head")
        db = Database(database_settings)
        queue, provider = SQS(), CountingProvider()
        processor = Processor(
            db,
            storage,
            WORKER,
            queue,
            inspector=lambda _source: pytest.fail("saved metadata must be reused"),
            provider=provider,
        )
        try:
            await processor.maintain()
            assert len(queue.messages) == 1
            migrated = await row(db, job.id)
            assert (migrated.status, migrated.stage, migrated.extraction_id) == (
                "queued",
                "pdf_analysis.v1",
                extraction.id,
            )
            assert migrated.generation == old_generation + 1
            assert migrated.attempts == 0

            await processor.run_message(queue.messages[0])
            finished = await row(db, job.id)
            assert (finished.status, finished.attempts, finished.extraction_id) == (
                "analysis_ready",
                1,
                extraction.id,
            )
            assert provider.calls == 1
            async with db.transaction() as repos:
                analyses = await repos.analyses.list(owner_id=owner.id)
                assert len(analyses) == 1
                assert analyses[0].job_id == job.id
                assert analyses[0].extraction_id == extraction.id
                result = await repos.analyses.read(analyses[0].id, owner_id=owner.id)
                assert result.publication == "published"
        finally:
            await db.close()

    asyncio.run(scenario())
