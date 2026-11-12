"""Authenticated ordered notifications and version-checked player commands.

The database, rather than a process-local broker, is the ordering boundary. Events
are observations: subscribers must never execute a replayed decision notification.
"""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.responses import StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import PresentationSession, SessionEvent
from buddy_api.presentation_sessions import (
    SessionContract,
    SessionSnapshot,
    UpdateSessionState,
    owned_session,
    snapshot,
)

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.5
STREAM_SECONDS = 150
REPLAY_LIMIT = 32
RETAIN_EVENTS = 256
RETENTION_SECONDS = 300
MAX_SEQUENCE = 2**63 - 1


class PlayerStateEvent(SessionContract):
    kind: Literal["player.state"] = "player.state"
    snapshot: SessionSnapshot


class SpeechEvent(SessionContract):
    kind: Literal["speech.transcript"] = "speech.transcript"
    stream_id: UUID
    segment_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    revision: int = Field(ge=0, le=1000000, strict=True)
    text: str = Field(min_length=1, max_length=4096)
    final: bool
    language: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9-]+$")
    start_ms: int = Field(ge=0, le=86400000, strict=True)
    end_ms: int = Field(ge=0, le=86400000, strict=True)

    @model_validator(mode="after")
    def ordered_times(self) -> "SpeechEvent":
        if self.end_ms < self.start_ms:
            raise ValueError("Speech segment end must follow its start.")
        return self


class DecisionEvent(SessionContract):
    kind: Literal["decision.status"] = "decision.status"
    decision_id: UUID
    outcome: Literal["applied", "abstained", "rejected"]
    # Notifications contain identifiers and bounded codes, never arbitrary tool calls.
    action: Literal["next_slide", "previous_slide", "go_to_slide"] | None = None
    reason: Literal["accepted", "uncertain", "stale", "unsupported", "manual_override"]


class EventEnvelope(SessionContract):
    protocol: Literal["session-events.v1"] = "session-events.v1"
    session_id: UUID
    sequence: int = Field(ge=0, le=MAX_SEQUENCE, strict=True)
    state_version: int = Field(ge=0, strict=True)
    created_at: datetime
    payload: Annotated[PlayerStateEvent | SpeechEvent | DecisionEvent, Field(discriminator="kind")]


class PlayerCommand(SessionContract):
    kind: Literal["player.state"]
    command_id: UUID
    sent_at: datetime
    state: UpdateSessionState

    @model_validator(mode="after")
    def aware_time(self) -> "PlayerCommand":
        if self.sent_at.tzinfo is None:
            raise ValueError("A timezone is required.")
        return self


class CommandAcknowledgment(SessionContract):
    command_id: UUID
    sequence: int
    duplicate: bool
    snapshot: SessionSnapshot


async def append_event(
    db: AsyncSession,
    record: PresentationSession,
    payload: PlayerStateEvent | SpeechEvent | DecisionEvent,
    *,
    command: PlayerCommand | None = None,
    purge_history: bool = False,
) -> EventEnvelope:
    """Call inside the session's row-lock transaction, including for server producers.

    Speech and decision producers supply the state version they observed to
    publish_server_event; public clients can publish only PlayerCommand.
    """
    # Allocate before the first flush of a state change. A second UPDATE of the
    # same session in one transaction can make PostgreSQL recheck its deck FK,
    # reversing the deck -> session lock order of a concurrent resume/start.
    record.event_sequence += 1
    now = datetime.now(UTC)
    event = EventEnvelope(
        session_id=record.id,
        sequence=record.event_sequence,
        state_version=record.state_version,
        created_at=now,
        payload=payload,
    )
    db.add(
        SessionEvent(
            session_id=record.id,
            sequence=event.sequence,
            document=event.model_dump(mode="json"),
            expires_at=now + timedelta(seconds=RETENTION_SECONDS),
            command_id=command.command_id if command else None,
            command_sha256=command_digest(command) if command else None,
        )
    )
    await db.execute(
        delete(SessionEvent).where(
            SessionEvent.session_id == record.id,
            SessionEvent.sequence
            <= (
                record.event_sequence - 1
                if purge_history
                else record.event_sequence - RETAIN_EVENTS
            ),
        )
    )
    return event


def command_digest(command: PlayerCommand) -> str:
    return hashlib.sha256(command.model_dump_json().encode()).hexdigest()


async def record_state_event(db: AsyncSession, record: PresentationSession) -> None:
    await append_event(
        db,
        record,
        PlayerStateEvent(snapshot=snapshot(record)),
        purge_history=record.status == "ended",
    )


async def publish_server_event(
    db: AsyncSession,
    principal: Principal,
    identifier: UUID,
    expected_state_version: int,
    payload: SpeechEvent | DecisionEvent,
) -> EventEnvelope:
    """Internal provider/decision boundary; callers own the transaction.

    No HTTP route accepts these payloads. They cannot mutate player state.
    """
    record = await owned_session(db, principal, identifier, lock=True)
    if record.status != "active" or record.state_version != expected_state_version:
        raise HTTPException(409, "The producer's session context is stale.")
    return await append_event(db, record, payload)


async def event_batch(
    request: Request,
    identifier: UUID,
    cursor: int | None,
) -> tuple[list[EventEnvelope], bool]:
    principal = await authenticated(request, request.headers.get("authorization"))
    async with request.app.state.database.sessions() as db, db.begin():
        # Read the sequence and its journal under the same lock as producers. A
        # committed sequence can never be visible without its committed event.
        record = await owned_session(db, principal, identifier, lock=True)
        now = datetime.now(UTC)
        if cursor is not None and cursor > record.event_sequence:
            raise HTTPException(409, "The reconnect cursor is ahead of this session.")
        events = []
        if cursor is not None and cursor < record.event_sequence:
            rows = (
                await db.scalars(
                    select(SessionEvent)
                    .where(
                        SessionEvent.session_id == identifier,
                        SessionEvent.sequence > cursor,
                        SessionEvent.expires_at > now,
                    )
                    .order_by(SessionEvent.sequence)
                    .limit(REPLAY_LIMIT + 1)
                )
            ).all()
            if (
                len(rows) <= REPLAY_LIMIT
                and len(rows) == record.event_sequence - cursor
                and rows
                and rows[0].sequence == cursor + 1
            ):
                events = [EventEnvelope.model_validate(row.document) for row in rows]
        # A missing/expired/overflowed cursor restores one authoritative snapshot.
        # This replaces history; it never asks a client to execute old actions.
        if cursor is None or (cursor != record.event_sequence and not events):
            events = [
                EventEnvelope(
                    session_id=identifier,
                    sequence=record.event_sequence,
                    state_version=record.state_version,
                    created_at=now,
                    payload=PlayerStateEvent(snapshot=snapshot(record)),
                )
            ]
        return events, record.status == "ended"


async def stream_events(
    request: Request,
    identifier: UUID,
    cursor: int | None,
) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STREAM_SECONDS
    logger.info("Session event stream opened")
    try:
        while loop.time() < deadline and not await request.is_disconnected():
            try:
                events, ended = await event_batch(request, identifier, cursor)
            except HTTPException as exc:
                yield f'event: access_closed\ndata: {{"status":{exc.status_code}}}\n\n'
                return
            except (SQLAlchemyError, OSError, TimeoutError):
                yield 'event: unavailable\ndata: {"retry_ms":1000}\n\n'
                return
            if events:
                for event in events:
                    yield (
                        f"id: {event.sequence}\nevent: session_event\ndata: "
                        + event.model_dump_json()
                        + "\n\n"
                    )
                    cursor = event.sequence
                logger.info("Session event batch delivered (%d)", len(events))
            else:
                yield ": heartbeat\n\n"
            if ended:
                return
            await asyncio.sleep(POLL_SECONDS)
    finally:
        logger.info("Session event stream closed")


async def prune_expired_events(database: Database) -> None:
    async with database.sessions() as db, db.begin():
        await db.execute(delete(SessionEvent).where(SessionEvent.expires_at <= datetime.now(UTC)))


async def cleanup_events(database: Database) -> None:
    while True:
        try:
            await prune_expired_events(database)
        except (SQLAlchemyError, OSError, TimeoutError):
            logger.warning("Session event retention cleanup unavailable")
        await asyncio.sleep(30)


def session_event_router() -> APIRouter:
    streams: dict[UUID, int] = {}
    router = APIRouter(
        prefix="/api/v1/sessions",
        tags=["session events"],
        responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 429, 503)},
    )

    @router.get("/{identifier}/sync/snapshot", response_model=EventEnvelope)
    async def read_snapshot(
        request: Request,
        identifier: UUID,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> EventEnvelope:
        """Read current state and the committed event cursor atomically."""
        try:
            batch, _ = await event_batch(request, identifier, None)
            return batch[0]
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    @router.get("/{identifier}/sync/events", response_class=StreamingResponse)
    async def subscribe(
        request: Request,
        identifier: UUID,
        principal: Annotated[Principal, Depends(authenticated)],
        last_event_id: Annotated[int | None, Header(ge=0, le=MAX_SEQUENCE)] = None,
    ) -> StreamingResponse:
        # Preflight returns real HTTP errors before committing stream headers.
        try:
            await event_batch(request, identifier, last_event_id)
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None
        if streams.get(principal.id, 0) >= 2 or sum(streams.values()) >= 128:
            raise HTTPException(429, headers={"Retry-After": "2"})
        streams[principal.id] = streams.get(principal.id, 0) + 1

        async def bounded_stream() -> AsyncIterator[str]:
            try:
                async for frame in stream_events(request, identifier, last_event_id):
                    yield frame
            finally:
                remaining = streams[principal.id] - 1
                if remaining:
                    streams[principal.id] = remaining
                else:
                    del streams[principal.id]

        return StreamingResponse(
            bounded_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.post("/{identifier}/sync/commands", response_model=CommandAcknowledgment)
    async def publish(
        request: Request,
        identifier: UUID,
        command: PlayerCommand,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> CommandAcknowledgment:
        from buddy_api.presentation_sessions import update_session_state

        try:
            async with request.app.state.database.sessions() as db, db.begin():
                record = await owned_session(db, principal, identifier, lock=True)
                receipt = await db.scalar(
                    select(SessionEvent).where(
                        SessionEvent.session_id == identifier,
                        SessionEvent.command_id == command.command_id,
                    )
                )
                if receipt is not None:
                    if receipt.command_sha256 != command_digest(command):
                        raise HTTPException(409, "Command ID was already used with other content.")
                    return CommandAcknowledgment(
                        command_id=command.command_id,
                        sequence=receipt.sequence,
                        duplicate=True,
                        snapshot=snapshot(record),
                    )
                age = (datetime.now(UTC) - command.sent_at).total_seconds()
                if not -5 <= age <= 10:
                    raise HTTPException(409, "The player command is stale; fetch current state.")
                await update_session_state(db, principal, record, command.state)
                event = await append_event(
                    db,
                    record,
                    PlayerStateEvent(snapshot=snapshot(record)),
                    command=command,
                )
                return CommandAcknowledgment(
                    command_id=command.command_id,
                    sequence=event.sequence,
                    duplicate=False,
                    snapshot=snapshot(record),
                )
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    return router


class SessionCommandLimitMiddleware:
    """Bound command JSON before FastAPI parses it, including chunked requests."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        size = 0

        async def bounded() -> Message:
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > 8192:
                raise HTTPException(413)
            return message

        target = (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"].startswith("/api/v1/sessions/")
            and scope["path"].rstrip("/").endswith("/sync/commands")
        )
        await self.app(scope, bounded if target else receive, send)
