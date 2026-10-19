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
from buddy_api.persistence.models import AnalysisVersion, Deck, ProcessingJob
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
                AnalysisVersion.job_id,
                AnalysisVersion.publication,
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
                for deck, job_id, job_status, attempts, analysis_job_id, publication in records:
                    has_analysis = analysis_job_id == job_id and publication == "published"
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
                        )
                    )
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None
        return LibraryResponse(
            data=LibraryData(items=items, total=total or 0, page=page, page_size=page_size),
            request_id=request.state.request_id,
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
        uploads = service(request)
        async with uploads.transaction() as session:
            deck = await session.scalar(
                select(Deck).where(Deck.id == identifier, Deck.owner_id == principal.id)
            )
            if deck is None:
                raise HTTPException(404)
            if deck.source_format != "pdf":
                raise UploadError(
                    409,
                    "presentation_unsupported",
                    "PPTX presentation is not supported yet. Export to PDF and upload it.",
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
            async with uploads.parser_slot:
                content = await blocking(
                    lambda: uploads.storage.read(
                        key,
                        version=version,
                        size=size,
                        sha256=digest,
                        source_format=deck.source_format,
                    )
                )
        return Response(
            content,
            media_type="application/pdf",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Source-Sha256": digest,
                "X-Source-Revision": revision,
                "Content-Disposition": 'attachment; filename="presentation.pdf"',
            },
        )

    return router
