"""Transactional state transitions. Lease tokens fence every output and heartbeat."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from buddy_api.persistence.models import (
    AnalysisVersion,
    Deck,
    DeckExtraction,
    JobOutbox,
    ProcessingJob,
)
from buddy_api.persistence.repositories import Extractions
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.contracts import ACTIVE, JobData, Message, MetadataOutput
from buddy_api.uploads.contracts import UploadError


def now() -> datetime:
    return datetime.now(UTC)


async def admission_lock(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(704008)"))


async def quota(session: AsyncSession, owner: UUID, limit: int) -> None:
    count = await session.scalar(
        select(func.count())
        .select_from(ProcessingJob)
        .where(
            ProcessingJob.owner_id == owner,
            ProcessingJob.source_revision.is_not(None),
            ProcessingJob.status.in_(ACTIVE),
        )
    )
    if int(count or 0) >= limit:
        raise UploadError(429, "job_capacity", "Processing capacity is full; retry later.", 30)


async def schedule(session: AsyncSession, deck: Deck, owner_quota: int = 4) -> ProcessingJob | None:
    if deck.source_format != "pdf":
        return None
    if not all(
        (
            deck.source_key,
            deck.source_revision,
            deck.source_sha256,
            deck.source_version_id,
            deck.source_size_bytes,
        )
    ):
        raise UploadError(409, "source_not_accepted", "An accepted PDF source is required.")
    await admission_lock(session)
    existing = await session.scalar(
        select(ProcessingJob).where(
            ProcessingJob.deck_id == deck.id,
            ProcessingJob.source_revision == deck.source_revision,
        )
    )
    if existing is not None:
        return existing
    await quota(session, deck.owner_id, owner_quota)
    job = ProcessingJob(
        id=uuid4(),
        owner_id=deck.owner_id,
        deck_id=deck.id,
        source_revision=deck.source_revision,
        status="queued",
    )
    session.add(job)
    await session.flush()
    session.add(JobOutbox(job_id=job.id, available_at=now()))
    await session.flush()
    return job


async def owned(session: AsyncSession, owner: UUID, identifier: UUID) -> ProcessingJob:
    job = await session.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.id == identifier,
            ProcessingJob.owner_id == owner,
        )
        .with_for_update()
    )
    if job is None:
        raise UploadError(404, "not_found", "Job not found.")
    return job


def describe(job: ProcessingJob) -> JobData:
    return JobData.model_validate(
        {key: getattr(job, key) for key in JobData.model_fields if key != "analysis_ready"}
    )


async def cancel(session: AsyncSession, job: ProcessingJob) -> None:
    if job.source_revision is None:
        raise UploadError(409, "legacy_job", "Legacy jobs cannot be controlled by this worker.")
    if job.status not in ACTIVE:
        return
    job.status, job.stage_state = "cancelled", "cancelled"
    job.generation += 1
    job.lease_id = job.lease_until = job.deadline_at = None


async def retry(session: AsyncSession, job: ProcessingJob, settings: WorkerSettings) -> None:
    if job.source_revision is None or job.status not in {
        "metadata_ready",
        "dead_letter",
        "failed",
        "cancelled",
    }:
        raise UploadError(409, "job_not_retryable", "This job cannot be retried.")
    # Lifetime attempts remain bounded, even across explicit retries.
    if job.attempts >= settings.max_attempts:
        raise UploadError(409, "attempts_exhausted", "The job attempt budget is exhausted.")
    await quota(session, job.owner_id, settings.owner_quota)
    job.generation += 1
    job.status, job.stage_state, job.progress = "queued", "pending", 0
    job.stage = "pdf_analysis.v1" if job.extraction_id else "pdf_metadata.v1"
    job.error_code = None
    job.not_before = now()
    outbox = await session.get(JobOutbox, job.id)
    assert outbox is not None
    outbox.available_at = now()


async def claim(
    session: AsyncSession, message: Message, settings: WorkerSettings
) -> ProcessingJob | None:
    await admission_lock(session)
    job = await session.scalar(
        select(ProcessingJob).where(ProcessingJob.id == message.job_id).with_for_update()
    )
    if (
        job is None
        or job.generation != message.generation
        or job.status not in {"queued", "retry_wait"}
        or job.source_revision is None
        or job.stage
        not in {"pdf_metadata.v1", "pdf_analysis.v1", "pdf_render", "provider", "validation"}
        or job.not_before > now()
    ):
        return None
    outbox = await session.get(JobOutbox, job.id)
    if outbox is None:
        return None
    running = await session.scalar(
        select(func.count())
        .select_from(ProcessingJob)
        .where(
            ProcessingJob.status == "running",
            ProcessingJob.source_revision.is_not(None),
            ProcessingJob.lease_until > now(),
        )
    )
    if int(running or 0) >= settings.concurrency:
        return None
    job.attempts += 1
    if job.attempts > settings.max_attempts:
        job.status, job.stage_state, job.error_code = "dead_letter", "failed", "attempts_exhausted"
        return None
    job.status, job.stage_state, job.progress = "running", "running", max(job.progress, 10)
    job.lease_id = uuid4()
    job.lease_until = now() + timedelta(seconds=settings.lease_seconds)
    job.deadline_at = now() + timedelta(seconds=settings.runtime_seconds)
    await session.flush()
    return job


def valid_lease(job: ProcessingJob, token: UUID) -> bool:
    return bool(
        job.status == "running"
        and job.lease_id == token
        and job.lease_until
        and job.lease_until > now()
        and job.deadline_at
        and job.deadline_at > now()
    )


async def heartbeat(
    session: AsyncSession, identifier: UUID, token: UUID, settings: WorkerSettings
) -> bool:
    job = await session.scalar(
        select(ProcessingJob).where(ProcessingJob.id == identifier).with_for_update()
    )
    if job is None or not valid_lease(job, token):
        return False
    job.lease_until = min(now() + timedelta(seconds=settings.lease_seconds), job.deadline_at)  # type: ignore[type-var]
    return True


async def advance_stage(
    session: AsyncSession,
    identifier: UUID,
    token: UUID,
    stage: str,
    progress: int,
) -> bool:
    """Persist a truthful, lease-fenced worker stage for library polling."""
    if stage not in {"pdf_render", "provider", "validation"} or not 0 <= progress <= 95:
        raise ValueError("Invalid processing stage")
    job = await session.scalar(
        select(ProcessingJob).where(ProcessingJob.id == identifier).with_for_update()
    )
    if job is None or not valid_lease(job, token):
        return False
    job.stage, job.stage_state = stage, "running"
    job.progress = max(job.progress, progress)
    return True


async def save_metadata(
    session: AsyncSession, identifier: UUID, token: UUID, output: MetadataOutput
) -> UUID | None:
    job = await session.scalar(
        select(ProcessingJob).where(ProcessingJob.id == identifier).with_for_update()
    )
    if job is None or not valid_lease(job, token):
        return None
    deck = await session.get(Deck, job.deck_id)
    metadata = output.metadata
    if (
        deck is None
        or deck.owner_id != job.owner_id
        or deck.source_format != "pdf"
        or deck.source_revision != job.source_revision
        or metadata.source_revision != job.source_revision
        or metadata.source_sha256 != deck.source_sha256
        or metadata.source_format != "pdf"
    ):
        raise UploadError(409, "source_changed", "The job source is no longer current.")
    extraction = await Extractions(session, DeckExtraction).save(
        owner_id=job.owner_id, deck_id=job.deck_id, extraction=metadata
    )
    job.extraction_id = extraction.id
    job.stage, job.stage_state, job.progress, job.error_code = (
        "pdf_analysis.v1",
        "running",
        35,
        None,
    )
    return extraction.id


async def complete(session: AsyncSession, identifier: UUID, token: UUID, analysis_id: UUID) -> bool:
    job = await session.scalar(
        select(ProcessingJob).where(ProcessingJob.id == identifier).with_for_update()
    )
    if job is None or not valid_lease(job, token):
        return False
    deck = await session.get(Deck, job.deck_id)
    analysis = await session.scalar(
        select(AnalysisVersion).where(
            AnalysisVersion.id == analysis_id,
            AnalysisVersion.job_id == job.id,
            AnalysisVersion.deck_id == job.deck_id,
            AnalysisVersion.owner_id == job.owner_id,
        )
    )
    if (
        deck is None
        or deck.owner_id != job.owner_id
        or deck.source_format != "pdf"
        or deck.source_revision != job.source_revision
        or analysis is None
        or analysis.extraction_id != job.extraction_id
        or analysis.publication not in {"published", "partial"}
    ):
        raise UploadError(409, "analysis_incomplete", "The analysis is not ready for completion.")
    job.status = "analysis_partial" if analysis.publication == "partial" else "analysis_ready"
    job.stage_state, job.progress, job.error_code = "succeeded", 100, None
    job.lease_id = job.lease_until = job.deadline_at = None
    return True


async def fail(
    session: AsyncSession,
    identifier: UUID,
    token: UUID,
    settings: WorkerSettings,
    code: str,
    *,
    permanent: bool = False,
) -> None:
    job = await session.scalar(
        select(ProcessingJob).where(ProcessingJob.id == identifier).with_for_update()
    )
    if job is None or job.status != "running" or job.lease_id != token:
        return
    job.error_code, job.stage_state = code, "failed"
    job.lease_id = job.lease_until = job.deadline_at = None
    job.generation += 1
    job.status = (
        "dead_letter" if permanent or job.attempts >= settings.max_attempts else "retry_wait"
    )
    outbox = await session.get(JobOutbox, job.id)
    assert outbox is not None
    outbox.available_at = now() + timedelta(seconds=min(60, 2**job.attempts))
    job.not_before = outbox.available_at


async def defer(
    session: AsyncSession,
    identifier: UUID,
    token: UUID,
    delay_seconds: int,
) -> None:
    """Release a job while a durable validator reservation is still in flight.

    This is a wait, not a failed processing attempt. Keep the existing extraction and
    analysis so the next delivery resumes at the same idempotent validation request.
    """
    job = await session.scalar(
        select(ProcessingJob).where(ProcessingJob.id == identifier).with_for_update()
    )
    if job is None or job.status != "running" or job.lease_id != token:
        return
    job.attempts = max(0, job.attempts - 1)
    job.generation += 1
    job.status, job.stage_state = "retry_wait", "pending"
    job.error_code = None
    job.lease_id = job.lease_until = job.deadline_at = None
    outbox = await session.get(JobOutbox, job.id)
    assert outbox is not None
    outbox.available_at = now() + timedelta(seconds=max(1, min(delay_seconds, 300)))
    job.not_before = outbox.available_at


async def reconcile(session: AsyncSession, settings: WorkerSettings) -> None:
    # Prior worker versions stopped at metadata_ready. Resume those durable jobs at
    # analysis without changing their owner, source revision or extraction. Analysis gets
    # its own bounded attempt budget because the earlier successful metadata attempt predates it.
    await admission_lock(session)
    active_job = aliased(ProcessingJob)
    active_count = (
        select(func.count())
        .select_from(active_job)
        .where(active_job.owner_id == ProcessingJob.owner_id, active_job.status.in_(ACTIVE))
        .correlate(ProcessingJob)
        .scalar_subquery()
    )
    candidates = (
        select(
            ProcessingJob.id.label("job_id"),
            ProcessingJob.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=ProcessingJob.owner_id,
                order_by=(ProcessingJob.created_at, ProcessingJob.id),
            )
            .label("owner_rank"),
            (settings.owner_quota - active_count).label("available_slots"),
        )
        .where(
            ProcessingJob.source_revision.is_not(None),
            ProcessingJob.extraction_id.is_not(None),
            ProcessingJob.status == "metadata_ready",
        )
        .subquery()
    )
    candidate_ids = (
        await session.scalars(
            select(candidates.c.job_id)
            .where(candidates.c.owner_rank <= candidates.c.available_slots)
            .order_by(candidates.c.created_at, candidates.c.job_id)
            .limit(16)
        )
    ).all()
    metadata_jobs = (
        await session.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.id.in_(candidate_ids))
            .order_by(ProcessingJob.created_at, ProcessingJob.id)
            .with_for_update(skip_locked=True)
        )
    ).all()
    active_counts = dict(
        (
            await session.execute(
                select(ProcessingJob.owner_id, func.count())
                .where(ProcessingJob.status.in_(ACTIVE))
                .group_by(ProcessingJob.owner_id)
            )
        ).all()
    )
    for job in metadata_jobs:
        job.generation += 1
        job.status, job.stage, job.stage_state = "queued", "pdf_analysis.v1", "pending"
        job.progress, job.error_code = 35, None
        job.attempts = 0
        job.lease_id = job.lease_until = job.deadline_at = None
        job.not_before = now()
        outbox = await session.get(JobOutbox, job.id)
        if outbox is None:
            session.add(JobOutbox(job_id=job.id, available_at=job.not_before))
        else:
            outbox.available_at = job.not_before
        active_counts[job.owner_id] = active_counts.get(job.owner_id, 0) + 1

    # Bounded work; expired workers lose their token before another attempt starts.
    jobs = (
        await session.scalars(
            select(ProcessingJob)
            .where(
                ProcessingJob.source_revision.is_not(None),
                ProcessingJob.status == "running",
                (ProcessingJob.lease_until <= now()) | (ProcessingJob.deadline_at <= now()),
            )
            .order_by(ProcessingJob.lease_until)
            .limit(16)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for job in jobs:
        assert job.lease_id is not None
        await fail(session, job.id, job.lease_id, settings, "worker_lease_expired")
