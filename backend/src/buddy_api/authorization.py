"""Private resource policy shared by HTTP reads/writes and streaming subscriptions."""

import asyncio
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.responses import StreamingResponse

from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.models import (
    AnalysisVersion,
    Deck,
    OwnedRecord,
    PresentationSession,
    ProcessingJob,
    RenderedAsset,
)


class ResourceKind(StrEnum):
    DECKS = "decks"
    ANALYSES = "analyses"
    ASSETS = "assets"
    SESSIONS = "sessions"
    JOBS = "jobs"


MODELS: dict[ResourceKind, type[OwnedRecord]] = {
    ResourceKind.DECKS: Deck,
    ResourceKind.ANALYSES: AnalysisVersion,
    ResourceKind.ASSETS: RenderedAsset,
    ResourceKind.SESSIONS: PresentationSession,
    ResourceKind.JOBS: ProcessingJob,
}


async def owned_resource(
    session: AsyncSession,
    principal: Principal,
    kind: ResourceKind,
    identifier: UUID,
    *,
    lock: bool = False,
) -> OwnedRecord:
    """Owner is the sole supported role. No email, group or client owner-id bypass."""
    query = select(MODELS[kind]).where(MODELS[kind].id == identifier)
    if lock:
        query = query.with_for_update()
    record = await session.scalar(query)
    if record is None:
        raise HTTPException(404)
    if record.owner_id != principal.id:
        raise HTTPException(403)
    return record


class ResourceResponse(BaseModel):
    data: dict[str, Any]
    request_id: str


class ResourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=256)
    status: str | None = Field(default=None, pattern="^(active|ended)$")
    state_version: int | None = Field(default=None, ge=0)


def resource_data(record: OwnedRecord) -> dict[str, Any]:
    data: dict[str, Any] = {"id": str(record.id), "owner_id": str(record.owner_id)}
    if isinstance(record, Deck):
        data["title"] = record.title
    elif isinstance(record, AnalysisVersion):
        # The generic resource route is metadata only; /analyses/{id} owns the typed payload.
        data.update(
            deck_id=str(record.deck_id), version=record.version, publication=record.publication
        )
    elif isinstance(record, RenderedAsset):
        data.update(
            deck_id=str(record.deck_id),
            content_type=record.content_type,
            size_bytes=record.size_bytes,
        )
    elif isinstance(record, PresentationSession):
        data.update(
            deck_id=str(record.deck_id), status=record.status, state_version=record.state_version
        )
    elif isinstance(record, ProcessingJob):
        data.update(deck_id=str(record.deck_id), status=record.status, attempts=record.attempts)
    # Internal storage keys are deliberately never exposed as public URLs.
    return data


async def operate(
    request: Request,
    principal: Principal,
    kind: ResourceKind,
    identifier: UUID,
    *,
    update: ResourceUpdate | None = None,
    delete: bool = False,
) -> ResourceResponse:
    try:
        async with request.app.state.database.sessions() as session, session.begin():
            record = await owned_resource(
                session, principal, kind, identifier, lock=update is not None or delete
            )
            if update is not None:
                if (
                    isinstance(record, Deck)
                    and update.title is not None
                    and update.status is None
                    and update.state_version is None
                ):
                    record.title = update.title
                elif (
                    isinstance(record, PresentationSession)
                    and update.status is not None
                    and update.title is None
                ):
                    if update.state_version != record.state_version or record.status == "ended":
                        raise HTTPException(409)
                    record.status = update.status
                    record.state_version += 1
                else:
                    raise HTTPException(403)
                await session.flush()
            result = ResourceResponse(
                data=resource_data(record), request_id=request.state.request_id
            )
            if delete:
                # Deletion of source bytes/retained backups belongs to #35. Only sessions
                # can be removed here, after they have ended; other records remain immutable.
                if not isinstance(record, PresentationSession) or record.status != "ended":
                    raise HTTPException(409)
                await session.delete(record)
            return result
    except IntegrityError:
        raise HTTPException(409) from None
    except (SQLAlchemyError, OSError, TimeoutError):
        raise HTTPException(503) from None


async def session_events(request: Request, identifier: UUID) -> AsyncIterator[str]:
    # Every snapshot rechecks live revocation and stored ownership, including reconnects.
    # Five-second polls and a 150-second stream bound avoid unbounded buffers/lifetimes.
    for _ in range(30):
        if await request.is_disconnected():
            return
        try:
            principal = await authenticated(request, request.headers.get("authorization"))
            snapshot = await operate(request, principal, ResourceKind.SESSIONS, identifier)
        except HTTPException as exc:
            yield f'event: access_closed\ndata: {{"status":{exc.status_code}}}\n\n'
            return
        yield "event: snapshot\ndata: " + snapshot.model_dump_json() + "\n\n"
        if snapshot.data["status"] == "ended":
            return
        await asyncio.sleep(5)


def private_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["private resources"],
        responses={
            **{code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 429, 503)},
            422: {"model": ErrorResponse, "description": "Request validation failed."},
        },
    )

    @router.get("/resources/{kind}/{identifier}", response_model=ResourceResponse)
    async def read(
        request: Request,
        kind: ResourceKind,
        identifier: UUID,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> ResourceResponse:
        return await operate(request, principal, kind, identifier)

    @router.patch("/resources/{kind}/{identifier}", response_model=ResourceResponse)
    async def update(
        request: Request,
        kind: ResourceKind,
        identifier: UUID,
        values: ResourceUpdate,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> ResourceResponse:
        return await operate(request, principal, kind, identifier, update=values)

    @router.delete("/resources/{kind}/{identifier}", response_model=ResourceResponse)
    async def delete(
        request: Request,
        kind: ResourceKind,
        identifier: UUID,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> ResourceResponse:
        return await operate(request, principal, kind, identifier, delete=True)

    @router.get("/sessions/{identifier}/events", response_class=StreamingResponse)
    async def subscribe(
        request: Request, identifier: UUID, principal: Annotated[Principal, Depends(authenticated)]
    ) -> StreamingResponse:
        await operate(request, principal, ResourceKind.SESSIONS, identifier)
        return StreamingResponse(
            session_events(request, identifier),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return router
