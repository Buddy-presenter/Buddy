"""Persisted publish/reconciliation and isolated PDF execution; no provider analysis."""

import asyncio
import hashlib
import logging
import os
import time
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
    PowerPointAnalysisInput,
    Region,
    RegionParameters,
    SlideOutput,
    Summary,
    ValidationInput,
    digest,
)
from buddy_api.analysis.powerpoint import PowerPointProvider, analysis_document, powerpoint_manifest
from buddy_api.analysis.provider import (
    MAX_FILE_BYTES,
    BedrockProvider,
    ModelOutput,
    ProviderFailure,
)
from buddy_api.analysis.store import Analyses
from buddy_api.extraction.contracts import Extraction, ExtractionError
from buddy_api.observability import emit_metric
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
from buddy_api.processing.contracts import (
    Message,
    MetadataOutput,
    PDFSource,
    PowerPointMetadataOutput,
    PowerPointSource,
)
from buddy_api.processing.queue import Queue
from buddy_api.processing.stage import execute, execute_grounding, execute_manifest
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.service import UploadService, blocking
from buddy_api.uploads.storage import SourceStorage
from buddy_api.validation.config import ValidatorSettings
from buddy_api.validation.contracts import ValidationRequest
from buddy_api.validation.powerpoint import powerpoint_validation_request
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
        inspector: Callable[[Any], MetadataOutput | PowerPointMetadataOutput] | None = None,
        provider: BedrockProvider | None = None,
        powerpoint_provider: PowerPointProvider | None = None,
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
        self.powerpoint_provider = powerpoint_provider or PowerPointProvider(settings.region)
        self.uploads = uploads or UploadService(
            database, storage.settings, storage, owner_job_quota=settings.owner_quota
        )
        self.validation = validation or (
            ValidationService(self.uploads, validator_settings) if validator_settings else None
        )

    @staticmethod
    def _duplicate_candidate_ids(slides: Any) -> set[str]:
        """Keep duplicates visible, but never treat repeated same-region actions as usable."""
        seen: set[tuple[str, str, float, float, float, float]] = set()
        duplicates: set[str] = set()
        for slide in slides:
            for result in slide.actions:
                action = getattr(result, "action", result)
                region = action.params.region
                key = (
                    action.slide_id,
                    action.type,
                    region.x,
                    region.y,
                    region.width,
                    region.height,
                )
                if key in seen:
                    duplicates.add(action.instance_id)
                else:
                    seen.add(key)
        return duplicates

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
                        Deck.source_format.in_(("pdf", "pptx")),
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
                # Commit after send: an ambiguous send can cause a safe duplicate.
                outbox.publish_attempts += 1
                envelope = Message(job_id=job.id, generation=job.generation)
                try:
                    await blocking(partial(self.queue.publish, envelope))
                except Exception:
                    logger.warning("Job publish deferred; outbox retained")
                    # Exponential retry avoids hammering SQS during a sustained outage.
                    retry_seconds = min(900, 30 * (2 ** min(outbox.publish_attempts - 1, 5)))
                    outbox.available_at = store.now() + timedelta(seconds=retry_seconds)
                else:
                    # Reconcile a valid job before its wake-up can expire or reach the DLQ.
                    # This leaves one visibility interval for the dispatcher to republish
                    # after repeated receives, without the old 30-second duplicate churn.
                    outbox.available_at = store.now() + timedelta(
                        seconds=self.settings.queue_reconciliation_seconds
                    )

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
            capacity_deferred = False
            async with self.database.sessions() as session, session.begin():
                current = await session.get(ProcessingJob, message.job_id)
                discard = (
                    current is None
                    or current.generation != message.generation
                    or current.status not in {"queued", "retry_wait"}
                )
                if not discard:
                    capacity_deferred = await store.defer_for_capacity(session, message)
            if discard or capacity_deferred:
                await blocking(lambda: self.queue.acknowledge(raw["ReceiptHandle"]))
            # A capacity miss gets one later outbox wake-up instead of exhausting SQS receives.
            return
        assert job.lease_id is not None
        token = job.lease_id
        stage, saved_extraction_id = job.stage, job.extraction_id
        metadata_stage = "pptx_metadata.v1" if source[5] == "pptx" else "pdf_metadata.v1"
        analysis_stage = "pptx_analysis.v1" if source[5] == "pptx" else "pdf_analysis.v1"
        render_stage = "pptx_inspection" if source[5] == "pptx" else "pdf_render"
        job_started_at = time.perf_counter()
        active_stage = stage
        active_stage_started_at = job_started_at
        environment = os.environ.get("BUDDY_ENVIRONMENT", "development")
        analysis_id_context: UUID | None = None
        analysis_version: int | None = None
        job_correlation = {
            "job_id": str(job.id),
            "deck_id": str(source[6]),
            "source_revision": str(source[4]),
        }

        def correlation_context() -> dict[str, str]:
            result = dict(job_correlation)
            if analysis_id_context is not None:
                result["analysis_id"] = str(analysis_id_context)
            return result

        def stage_metric(name: str, result: str, started_at: float) -> None:
            emit_metric(
                environment=environment,
                service="worker",
                stage=name,
                outcome=result,
                metric="StageOutcome",
                correlation_ids=correlation_context(),
                analysis_version=analysis_version,
            )
            emit_metric(
                environment=environment,
                service="worker",
                stage=name,
                outcome=result,
                metric="DurationMs",
                value=(time.perf_counter() - started_at) * 1000,
                correlation_ids=correlation_context(),
                analysis_version=analysis_version,
            )

        emit_metric(
            environment=environment,
            service="worker",
            stage=stage,
            outcome="success",
            metric="JobAttempt",
            correlation_ids=correlation_context(),
            analysis_version=analysis_version,
        )

        def inspect() -> MetadataOutput | PowerPointMetadataOutput:
            key, version, size, source_digest, revision, source_format, deck_id, _owner_id = source
            if (
                not key
                or not version
                or not size
                or not source_digest
                or not revision
                or source_format not in {"pdf", "pptx"}
            ):
                raise ExtractionError("invalid_pdf_source")
            if source_format == "pdf" and size > MAX_FILE_BYTES:
                raise ProviderFailure("provider_file_limit", permanent=True)
            return self.inspector(
                (PowerPointSource if source_format == "pptx" else PDFSource)(
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
                        logger.warning(
                            "Worker lost its lease during %s for job %s",
                            active_stage,
                            job.id,
                        )
                        if not task.done():
                            task.cancel()
                        with suppress(Exception, asyncio.CancelledError):
                            await task
                        return False, None
                return True, await task
            except asyncio.CancelledError:
                if not task.done():
                    task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await task
                raise

        async def persist_stage(name: str, progress: int) -> bool:
            async with self.database.sessions() as session, session.begin():
                return await store.advance_stage(session, job.id, token, name, progress)

        try:
            key, version, size, source_digest, revision, _format, deck_id, owner_id = source
            manifest: Any
            if not key or not version or not size or not source_digest:
                raise ExtractionError("invalid_pdf_source")
            extraction_id: UUID | None = None
            logger.info("Worker reading pinned presentation source for job %s", job.id)
            alive, content = await fenced(
                blocking(
                    lambda: self.storage.read(
                        key,
                        version=version,
                        size=size,
                        sha256=source_digest,
                        source_format=_format,
                    )
                )
            )
            if not alive:
                return
            logger.info("Worker read pinned presentation source for job %s", job.id)
            if saved_extraction_id is not None:
                active_stage = render_stage
                active_stage_started_at = time.perf_counter()
                async with self.database.sessions() as session:
                    saved = await session.get(DeckExtraction, saved_extraction_id)
                if (
                    saved is None
                    or saved.owner_id != owner_id
                    or saved.deck_id != deck_id
                    or saved.source_revision != revision
                    or saved.source_sha256 != source_digest
                ):
                    raise UploadError(
                        409, "source_changed", "Saved extraction is no longer current."
                    )
                try:
                    extraction = Extraction.model_validate(saved.document)
                except ValueError:
                    raise ExtractionError("saved_extraction_invalid") from None
                render_started_at = time.perf_counter()
                if _format == "pptx":
                    alive, manifest = True, powerpoint_manifest(extraction, str(deck_id))
                else:
                    alive, manifest = await fenced(
                        blocking(
                            partial(
                                execute_manifest,
                                content,
                                extraction,
                                str(deck_id),
                                min(120, self.settings.runtime_seconds - 15),
                            )
                        )
                    )
                if not alive:
                    return
                stage_metric(render_stage, "success", render_started_at)
                if not await persist_stage("provider", 60):
                    return
                active_stage = analysis_stage
                active_stage_started_at = time.perf_counter()
                extraction_id = saved_extraction_id
            else:
                active_stage = metadata_stage
                active_stage_started_at = time.perf_counter()
                inspect_started_at = time.perf_counter()
                alive, output = await fenced(blocking(inspect))
                if not alive:
                    return
                stage_metric(metadata_stage, "success", inspect_started_at)
                async with self.database.sessions() as session, session.begin():
                    extraction_id = await store.save_metadata(session, job.id, token, output)
                if extraction_id is None:
                    return
                extraction, manifest = output.metadata, output.manifest
            if extraction_id is None:
                return

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
                selected_provider = self.powerpoint_provider if _format == "pptx" else self.provider
                analyze_async = getattr(selected_provider, "analyze_async", None)
                provider_call = (
                    analyze_async(content, extraction, str(deck_id), manifest)
                    if analyze_async is not None
                    else blocking(
                        partial(self.provider.analyze, content, extraction, str(deck_id), manifest)
                    )
                )
                active_stage = "provider"
                active_stage_started_at = time.perf_counter()
                if not await persist_stage("provider", 60):
                    return
                alive, generated = await fenced(provider_call)
                if not alive:
                    return
                if _format == "pdf":
                    alive, generated = await fenced(
                        blocking(
                            partial(
                                execute_grounding,
                                content,
                                extraction,
                                manifest,
                                generated,
                                min(120, self.settings.runtime_seconds - 15),
                            )
                        )
                    )
                if not alive:
                    return
                active_stage = analysis_stage
                active_stage_started_at = time.perf_counter()
                document: AnalysisInput | PowerPointAnalysisInput
                if _format == "pptx":
                    document = analysis_document(
                        generated, extraction, manifest, deck_id, job.id, extraction_id
                    )
                else:
                    document = self._analysis_document(
                        generated, extraction, manifest, deck_id, job.id, extraction_id
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
            analysis_id_context = analysis_result.id
            analysis_version = analysis_result.version
            if analysis_hash is None:
                raise ProviderFailure("stored_analysis_unavailable")
            visual_qa_pending = set(analysis_result.visual_qa_pending_instance_ids)
            has_candidates = any(
                action.state == "candidate" and action.action.instance_id not in visual_qa_pending
                for slide in analysis_result.slides
                for action in slide.actions
            )
            if has_candidates and self.validation is None:
                raise ProviderFailure("validator_unavailable")
            if has_candidates and not await persist_stage("validation", 80):
                return
            duplicate_candidate_ids = self._duplicate_candidate_ids(analysis_result.slides)
            for slide in analysis_result.slides:
                for action_result in slide.actions:
                    if action_result.state != "candidate":
                        continue
                    if action_result.action.instance_id in visual_qa_pending:
                        # Geometry inside a flattened image is deliberately kept as a
                        # candidate for #50/#51. Executor success cannot promote it.
                        continue
                    assert self.validation is not None
                    action = action_result.action
                    retry_key = uuid5(analysis_id, "validation:" + action.instance_id)
                    active_stage = "validation"
                    validation_started_at = time.perf_counter()
                    active_stage_started_at = validation_started_at
                    alive, stored = await fenced(
                        self.validation.validate(
                            owner_id,
                            deck_id,
                            retry_key,
                            (
                                powerpoint_validation_request(action, analysis_result.inputs)
                                if _format == "pptx"
                                else ValidationRequest(
                                    action=action.model_dump(mode="json"), profile="wide-1x"
                                )
                            ),
                            source_content=content,
                            precision=("line" if action.type == "highlight_region" else "standard"),
                            duplicate_candidate=action.instance_id in duplicate_candidate_ids,
                        )
                    )
                    if not alive:
                        return
                    stage_metric("validation", "success", validation_started_at)
                    active_stage = analysis_stage
                    active_stage_started_at = time.perf_counter()
                    result = stored.result
                    validation_input = ValidationInput(
                        source_format="pptx" if _format == "pptx" else "pdf",
                        runtime_version=getattr(result, "office_version", None),
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
                completed = await store.complete(session, job.id, token, analysis_id)
            if not completed:
                return
            emit_metric(
                environment=environment,
                service="worker",
                stage=stage,
                outcome="success",
                metric="JobOutcome",
                correlation_ids=correlation_context(),
                analysis_version=analysis_version,
            )
            emit_metric(
                environment=environment,
                service="worker",
                stage=stage,
                outcome="success",
                metric="DurationMs",
                value=(time.perf_counter() - job_started_at) * 1000,
                correlation_ids=correlation_context(),
                analysis_version=analysis_version,
            )
        except asyncio.CancelledError:
            # Shutdown drains local temporary work, then releases the durable lease.
            # Fenced work drains its provider, renderer, or validator child before cleanup.
            async with self.database.sessions() as session, session.begin():
                await store.fail(session, job.id, token, self.settings, "worker_interrupted")
                current = await session.get(ProcessingJob, job.id)
                final_status = current.status if current is not None else "failed"
            outcome = "dead_letter" if final_status == "dead_letter" else "retry"
            emit_metric(
                environment=environment,
                service="worker",
                stage=stage,
                outcome=outcome,
                metric="JobOutcome",
                correlation_ids=correlation_context(),
                analysis_version=analysis_version,
            )
            emit_metric(
                environment=environment,
                service="worker",
                stage=stage,
                outcome=outcome,
                metric="DurationMs",
                value=(time.perf_counter() - job_started_at) * 1000,
                correlation_ids=correlation_context(),
                analysis_version=analysis_version,
            )
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
                current = await session.get(ProcessingJob, job.id)
                final_status = current.status if current is not None else "failed"
            outcome = "dead_letter" if final_status == "dead_letter" else "retry"
            emit_metric(
                environment=environment,
                service="worker",
                stage=stage,
                outcome=outcome,
                metric="JobOutcome",
                correlation_ids=correlation_context(),
                analysis_version=analysis_version,
            )
            emit_metric(
                environment=environment,
                service="worker",
                stage=stage,
                outcome=outcome,
                metric="DurationMs",
                value=(time.perf_counter() - job_started_at) * 1000,
                correlation_ids=correlation_context(),
                analysis_version=analysis_version,
            )
            if active_stage != "provider":
                emit_metric(
                    environment=environment,
                    service="worker",
                    stage=active_stage,
                    outcome="failed",
                    metric="StageOutcome",
                    correlation_ids=correlation_context(),
                    analysis_version=analysis_version,
                )
                emit_metric(
                    environment=environment,
                    service="worker",
                    stage=active_stage,
                    outcome="failed",
                    metric="DurationMs",
                    value=(time.perf_counter() - active_stage_started_at) * 1000,
                    correlation_ids=correlation_context(),
                    analysis_version=analysis_version,
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

        if extraction.source_format != "pdf":
            raise ProviderFailure("pdf_source_mismatch", permanent=True)
        if len(generated.slides) != len(extraction.slides) or len(manifest.slides) != len(
            extraction.slides
        ):
            raise ProviderFailure("provider_slide_order")
        rendered: list[SlideOutput] = []
        visual_qa_pending_instance_ids: list[str] = []
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
                    instance_id = str(
                        uuid5(
                            NAMESPACE_URL,
                            f"buddy:{job_id}:{extraction.source_revision}:{index}:{action_index}",
                        )
                    )
                    actions.append(
                        ContentAction.model_validate(
                            {
                                "version": "1.1",
                                "instance_id": instance_id,
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
                    if candidate.visual_qa_pending:
                        visual_qa_pending_instance_ids.append(instance_id)
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
                visual_qa_pending_instance_ids=tuple(visual_qa_pending_instance_ids),
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
