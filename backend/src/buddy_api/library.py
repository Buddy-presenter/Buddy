"""Owner-scoped deck discovery backed by durable deck and processing records."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.models import AnalysisVersion, Deck, DeckExtraction, ProcessingJob
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.router import service
from buddy_api.uploads.service import blocking


class DeckStatus(StrEnum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    FAILED = "failed"
    READY = "ready"
    INCOMPLETE = "incomplete"


class ProcessingState(StrEnum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    INSPECTING = "inspecting_pdf_metadata"
    ANALYZING = "analyzing"
    VALIDATING = "validating"
    REVIEW_READY = "review_ready"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LibraryProcessing(BaseModel):
    job_id: UUID | None = None
    state: ProcessingState
    stage: str | None = None
    progress: int = Field(ge=0, le=100)
    attempts: int = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=64)
    can_retry: bool = False
    can_cancel: bool = False


class LibrarySort(StrEnum):
    NEWEST = "newest"
    OLDEST = "oldest"
    TITLE_ASC = "title_asc"
    TITLE_DESC = "title_desc"


class LibraryDeck(BaseModel):
    id: UUID
    title: str = Field(min_length=1, max_length=256)
    created_at: datetime
    status: DeckStatus
    attempts: int = Field(ge=0)
    source_format: Literal["pptx", "pdf"] = "pptx"
    can_present: bool = False
    processing: LibraryProcessing | None = None


class LibraryData(BaseModel):
    items: list[LibraryDeck]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=50)


class LibraryResponse(BaseModel):
    data: LibraryData
    request_id: str


def library_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/decks",
        tags=["library"],
        responses={
            **{code: {"model": ErrorResponse} for code in (401, 429, 503)},
            422: {"model": ErrorResponse, "description": "Request validation failed."},
        },
    )

    @router.get("", response_model=LibraryResponse)
    async def listing(
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
        search: Annotated[str, Query(max_length=256)] = "",
        sort: LibrarySort = LibrarySort.NEWEST,
        page: Annotated[int, Query(ge=1, le=100000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> LibraryResponse:
        response.headers["Cache-Control"] = "no-store"
        filters = [Deck.owner_id == principal.id]
        if search.strip():
            # Escape LIKE metacharacters: user searches are literal substrings, not patterns.
            term = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(Deck.title.ilike("%" + term + "%", escape="\\"))
        latest_job = (
            select(ProcessingJob.id)
            .where(ProcessingJob.deck_id == Deck.id, ProcessingJob.owner_id == principal.id)
            .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
            .limit(1)
            .correlate(Deck)
            .scalar_subquery()
        )
        current_analysis = (
            select(AnalysisVersion.id)
            .where(AnalysisVersion.deck_id == Deck.id, AnalysisVersion.owner_id == principal.id)
            .order_by(AnalysisVersion.version.desc())
            .limit(1)
            .correlate(Deck)
            .scalar_subquery()
        )
        current_extraction = (
            select(AnalysisVersion.extraction_id)
            .where(AnalysisVersion.id == current_analysis)
            .correlate(Deck)
            .scalar_subquery()
        )
        analysis_source_revision = (
            select(DeckExtraction.source_revision)
            .where(DeckExtraction.id == current_extraction)
            .correlate(Deck)
            .scalar_subquery()
        )
        analysis_source_sha256 = (
            select(DeckExtraction.source_sha256)
            .where(DeckExtraction.id == current_extraction)
            .correlate(Deck)
            .scalar_subquery()
        )
        analysis_source_format = (
            select(DeckExtraction.document["source_format"].as_string())
            .where(DeckExtraction.id == current_extraction)
            .correlate(Deck)
            .scalar_subquery()
        )
        order = {
            LibrarySort.NEWEST: Deck.created_at.desc(),
            LibrarySort.OLDEST: Deck.created_at.asc(),
            LibrarySort.TITLE_ASC: func.lower(Deck.title).asc(),
            LibrarySort.TITLE_DESC: func.lower(Deck.title).desc(),
        }[sort]
        query = (
            select(
                Deck,
                ProcessingJob.id,
                ProcessingJob.status,
                ProcessingJob.attempts,
                ProcessingJob.stage,
                ProcessingJob.progress,
                ProcessingJob.error_code,
                ProcessingJob.source_revision,
                AnalysisVersion.job_id,
                AnalysisVersion.publication,
                analysis_source_revision,
                analysis_source_sha256,
                analysis_source_format,
            )
            .outerjoin(ProcessingJob, ProcessingJob.id == latest_job)
            .outerjoin(AnalysisVersion, AnalysisVersion.id == current_analysis)
            .where(*filters)
            .order_by(order, Deck.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        try:
            async with request.app.state.database.sessions() as session, session.begin():
                # Count and rows share a snapshot even while upload/processing writes occur.
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                total = await session.scalar(select(func.count()).select_from(Deck).where(*filters))
                records = (await session.execute(query)).all()
                items = []
                worker_settings = request.app.state.worker_settings
                for (
                    deck,
                    job_id,
                    job_status,
                    attempts,
                    job_stage,
                    progress,
                    error_code,
                    job_revision,
                    analysis_job_id,
                    publication,
                    analysis_revision,
                    analysis_sha256,
                    analysis_format,
                ) in records:
                    analysis_matches_source = bool(
                        analysis_revision == deck.source_revision
                        and analysis_sha256 == deck.source_sha256
                        and analysis_format == deck.source_format
                    )
                    source_matches = bool(
                        job_id and job_revision and job_revision == deck.source_revision
                    )
                    has_analysis = (
                        source_matches
                        and analysis_job_id == job_id
                        and publication == "published"
                        and analysis_matches_source
                    )
                    # RenderedAsset currently has no slide identity, render revision, order,
                    # or checksum. Neither its presence nor a matching row count can prove
                    # that every raster in this analysis's manifest exists. Fail closed
                    # until the renderer persists that complete, revision-bound evidence.
                    has_render_proof = False
                    status = {
                        None: DeckStatus.UPLOADED,
                        "pending": DeckStatus.QUEUED,
                        "running": DeckStatus.PROCESSING,
                        "queued": DeckStatus.QUEUED,
                        "retry_wait": DeckStatus.QUEUED,
                        "metadata_ready": DeckStatus.INCOMPLETE,
                        "analysis_partial": DeckStatus.INCOMPLETE,
                        "analysis_ready": (
                            DeckStatus.READY
                            if has_analysis and has_render_proof
                            else DeckStatus.INCOMPLETE
                        ),
                        "cancelled": DeckStatus.INCOMPLETE,
                        "dead_letter": DeckStatus.FAILED,
                        "failed": DeckStatus.FAILED,
                        "succeeded": (
                            DeckStatus.READY
                            if has_analysis and has_render_proof
                            else DeckStatus.INCOMPLETE
                        ),
                    }[job_status]
                    processing_state = {
                        None: ProcessingState.UPLOADED,
                        "pending": ProcessingState.QUEUED,
                        "queued": ProcessingState.QUEUED,
                        "retry_wait": ProcessingState.QUEUED,
                        "running": {
                            "pdf_metadata.v1": ProcessingState.INSPECTING,
                            "pptx_metadata.v1": ProcessingState.INSPECTING,
                            "pptx_analysis.v1": ProcessingState.ANALYZING,
                            "pdf_render": ProcessingState.ANALYZING,
                            "provider": ProcessingState.ANALYZING,
                            "pdf_analysis.v1": ProcessingState.ANALYZING,
                            "validation": ProcessingState.VALIDATING,
                        }.get(job_stage, ProcessingState.ANALYZING),
                        "analysis_ready": ProcessingState.REVIEW_READY,
                        "succeeded": ProcessingState.REVIEW_READY,
                        "metadata_ready": ProcessingState.FAILED,
                        "analysis_partial": ProcessingState.PARTIAL,
                        "dead_letter": ProcessingState.FAILED,
                        "failed": ProcessingState.FAILED,
                        "cancelled": ProcessingState.CANCELLED,
                    }[job_status]
                    if processing_state == ProcessingState.REVIEW_READY and not has_analysis:
                        processing_state = ProcessingState.FAILED
                    retryable = job_status in {
                        "metadata_ready",
                        "dead_letter",
                        "failed",
                        "cancelled",
                    }
                    items.append(
                        LibraryDeck(
                            id=deck.id,
                            title=deck.title,
                            created_at=deck.created_at,
                            status=status,
                            attempts=attempts or 0,
                            source_format=deck.source_format,
                            can_present=deck.source_format == "pdf"
                            and bool(deck.source_version_id),
                            processing=LibraryProcessing(
                                job_id=job_id,
                                state=processing_state,
                                stage=job_stage,
                                progress=progress or 0,
                                attempts=attempts or 0,
                                error_code=(
                                    "analysis_unavailable"
                                    if job_status in {"analysis_ready", "succeeded"}
                                    and not has_analysis
                                    else error_code
                                ),
                                can_retry=bool(
                                    retryable
                                    and worker_settings is not None
                                    and attempts < worker_settings.max_attempts
                                    and job_revision == deck.source_revision
                                ),
                                can_cancel=job_status in {"queued", "retry_wait", "running"}
                                and source_matches,
                            ),
                        )
                    )
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None
        return LibraryResponse(
            data=LibraryData(items=items, total=total or 0, page=page, page_size=page_size),
            request_id=request.state.request_id,
        )

    async def download_source(
        identifier: UUID,
        request: Request,
        principal: Principal,
        source_format: Literal["pdf", "pptx"],
    ) -> Response:
        uploads = service(request)
        async with uploads.transaction() as session:
            deck = await session.scalar(
                select(Deck).where(Deck.id == identifier, Deck.owner_id == principal.id)
            )
            if deck is None:
                raise HTTPException(404)
            if deck.source_format != source_format:
                raise UploadError(
                    409,
                    "presentation_unsupported",
                    "The requested source format does not match this presentation.",
                )
            key, version, size, digest, revision = (
                deck.source_key,
                deck.source_version_id,
                deck.source_size_bytes,
                deck.source_sha256,
                deck.source_revision,
            )
            if not key or not version or not size or not digest or not revision:
                raise HTTPException(404)
        # Authorization and immutable source pointers are captured above. Release
        # the connection before waiting on storage or the bounded parser slot;
        # concurrent slow downloads must not exhaust the API database pool.
        async with uploads.parser_slot:
            content = await blocking(
                lambda: uploads.storage.read(
                    key,
                    version=version,
                    size=size,
                    sha256=digest,
                    source_format=source_format,
                )
            )
        return Response(
            content,
            media_type="application/pdf"
            if source_format == "pdf"
            else "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Source-Sha256": digest,
                "X-Source-Revision": revision,
                "X-Source-Format": source_format,
                "Content-Disposition": f'attachment; filename="presentation.{source_format}"',
            },
        )

    @router.get(
        "/{identifier}/source",
        response_class=Response,
        responses={
            200: {
                "content": {"application/pdf": {}},
                "description": "Owner-only original PDF bytes.",
            }
        },
    )
    async def source(
        identifier: UUID, request: Request, principal: Annotated[Principal, Depends(authenticated)]
    ) -> Response:
        return await download_source(identifier, request, principal, "pdf")

    @router.get(
        "/{identifier}/powerpoint-source",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {}
                },
                "description": "Owner-only immutable original PowerPoint source.",
            }
        },
    )
    async def powerpoint_source(
        identifier: UUID, request: Request, principal: Annotated[Principal, Depends(authenticated)]
    ) -> Response:
        return await download_source(identifier, request, principal, "pptx")

    return router
