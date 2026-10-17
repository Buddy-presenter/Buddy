"""Owner-scoped deck discovery backed by durable deck and processing records."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.models import AnalysisVersion, Deck, ProcessingJob, RenderedAsset


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
        analysis = exists().where(
            AnalysisVersion.deck_id == Deck.id,
            AnalysisVersion.owner_id == principal.id,
            AnalysisVersion.job_id == ProcessingJob.id,
            AnalysisVersion.publication == "published",
        )
        assets = exists().where(
            RenderedAsset.deck_id == Deck.id, RenderedAsset.owner_id == principal.id
        )
        order = {
            LibrarySort.NEWEST: Deck.created_at.desc(),
            LibrarySort.OLDEST: Deck.created_at.asc(),
            LibrarySort.TITLE_ASC: func.lower(Deck.title).asc(),
            LibrarySort.TITLE_DESC: func.lower(Deck.title).desc(),
        }[sort]
        query = (
            select(Deck, ProcessingJob.status, ProcessingJob.attempts, analysis, assets)
            .outerjoin(ProcessingJob, ProcessingJob.id == latest_job)
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
                for deck, job_status, attempts, has_analysis, has_assets in records:
                    status = {
                        None: DeckStatus.UPLOADED,
                        "pending": DeckStatus.QUEUED,
                        "running": DeckStatus.PROCESSING,
                        "failed": DeckStatus.FAILED,
                        "succeeded": (
                            DeckStatus.READY
                            if has_analysis and has_assets
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
                        )
                    )
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None
        return LibraryResponse(
            data=LibraryData(items=items, total=total or 0, page=page, page_size=page_size),
            request_id=request.state.request_id,
        )

    return router
