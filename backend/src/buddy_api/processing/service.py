"""Persisted publish/reconciliation and isolated PDF execution; no provider analysis."""

import asyncio
import hashlib
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from functools import partial
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import exists, func, select

from buddy_api.analysis.contracts import (
    AnalysisInput,
    ContentAction,
    EvidenceReference,
    Region,
    RegionParameters,
    SlideOutput,
    Summary,
    ValidationInput,
    digest,
)
from buddy_api.analysis.provider import (
    MAX_FILE_BYTES,
    BedrockProvider,
    ModelOutput,
    ProviderFailure,
)
from buddy_api.analysis.store import Analyses
from buddy_api.extraction.contracts import Extraction, ExtractionError
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import (
    AnalysisVersion,
    Deck,
    DeckExtraction,
    JobOutbox,
    ProcessingJob,
)
from buddy_api.processing import store
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.contracts import Message, MetadataOutput, PDFSource
from buddy_api.processing.queue import Queue
from buddy_api.processing.stage import execute
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.service import UploadService, blocking
from buddy_api.uploads.storage import SourceStorage
from buddy_api.validation.config import ValidatorSettings
from buddy_api.validation.contracts import ValidationRequest
from buddy_api.validation.service import ValidationService

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
        provider: BedrockProvider | None = None,
        validation: ValidationService | None = None,
        validator_settings: ValidatorSettings | None = None,
        uploads: UploadService | None = None,
    ):
        self.database, self.storage, self.settings, self.queue = database, storage, settings, queue
        self.inspector = inspector or (
            lambda source: execute(
                source, storage.settings, min(120, settings.runtime_seconds - 15)
            )
        )
        self.provider = provider or BedrockProvider(settings.region)
        self.uploads = uploads or UploadService(
            database, storage.settings, storage, owner_job_quota=settings.owner_quota
        )
        self.validation = validation or (
            ValidationService(self.uploads, validator_settings) if validator_settings else None
        )

    async def maintain(self, *, publish: bool = True) -> None:
        async with self.database.sessions() as session, session.begin():
            await store.reconcile(session, self.settings)
        if self.validation is not None:
            await self.validation.reconcile()
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
                    deck.id,
                    deck.owner_id,
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
            key, version, size, source_digest, revision, source_format, deck_id, _owner_id = source
            if (
                not key
                or not version
                or not size
                or not source_digest
                or not revision
                or source_format != "pdf"
            ):
                raise ExtractionError("invalid_pdf_source")
            if size > MAX_FILE_BYTES:
                raise ProviderFailure("provider_file_limit", permanent=True)
            return self.inspector(
                PDFSource(
                    key=key,
                    version=version,
                    size=size,
                    sha256=source_digest,
                    revision=revision,
                    deck_id=deck_id,
                )
            )

        async def fenced(awaitable: Any) -> tuple[bool, Any]:
            task = asyncio.ensure_future(awaitable)
            try:
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=10)
                    if done:
                        break
                    async with self.database.sessions() as session, session.begin():
                        alive = await store.heartbeat(session, job.id, token, self.settings)
                    if not alive:
                        await task
                        return False, None
                return True, await task
            except asyncio.CancelledError:
                if not task.done():
                    task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await task
                raise

        try:
            alive, output = await fenced(blocking(inspect))
            if not alive:
                return
            async with self.database.sessions() as session, session.begin():
                extraction_id = await store.save_metadata(session, job.id, token, output)
            if extraction_id is None:
                return

            key, version, size, source_digest, revision, _format, deck_id, owner_id = source
            if not key or not version or not size or not source_digest:
                raise ExtractionError("invalid_pdf_source")
            content = await blocking(
                lambda: self.storage.read(
                    key,
                    version=version,
                    size=size,
                    sha256=source_digest,
                    source_format="pdf",
                )
            )
            async with self.database.sessions() as session:
                existing = await session.scalar(
                    select(AnalysisVersion).where(
                        AnalysisVersion.job_id == job.id,
                        AnalysisVersion.deck_id == deck_id,
                        AnalysisVersion.owner_id == owner_id,
                    )
                )
            if existing is None:
                if hashlib.sha256(content).hexdigest() != source_digest:
                    raise UploadError(409, "source_mismatch", "Accepted PDF source changed.")
                alive, generated = await fenced(
                    blocking(
                        partial(
                            self.provider.analyze,
                            content,
                            output.metadata,
                            str(deck_id),
                            output.manifest,
                        )
                    )
                )
                if not alive:
                    return
                document = self._analysis_document(
                    generated, output.metadata, output.manifest, deck_id, job.id, extraction_id
                )
                async with self.database.sessions() as session, session.begin():
                    current = await session.scalar(
                        select(ProcessingJob).where(ProcessingJob.id == job.id).with_for_update()
                    )
                    if current is None or not store.valid_lease(current, token):
                        return
                    current_extraction = await session.get(DeckExtraction, extraction_id)
                    if (
                        current_extraction is None
                        or current_extraction.owner_id != owner_id
                        or current_extraction.deck_id != deck_id
                        or current_extraction.source_revision != revision
                    ):
                        raise UploadError(
                            409, "source_changed", "Analysis source is no longer current."
                        )
                    latest = await session.scalar(
                        select(func.max(AnalysisVersion.version)).where(
                            AnalysisVersion.deck_id == deck_id
                        )
                    )
                    analysis = await Analyses(session).create(
                        owner_id=owner_id,
                        deck_id=deck_id,
                        version=int(latest or 0) + 1,
                        document=document,
                    )
                    analysis_id = analysis.id
            else:
                if existing.extraction_id != extraction_id:
                    raise UploadError(409, "source_changed", "Stored analysis is stale.")
                analysis_id = existing.id

            async with self.database.sessions() as session:
                analysis_result = await Analyses(session).read(analysis_id, owner_id=owner_id)
            analysis_hash = analysis_result.analysis_sha256
            if analysis_hash is None:
                raise ProviderFailure("stored_analysis_unavailable")
            has_candidates = any(
                action.state == "candidate"
                for slide in analysis_result.slides
                for action in slide.actions
            )
            if has_candidates and self.validation is None:
                raise ProviderFailure("validator_unavailable")
            for slide in analysis_result.slides:
                for action_result in slide.actions:
                    if action_result.state != "candidate":
                        continue
                    assert self.validation is not None
                    action = action_result.action
                    retry_key = uuid5(analysis_id, "validation:" + action.instance_id)
                    alive, stored = await fenced(
                        self.validation.validate(
                            owner_id,
                            deck_id,
                            retry_key,
                            ValidationRequest(
                                action=action.model_dump(mode="json"), profile="wide-1x"
                            ),
                            source_content=content,
                        )
                    )
                    if not alive:
                        return
                    result = stored.result
                    validation_input = ValidationInput(
                        validation_id=uuid5(analysis_id, "result:" + action.instance_id),
                        analysis_id=analysis_id,
                        analysis_sha256=analysis_hash,
                        instance_id=action.instance_id,
                        action_sha256=digest(action),
                        validator_version=result.validator_version,
                        renderer_version=result.renderer_version,
                        display_profile=result.profile,
                        state=result.state,
                        reasons=tuple(result.reasons),
                        evidence=tuple(
                            EvidenceReference(id=item.id, kind=item.kind, sha256=item.sha256)
                            for item in stored.evidence
                        ),
                    )
                    async with self.database.sessions() as session, session.begin():
                        current = await session.scalar(
                            select(ProcessingJob)
                            .where(ProcessingJob.id == job.id)
                            .with_for_update()
                        )
                        if current is None or not store.valid_lease(current, token):
                            return
                        await Analyses(session).record_validation(
                            owner_id=owner_id, result=validation_input
                        )
            async with self.database.sessions() as session, session.begin():
                await store.complete(session, job.id, token, analysis_id)
        except asyncio.CancelledError:
            # Shutdown drains local temporary work, then releases the durable lease.
            # Fenced work drains its provider, renderer, or validator child before cleanup.
            async with self.database.sessions() as session, session.begin():
                await store.fail(session, job.id, token, self.settings, "worker_interrupted")
            raise
        except Exception as exc:
            if isinstance(exc, UploadError) and exc.code in {
                "validation_pending",
                "cleanup_pending",
            }:
                async with self.database.sessions() as session, session.begin():
                    await store.defer(session, job.id, token, exc.retry_after or 30)
                await blocking(lambda: self.queue.acknowledge(raw["ReceiptHandle"]))
                return
            # Never persist parser/provider exception text or arbitrary codes.
            code = (
                exc.code
                if isinstance(exc, ProviderFailure)
                else "pdf_inspection_failed"
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
                    permanent=(
                        isinstance(exc, ExtractionError)
                        or (isinstance(exc, ProviderFailure) and exc.permanent)
                    ),
                )
        await blocking(lambda: self.queue.acknowledge(raw["ReceiptHandle"]))

    @staticmethod
    def _summary(text: str | None, reason: str | None) -> Summary:
        clean = text.strip() if isinstance(text, str) else None
        why = reason.strip() if isinstance(reason, str) else None
        if clean and why is None:
            return Summary(status="available", text=clean)
        if clean is None and why:
            return Summary(status="unavailable", reason=why)
        raise ProviderFailure("provider_malformed_output")

    @classmethod
    def _analysis_document(
        cls,
        generated: ModelOutput,
        extraction: Extraction,
        manifest: Any,
        deck_id: UUID,
        job_id: UUID,
        extraction_id: UUID,
    ) -> AnalysisInput:
        from uuid import NAMESPACE_URL

        if len(generated.slides) != len(extraction.slides) or len(manifest.slides) != len(
            extraction.slides
        ):
            raise ProviderFailure("provider_slide_order")
        rendered: list[SlideOutput] = []
        for index, (model_slide, source_slide) in enumerate(
            zip(generated.slides, extraction.slides, strict=True)
        ):
            if (
                model_slide.number != index + 1
                or manifest.slides[index].slide_id != source_slide.id
            ):
                raise ProviderFailure("provider_slide_order")
            actions = []
            for action_index, candidate in enumerate(model_slide.actions):
                try:
                    actions.append(
                        ContentAction.model_validate(
                            {
                                "version": "1.1",
                                "instance_id": str(
                                    uuid5(
                                        NAMESPACE_URL,
                                        f"buddy:{job_id}:{extraction.source_revision}:{index}:{action_index}",
                                    )
                                ),
                                "purpose": candidate.purpose.strip(),
                                "deck_id": str(deck_id),
                                "render_id": manifest.render_id,
                                "slide_id": source_slide.id,
                                "type": candidate.type,
                                "params": RegionParameters(
                                    region=Region(
                                        x=candidate.x,
                                        y=candidate.y,
                                        width=candidate.width,
                                        height=candidate.height,
                                    )
                                ).model_dump(mode="json"),
                            }
                        )
                    )
                except ValueError:
                    raise ProviderFailure("provider_invalid_action") from None
            rendered.append(
                SlideOutput(
                    slide_id=source_slide.id,
                    summary=cls._summary(model_slide.text, model_slide.reason),
                    actions=tuple(actions),
                )
            )
        try:
            return AnalysisInput(
                extraction_id=extraction_id,
                source_revision=extraction.source_revision,
                source_sha256=extraction.source_sha256,
                job_id=job_id,
                renderer_version="pdfium-5.14.0-144dpi",
                display_profile="wide-1x",
                validator_version="qt-executor-1",
                manifest=manifest,
                presentation_summary=cls._summary(
                    generated.presentation.text, generated.presentation.reason
                ),
                slides=tuple(rendered),
            )
        except (ValueError, TypeError):
            raise ProviderFailure("provider_invalid_analysis") from None

    async def dispatch(self, *, publish: bool = True) -> None:
        while True:
            try:
                await self.maintain(publish=publish)
            except Exception:
                logger.warning("Job reconciliation deferred; durable state retained")
            await asyncio.sleep(5)
