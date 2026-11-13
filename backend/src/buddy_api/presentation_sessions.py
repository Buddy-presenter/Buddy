"""Authenticated, durable presentation-session lifecycle and versioned state."""

import logging
from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from buddy_api.analysis.contracts import ValidationInput
from buddy_api.analysis.store import action_is_usable, check_validation_evidence
from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.errors import PersistenceConflict
from buddy_api.persistence.models import (
    ActionValidation,
    AnalysisSlide,
    AnalysisVersion,
    Deck,
    PresentationSession,
    ProcessingJob,
    SlideAction,
)

logger = logging.getLogger(__name__)


class SessionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FocusRegion(SessionContract):
    x: float = Field(ge=0, le=100000, allow_inf_nan=False)
    y: float = Field(ge=0, le=100000, allow_inf_nan=False)
    width: float = Field(gt=0, le=100000, allow_inf_nan=False)
    height: float = Field(gt=0, le=100000, allow_inf_nan=False)

    @model_validator(mode="after")
    def within_coordinate_limit(self) -> "FocusRegion":
        if self.x + self.width > 100000 or self.y + self.height > 100000:
            raise ValueError("Focus region exceeds the supported slide-coordinate limit.")
        return self


class SessionState(SessionContract):
    active_slide_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    focus_view: Literal["slide", "zoom_region", "highlight_region"] = "slide"
    focus_region: FocusRegion | None = None
    autonomy_mode: Literal["manual", "assisted", "autonomous"] = "manual"
    last_accepted_action_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def consistent_focus(self) -> "SessionState":
        if (self.focus_view == "slide") != (self.focus_region is None):
            raise ValueError("Slide focus has no region; focused view requires a region.")
        return self


class StartSession(SessionContract):
    deck_id: UUID


class UpdateSessionState(SessionState):
    expected_state_version: int = Field(ge=0)


class EndSession(SessionContract):
    expected_state_version: int = Field(ge=0)


class SessionSnapshot(SessionContract):
    id: UUID
    owner_id: UUID
    deck_id: UUID
    analysis_version_id: UUID
    status: Literal["created", "active", "ended"]
    state_version: int = Field(ge=0)
    state: SessionState | None
    created_at: datetime


class SessionResponse(SessionContract):
    data: SessionSnapshot
    request_id: str


def snapshot(record: PresentationSession) -> SessionSnapshot:
    state = None
    if record.active_slide_id is not None:
        state = SessionState(
            active_slide_id=record.active_slide_id,
            focus_view=cast(
                Literal["slide", "zoom_region", "highlight_region"],
                record.focus_view,
            ),
            focus_region=(
                FocusRegion.model_validate(record.focus_region)
                if record.focus_region is not None
                else None
            ),
            autonomy_mode=cast(
                Literal["manual", "assisted", "autonomous"],
                record.autonomy_mode,
            ),
            last_accepted_action_id=record.last_accepted_action_id,
        )
    return SessionSnapshot(
        id=record.id,
        owner_id=record.owner_id,
        deck_id=record.deck_id,
        analysis_version_id=record.analysis_version_id,
        status=cast(Literal["created", "active", "ended"], record.status),
        state_version=record.state_version,
        state=state,
        created_at=record.created_at,
    )


async def owned_session(
    db: AsyncSession, principal: Principal, identifier: UUID, *, lock: bool = False
) -> PresentationSession:
    query = (
        select(PresentationSession)
        .join(Deck, Deck.id == PresentationSession.deck_id)
        .where(
            PresentationSession.id == identifier,
            Deck.deletion_requested_at.is_(None),
        )
    )
    if lock:
        # Refresh a session previously read into this AsyncSession's identity map.
        # A concurrent end may commit while resume waits for this row lock.
        # Only lock the session row: locking the joined deck reverses the lock order
        # used by session creation/deletion and can deadlock with concurrent end.
        query = query.with_for_update(of=PresentationSession).execution_options(
            populate_existing=True
        )
    record = await db.scalar(query)
    if record is None:
        raise HTTPException(404)
    if record.owner_id != principal.id:
        raise HTTPException(403)
    return record


async def current_ready_analysis(
    db: AsyncSession, deck: Deck, owner_id: UUID
) -> tuple[AnalysisVersion, list[AnalysisSlide]]:
    """Require a current analysis with a complete slide map for presentation control."""
    if not all(
        (
            deck.source_key,
            deck.source_revision,
            deck.source_sha256,
            deck.source_version_id,
            deck.source_slide_count,
        )
    ):
        raise HTTPException(409, "The deck source is not ready for presentation.")
    job = await db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.deck_id == deck.id, ProcessingJob.owner_id == owner_id)
        .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
        .limit(1)
    )
    analysis = await db.scalar(
        select(AnalysisVersion)
        .where(AnalysisVersion.deck_id == deck.id, AnalysisVersion.owner_id == owner_id)
        .order_by(AnalysisVersion.version.desc(), AnalysisVersion.id.desc())
        .limit(1)
    )
    if job is None or analysis is None:
        raise HTTPException(409, "The deck does not have a current analysis for presentation.")
    published = (
        job.status in {"analysis_ready", "succeeded"} and analysis.publication == "published"
    )
    partial_pdf = (
        deck.source_format == "pdf"
        and job.status == "analysis_partial"
        and analysis.publication in {"partial", "awaiting_validation"}
    )
    if (
        not (published or partial_pdf)
        or job.source_revision != deck.source_revision
        or analysis.job_id != job.id
        or analysis.source_format != deck.source_format
        or analysis.extraction_id is None
    ):
        raise HTTPException(409, "The deck does not have a current analysis for presentation.")
    slides = list(
        (
            await db.scalars(
                select(AnalysisSlide)
                .where(
                    AnalysisSlide.analysis_id == analysis.id,
                    AnalysisSlide.deck_id == deck.id,
                    AnalysisSlide.owner_id == owner_id,
                )
                .order_by(AnalysisSlide.position, AnalysisSlide.id)
            )
        ).all()
    )
    provenance = analysis.provenance or {}
    manifest = provenance.get("manifest")
    manifest_slides = manifest.get("slides") if isinstance(manifest, dict) else None
    slide_ids = [slide.slide_id for slide in slides]
    manifest_ids = [
        item.get("slide_id") for item in manifest_slides or () if isinstance(item, dict)
    ]
    if (
        not slides
        or len(slides) != deck.source_slide_count
        or provenance.get("source_revision") != deck.source_revision
        or provenance.get("source_sha256") != deck.source_sha256
        or not isinstance(manifest, dict)
        or manifest.get("deck_id") != str(deck.id)
        or len(manifest_ids) != len(manifest_slides or ())
        or slide_ids != manifest_ids
    ):
        raise HTTPException(409, "The analysis does not match the current deck source.")
    return analysis, slides


async def validate_action(
    db: AsyncSession,
    analysis: AnalysisVersion,
    owner_id: UUID,
    identifier: str | None,
) -> None:
    if identifier is None:
        return
    action = await db.scalar(
        select(SlideAction).where(
            SlideAction.analysis_id == analysis.id,
            SlideAction.owner_id == owner_id,
            SlideAction.instance_id == identifier,
        )
    )
    if action is None:
        raise HTTPException(422, "The action does not belong to this session analysis.")
    latest = await db.scalar(
        select(ActionValidation)
        .where(ActionValidation.action_id == action.id)
        .where(ActionValidation.analysis_id == analysis.id, ActionValidation.owner_id == owner_id)
        .order_by(ActionValidation.sequence.desc(), ActionValidation.id.desc())
        .limit(1)
    )
    if latest is None or latest.state != "validated" or latest.document is None:
        raise HTTPException(422, "Only validated actions can be accepted into a session.")
    try:
        evidence = ValidationInput.model_validate(latest.document)
        check_validation_evidence(analysis, action, evidence)
    except (PersistenceConflict, ValidationError, ValueError, TypeError):
        raise HTTPException(
            422, "Only current, usable actions can be accepted into a session."
        ) from None
    if latest.state != evidence.state or not action_is_usable(
        analysis.source_format, evidence.state, evidence.semantic_verified
    ):
        raise HTTPException(422, "Only current, usable actions can be accepted into a session.")


async def validate_focus_region(
    db: AsyncSession,
    analysis_id: UUID,
    slide_id: str,
    region: FocusRegion | None,
) -> None:
    if region is None:
        return
    analysis = await db.get(AnalysisVersion, analysis_id)
    manifest = (analysis.provenance or {}).get("manifest") if analysis else None
    manifest_slides = manifest.get("slides") if isinstance(manifest, dict) else None
    slide = next(
        (
            item
            for item in manifest_slides or ()
            if isinstance(item, dict) and item.get("slide_id") == slide_id
        ),
        None,
    )
    source = slide.get("source") if isinstance(slide, dict) else None
    width, height = (
        (source.get("width"), source.get("height")) if isinstance(source, dict) else (None, None)
    )
    if (
        not isinstance(width, int | float)
        or not isinstance(height, int | float)
        or region.x + region.width > width
        or region.y + region.height > height
    ):
        raise HTTPException(422, "The focus region falls outside the active slide.")


def apply_state(record: PresentationSession, state: SessionState) -> None:
    record.active_slide_id = state.active_slide_id
    record.focus_view = state.focus_view
    record.focus_region = state.focus_region.model_dump(mode="json") if state.focus_region else None
    record.autonomy_mode = state.autonomy_mode
    record.last_accepted_action_id = state.last_accepted_action_id


async def find_deck(db: AsyncSession, principal: Principal, identifier: UUID) -> Deck:
    deck = await db.scalar(
        select(Deck)
        .where(Deck.id == identifier, Deck.deletion_requested_at.is_(None))
        .with_for_update()
    )
    if deck is None:
        raise HTTPException(404)
    if deck.owner_id != principal.id:
        raise HTTPException(403)
    return deck


async def update_session_state(
    db: AsyncSession, principal: Principal, record: PresentationSession, values: UpdateSessionState
) -> None:
    """Apply a checked state replacement while the caller holds the session row lock."""
    if record.status != "active":
        raise HTTPException(409, "Only an active session can be updated.")
    if values.expected_state_version != record.state_version:
        raise HTTPException(409, "Session state changed elsewhere. Fetch it before retrying.")
    slide = await db.scalar(
        select(AnalysisSlide.id).where(
            AnalysisSlide.analysis_id == record.analysis_version_id,
            AnalysisSlide.slide_id == values.active_slide_id,
        )
    )
    if slide is None:
        raise HTTPException(422, "The slide does not belong to this session analysis.")
    analysis = await db.get(AnalysisVersion, record.analysis_version_id)
    if analysis is None:
        raise HTTPException(503)
    await validate_focus_region(
        db, record.analysis_version_id, values.active_slide_id, values.focus_region
    )
    await validate_action(db, analysis, principal.id, values.last_accepted_action_id)
    apply_state(record, values)
    record.state_version += 1


def session_router() -> APIRouter:
    from buddy_api.session_events import record_state_event

    router = APIRouter(
        prefix="/api/v1/sessions",
        tags=["presentation sessions"],
        responses={
            **{code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 503)},
        },
    )

    @router.post("", response_model=SessionResponse, status_code=200)
    async def start_or_resume(
        request: Request,
        values: StartSession,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> SessionResponse:
        """Start a session or idempotently resume the current one for this ready deck."""
        try:
            async with request.app.state.database.sessions() as db, db.begin():
                deck = await find_deck(db, principal, values.deck_id)
                analysis, slides = await current_ready_analysis(db, deck, principal.id)
                records = list(
                    (
                        await db.scalars(
                            select(PresentationSession)
                            .where(
                                PresentationSession.deck_id == deck.id,
                                PresentationSession.owner_id == principal.id,
                                PresentationSession.status.in_(("created", "active")),
                            )
                            .order_by(
                                PresentationSession.created_at.desc(), PresentationSession.id.desc()
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                previous_versions = {item.id: item.state_version for item in records}
                current = next(
                    (
                        item
                        for item in records
                        if item.analysis_version_id == analysis.id and item.status == "active"
                    ),
                    None,
                )
                if current is not None:
                    for stale in records:
                        if stale.id != current.id and stale.status == "active":
                            stale.status = "ended"
                            stale.state_version += 1
                    if current.active_slide_id is None:
                        current.active_slide_id = slides[0].slide_id
                        current.state_version += 1
                    record = current
                else:
                    for stale in records:
                        if stale.status == "active":
                            stale.status = "ended"
                            stale.state_version += 1
                    created = next(
                        (item for item in records if item.analysis_version_id == analysis.id), None
                    )
                    if created is not None:
                        created.status = "active"
                        created.state_version += 1
                        if created.active_slide_id is None:
                            created.active_slide_id = slides[0].slide_id
                        record = created
                    else:
                        record = PresentationSession(
                            owner_id=principal.id,
                            deck_id=deck.id,
                            analysis_version_id=analysis.id,
                            status="active",
                            state_version=1,
                            active_slide_id=slides[0].slide_id,
                            focus_view="slide",
                            focus_region=None,
                            autonomy_mode="manual",
                        )
                        db.add(record)
                        await db.flush()
                for changed in records:
                    if changed.state_version != previous_versions[changed.id]:
                        await record_state_event(db, changed)
                if record.id not in previous_versions:
                    await record_state_event(db, record)
                return SessionResponse(data=snapshot(record), request_id=request.state.request_id)
        except HTTPException:
            raise
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            logger.exception(
                "Presentation session start violated a database integrity constraint (%s).",
                constraint or "unknown",
            )
            raise HTTPException(
                409, "The session changed concurrently. Refresh and retry."
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    @router.get("/{identifier}", response_model=SessionResponse)
    async def read(
        request: Request,
        identifier: UUID,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> SessionResponse:
        try:
            async with request.app.state.database.sessions() as db, db.begin():
                record = await owned_session(db, principal, identifier)
                return SessionResponse(data=snapshot(record), request_id=request.state.request_id)
        except HTTPException:
            raise
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    @router.post("/{identifier}/resume", response_model=SessionResponse)
    async def resume(
        request: Request,
        identifier: UUID,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> SessionResponse:
        try:
            async with request.app.state.database.sessions() as db, db.begin():
                observed = await owned_session(db, principal, identifier)
                deck = await find_deck(db, principal, observed.deck_id)
                record = await owned_session(db, principal, identifier, lock=True)
                if record.status == "ended":
                    raise HTTPException(
                        409, "Ended sessions cannot be resumed; start a new session."
                    )
                previous_version = record.state_version
                analysis, slides = await current_ready_analysis(db, deck, principal.id)
                if analysis.id != record.analysis_version_id:
                    raise HTTPException(409, "The deck analysis changed; start a new session.")
                if record.status == "created":
                    record.status = "active"
                    record.state_version += 1
                if record.active_slide_id is None:
                    record.active_slide_id = slides[0].slide_id
                    record.state_version += 1
                if record.state_version != previous_version:
                    await record_state_event(db, record)
                return SessionResponse(data=snapshot(record), request_id=request.state.request_id)
        except HTTPException:
            raise
        except IntegrityError:
            raise HTTPException(
                409, "The session changed concurrently. Refresh and retry."
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    @router.patch("/{identifier}/state", response_model=SessionResponse)
    async def update_state(
        request: Request,
        identifier: UUID,
        values: UpdateSessionState,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> SessionResponse:
        try:
            async with request.app.state.database.sessions() as db, db.begin():
                record = await owned_session(db, principal, identifier, lock=True)
                await update_session_state(db, principal, record, values)
                await record_state_event(db, record)
                return SessionResponse(data=snapshot(record), request_id=request.state.request_id)
        except HTTPException:
            raise
        except IntegrityError:
            raise HTTPException(
                409, "The session changed concurrently. Refresh and retry."
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    @router.post("/{identifier}/end", response_model=SessionResponse)
    async def end(
        request: Request,
        identifier: UUID,
        values: EndSession,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> SessionResponse:
        try:
            async with request.app.state.database.sessions() as db, db.begin():
                record = await owned_session(db, principal, identifier, lock=True)
                if record.status == "ended":
                    return SessionResponse(
                        data=snapshot(record), request_id=request.state.request_id
                    )
                if values.expected_state_version != record.state_version:
                    raise HTTPException(
                        409, "Session state changed elsewhere. Fetch it before retrying."
                    )
                record.status = "ended"
                record.state_version += 1
                await record_state_event(db, record)
                return SessionResponse(data=snapshot(record), request_id=request.state.request_id)
        except HTTPException:
            raise
        except IntegrityError:
            raise HTTPException(
                409, "The session changed concurrently. Refresh and retry."
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    return router
