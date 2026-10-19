"""Persisted publish/reconciliation and isolated PDF execution; no provider analysis."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from functools import partial
from typing import Any

from sqlalchemy import exists, select

from buddy_api.extraction.contracts import ExtractionError
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import Deck, JobOutbox, ProcessingJob
from buddy_api.processing import store
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.contracts import Message, MetadataOutput, PDFSource
from buddy_api.processing.queue import Queue
from buddy_api.processing.stage import execute
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.service import blocking
from buddy_api.uploads.storage import SourceStorage

logger = logging.getLogger(__name__)


class Processor:
    def __init__(
        self,
        database: Database,
        storage: SourceStorage,
        settings: WorkerSettings,
        queue: Queue,
        *,
        inspector: Callable[[PDFSource], MetadataOutput] | None = None,
    ):
        self.database, self.storage, self.settings, self.queue = database, storage, settings, queue
        self.inspector = inspector or (
            lambda source: execute(source, storage.settings, settings.runtime_seconds - 15)
        )

    async def maintain(self, *, publish: bool = True) -> None:
        async with self.database.sessions() as session, session.begin():
            await store.reconcile(session, self.settings)
        # Recover accepted pre-upgrade PDFs and any missing scheduler transaction.
        async with self.database.sessions() as session, session.begin():
            decks = (
                await session.scalars(
                    select(Deck)
                    .where(
                        Deck.source_format == "pdf",
                        Deck.source_version_id.is_not(None),
                        ~exists(
                            select(ProcessingJob.id).where(
                                ProcessingJob.deck_id == Deck.id,
                                ProcessingJob.source_revision == Deck.source_revision,
                            )
                        ),
                    )
                    .order_by(Deck.created_at, Deck.id)
                    .limit(16)
                )
            ).all()
            for deck in decks:
                try:
                    await store.schedule(session, deck, self.settings.owner_quota)
                except UploadError as exc:
                    if exc.code not in {"job_capacity", "source_not_accepted"}:
                        raise
        if not publish:
            return
        for _ in range(16):
            async with self.database.sessions() as session, session.begin():
                job = await session.scalar(
                    select(ProcessingJob)
                    .join(JobOutbox, JobOutbox.job_id == ProcessingJob.id)
                    .where(
                        ProcessingJob.status.in_(("queued", "retry_wait")),
                        JobOutbox.available_at <= store.now(),
                    )
                    .order_by(JobOutbox.available_at)
                    .limit(1)
                    .with_for_update(of=ProcessingJob, skip_locked=True)
                )
                if job is None:
                    return
                outbox = await session.get(JobOutbox, job.id)
                assert outbox is not None
                # Commit after send: lost acknowledgements cause a safe duplicate.
                # Keep a periodic republish until claimed; a DLQ/lost message cannot strand a job.
                outbox.publish_attempts += 1
                envelope = Message(job_id=job.id, generation=job.generation)
                try:
                    await blocking(partial(self.queue.publish, envelope))
                except Exception:
                    logger.warning("Job publish deferred; outbox retained")
                outbox.available_at = store.now() + timedelta(seconds=30)

    async def run_message(self, raw: dict[str, Any]) -> None:
        try:
            message = Message.model_validate_json(raw["Body"])
        except (ValueError, KeyError, TypeError):
            # Poison envelopes remain unacknowledged for SQS's bounded DLQ policy.
            return
        async with self.database.sessions() as session, session.begin():
            job = await store.claim(session, message, self.settings)
            if job is not None:
                deck = await session.get(Deck, job.deck_id)
                assert deck is not None
                source = (
                    deck.source_key,
                    deck.source_version_id,
                    deck.source_size_bytes,
                    deck.source_sha256,
                    deck.source_revision,
                    deck.source_format,
                )
        if job is None:
            async with self.database.sessions() as session:
                current = await session.get(ProcessingJob, message.job_id)
                discard = (
                    current is None
                    or current.generation != message.generation
                    or current.status not in {"queued", "retry_wait"}
                )
            if discard:
                await blocking(lambda: self.queue.acknowledge(raw["ReceiptHandle"]))
            # Capacity misses retain the wakeup; periodic outbox also repairs it.
            return
        assert job.lease_id is not None
        token = job.lease_id

        def inspect() -> MetadataOutput:
            key, version, size, digest, revision, source_format = source
            if (
                not key
                or not version
                or not size
                or not digest
                or not revision
                or source_format != "pdf"
            ):
                raise ExtractionError("invalid_pdf_source")
            return self.inspector(
                PDFSource(key=key, version=version, size=size, sha256=digest, revision=revision)
            )

        task = asyncio.create_task(blocking(inspect))
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=10)
                if done:
                    break
                async with self.database.sessions() as session, session.begin():
                    alive = await store.heartbeat(session, job.id, token, self.settings)
                if not alive:
                    # The bounded parser is drained before temp cleanup; never publish its result.
                    await task
                    return
            output = await task
            async with self.database.sessions() as session, session.begin():
                await store.complete(session, job.id, token, output)
        except asyncio.CancelledError:
            # Shutdown drains local temporary work, then releases the durable lease.
            with suppress(Exception, asyncio.CancelledError):
                await task
            async with self.database.sessions() as session, session.begin():
                await store.fail(session, job.id, token, self.settings, "worker_interrupted")
            raise
        except Exception as exc:
            with suppress(Exception, asyncio.CancelledError):
                await task
            # Never persist parser/provider exception text or arbitrary codes.
            code = (
                "pdf_inspection_failed"
                if isinstance(exc, ExtractionError)
                else "source_unavailable"
                if isinstance(exc, UploadError)
                else "processing_unavailable"
            )
            async with self.database.sessions() as session, session.begin():
                await store.fail(
                    session,
                    job.id,
                    token,
                    self.settings,
                    code,
                    permanent=isinstance(exc, ExtractionError),
                )
        await blocking(lambda: self.queue.acknowledge(raw["ReceiptHandle"]))

    async def dispatch(self, *, publish: bool = True) -> None:
        while True:
            try:
                await self.maintain(publish=publish)
            except Exception:
                logger.warning("Job reconciliation deferred; durable state retained")
            await asyncio.sleep(5)
