"""Owner-authorized job inspection, bounded retry and cancellation."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from buddy_api.analysis.provider import ModelSummary, parse_model_output
from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.models import Deck, PDFProcessingCheckpoint, ProcessingJob
from buddy_api.processing import store
from buddy_api.processing.contracts import JobResponse, PDFPageSummary, PDFProcessingSummaries
from buddy_api.uploads.contracts import UploadError


def processing_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["processing"],
        responses={code: {"model": ErrorResponse} for code in (401, 404, 409, 429, 503)},
    )

    async def operate(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Principal,
        operation: str = "read",
    ) -> JobResponse:
        response.headers["Cache-Control"] = "no-store"
        database = request.app.state.database
        settings = request.app.state.worker_settings
        if database is None:
            raise UploadError(503, "processing_unavailable", "Processing is unavailable.")
        try:
            async with database.sessions() as session, session.begin():
                if operation == "retry":
                    await store.admission_lock(session)
                job = await store.owned(session, principal.id, identifier)
                if operation == "cancel":
                    await store.cancel(session, job)
                elif operation == "retry":
                    if settings is None:
                        raise UploadError(
                            503, "processing_unavailable", "Processing is unavailable."
                        )
                    await store.retry(session, job, settings)
                await session.flush()
                return JobResponse(data=store.describe(job), request_id=request.state.request_id)
        except (SQLAlchemyError, OSError, TimeoutError):
            raise UploadError(
                503, "processing_unavailable", "Processing is temporarily unavailable.", 15
            ) from None

    @router.get("/decks/{identifier}/job", response_model=JobResponse)
    async def latest(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> JobResponse:
        database = request.app.state.database
        if database is None:
            raise UploadError(503, "processing_unavailable", "Processing is unavailable.")
        try:
            async with database.sessions() as session:
                job_id = await session.scalar(
                    select(ProcessingJob.id)
                    .where(
                        ProcessingJob.deck_id == identifier,
                        ProcessingJob.owner_id == principal.id,
                    )
                    .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
                    .limit(1)
                )
            if job_id is None:
                raise UploadError(404, "not_found", "Job not found.")
            return await operate(job_id, request, response, principal)
        except (SQLAlchemyError, OSError, TimeoutError):
            raise UploadError(
                503, "processing_unavailable", "Processing is temporarily unavailable.", 15
            ) from None

    @router.get("/jobs/{identifier}/pdf-summaries", response_model=PDFProcessingSummaries)
    async def pdf_summaries(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> PDFProcessingSummaries:
        response.headers["Cache-Control"] = "no-store"
        if request.app.state.database is None:
            raise UploadError(503, "processing_unavailable", "Processing is unavailable.")
        try:
            async with request.app.state.database.sessions() as session, session.begin():
                job = await store.owned(session, principal.id, identifier)
                row = await session.get(PDFProcessingCheckpoint, identifier)
                deck = await session.get(Deck, job.deck_id)
                if row is None:
                    raise UploadError(409, "summaries_pending", "PDF summaries are not saved yet.")
                if (
                    deck is None
                    or deck.source_format != "pdf"
                    or deck.source_revision != row.source_revision
                    or deck.source_sha256 != row.source_sha256
                    or row.owner_id != principal.id
                    or row.deck_id != job.deck_id
                    or row.extraction_id != job.extraction_id
                ):
                    raise UploadError(
                        409, "source_changed", "Saved PDF summaries are no longer current."
                    )
                generated = parse_model_output(row.generated)
                return PDFProcessingSummaries(
                    job_id=job.id,
                    source_revision=row.source_revision,
                    presentation=generated.presentation,
                    slides=tuple(
                        PDFPageSummary(
                            number=s.number, summary=ModelSummary(text=s.text, reason=s.reason)
                        )
                        for s in generated.slides
                    ),
                    grounded_pages=tuple(sorted(int(n) for n in row.pages)),
                )
        except (SQLAlchemyError, OSError, TimeoutError, ValueError):
            raise UploadError(
                503, "processing_unavailable", "Saved PDF summaries are temporarily unavailable."
            ) from None

    @router.get("/jobs/{identifier}", response_model=JobResponse)
    async def read(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> JobResponse:
        return await operate(identifier, request, response, principal)

    @router.post("/jobs/{identifier}/cancel", response_model=JobResponse)
    async def cancel(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> JobResponse:
        return await operate(identifier, request, response, principal, "cancel")

    @router.post("/jobs/{identifier}/retry", response_model=JobResponse)
    async def retry(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> JobResponse:
        return await operate(identifier, request, response, principal, "retry")

    return router
