"""PDF-only durable work; raw provider coordinates never become executable actions."""

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from buddy_api.analysis.contracts import Manifest
from buddy_api.analysis.grounding import limit_grounded_actions
from buddy_api.analysis.provider import ModelOutput, ModelSlide, ProviderFailure, parse_model_output
from buddy_api.extraction.contracts import Extraction
from buddy_api.persistence.models import Deck, PDFProcessingCheckpoint, ProcessingJob
from buddy_api.processing import store
from buddy_api.uploads.contracts import UploadError


def binding(extraction: Extraction, manifest: Manifest) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "protocol": "pdf-grounding-checkpoint.v1",
                "extraction": extraction.model_dump(mode="json"),
                "manifest": manifest.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


async def locked_job(session: AsyncSession, identifier: UUID, token: UUID) -> ProcessingJob | None:
    job = await session.scalar(
        select(ProcessingJob).where(ProcessingJob.id == identifier).with_for_update()
    )
    if job is None or not store.valid_lease(job, token):
        return None
    deck = await session.get(Deck, job.deck_id)
    if (
        deck is None
        or deck.deletion_requested_at is not None
        or deck.owner_id != job.owner_id
        or deck.source_format != "pdf"
        or deck.source_revision != job.source_revision
    ):
        raise UploadError(409, "source_changed", "PDF checkpoint source is no longer current.")
    return job


def verify(
    row: PDFProcessingCheckpoint, job: ProcessingJob, extraction: Extraction, manifest: Manifest
) -> None:
    if (
        row.owner_id != job.owner_id
        or row.deck_id != job.deck_id
        or row.extraction_id != job.extraction_id
        or row.source_revision != job.source_revision
        or row.source_sha256 != extraction.source_sha256
        or row.binding != binding(extraction, manifest)
    ):
        raise ProviderFailure("grounding_checkpoint_stale", permanent=True)


async def load(
    session: AsyncSession, identifier: UUID, token: UUID, extraction: Extraction, manifest: Manifest
) -> tuple[ModelOutput, dict[str, Any]] | None:
    job = await locked_job(session, identifier, token)
    if job is None:
        return None
    row = await session.get(PDFProcessingCheckpoint, identifier)
    if row is None:
        return None
    verify(row, job, extraction, manifest)
    generated = parse_model_output(row.generated)
    if [s.number for s in generated.slides] != list(range(1, len(extraction.slides) + 1)):
        raise ProviderFailure("grounding_checkpoint_invalid", permanent=True)
    return generated, dict(row.pages)


async def save_provider(
    session: AsyncSession,
    identifier: UUID,
    token: UUID,
    extraction: Extraction,
    manifest: Manifest,
    generated: ModelOutput,
) -> bool:
    job = await locked_job(session, identifier, token)
    if job is None:
        return False
    existing = await session.get(PDFProcessingCheckpoint, identifier)
    if existing is not None:
        verify(existing, job, extraction, manifest)
        if existing.generated != generated.model_dump(mode="json"):
            raise ProviderFailure("grounding_checkpoint_invalid", permanent=True)
        return True
    session.add(
        PDFProcessingCheckpoint(
            job_id=job.id,
            deck_id=job.deck_id,
            owner_id=job.owner_id,
            extraction_id=job.extraction_id,
            source_revision=extraction.source_revision,
            source_sha256=extraction.source_sha256,
            binding=binding(extraction, manifest),
            generated=generated.model_dump(mode="json"),
            pages={},
        )
    )
    await session.flush()
    return True


async def save_page(
    session: AsyncSession,
    identifier: UUID,
    token: UUID,
    extraction: Extraction,
    manifest: Manifest,
    page: int,
    output: ModelOutput,
) -> bool:
    job = await locked_job(session, identifier, token)
    if job is None:
        return False
    row = await session.get(PDFProcessingCheckpoint, identifier)
    if row is None:
        raise ProviderFailure("grounding_checkpoint_invalid", permanent=True)
    verify(row, job, extraction, manifest)
    if (
        len(output.slides) != 1
        or output.slides[0].number != page
        or not 1 <= page <= len(extraction.slides)
    ):
        raise ProviderFailure("grounding_checkpoint_invalid", permanent=True)
    row.pages = {
        **row.pages,
        str(page): {
            "slide": output.slides[0].model_dump(mode="json"),
            "action_limit_reached": output.action_limit_reached,
        },
    }
    job.stage, job.progress = (
        "grounding",
        max(job.progress, 60 + 19 * len(row.pages) // len(extraction.slides)),
    )
    return True


def assemble(generated: ModelOutput, pages: dict[str, Any]) -> ModelOutput:
    if set(pages) != {str(s.number) for s in generated.slides}:
        raise ProviderFailure("grounding_checkpoint_incomplete")
    slides = tuple(
        ModelSlide.model_validate_json(json.dumps(pages[str(s.number)]["slide"]))
        for s in generated.slides
    )
    if any(
        slide.number != original.number
        or (slide.text, slide.reason) != (original.text, original.reason)
        for slide, original in zip(slides, generated.slides, strict=True)
    ):
        raise ProviderFailure("grounding_checkpoint_invalid", permanent=True)
    return limit_grounded_actions(
        generated.model_copy(
            update={
                "slides": slides,
                "action_limit_reached": generated.action_limit_reached
                or any(p["action_limit_reached"] for p in pages.values()),
            }
        )
    )
