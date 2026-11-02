"""Owner-authorized typed analysis reads. Validation is never a public assertion."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from buddy_api.analysis.contracts import AnalysisResult, Model, PowerPointAnalysisResult
from buddy_api.analysis.powerpoint_v1 import (
    PowerPointAnalysisResult as LegacyPowerPointAnalysisResult,
)
from buddy_api.analysis.store import Analyses
from buddy_api.auth import Principal, authenticated
from buddy_api.authorization import ResourceKind, owned_resource
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.errors import PersistenceError, RecordNotFound


class AnalysisResponse(Model):
    data: AnalysisResult
    request_id: str


class PowerPointAnalysisResponse(Model):
    data: LegacyPowerPointAnalysisResult
    request_id: str


class PowerPointAnalysisResponseV2(Model):
    data: PowerPointAnalysisResult
    request_id: str


def analysis_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/analyses",
        tags=["analysis"],
        responses={
            **{code: {"model": ErrorResponse} for code in (401, 403, 404, 503)},
            422: {"model": ErrorResponse, "description": "Request validation failed."},
        },
    )

    @router.get("/{identifier}", response_model=AnalysisResponse)
    async def read(
        request: Request,
        response: Response,
        identifier: UUID,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> AnalysisResponse:
        response.headers["Cache-Control"] = "no-store"
        try:
            async with request.app.state.database.sessions() as session, session.begin():
                # One snapshot prevents publication and validation-history races across reads.
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                await owned_resource(session, principal, ResourceKind.ANALYSES, identifier)
                result = await Analyses(session).read(identifier, owner_id=principal.id)
                if not isinstance(result, AnalysisResult):
                    raise HTTPException(415, "Use the PowerPoint analysis read contract.")
                return AnalysisResponse(data=result, request_id=request.state.request_id)
        except (PersistenceError, SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    @router.get("/by-deck/{deck_id}", response_model=AnalysisResponse)
    async def latest_for_deck(
        deck_id: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> AnalysisResponse:
        """Resolve a deck's latest revision without exposing analysis discovery cross-owner."""
        response.headers["Cache-Control"] = "no-store"
        try:
            async with request.app.state.database.sessions() as session, session.begin():
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                await owned_resource(session, principal, ResourceKind.DECKS, deck_id)
                analyses = Analyses(session)
                try:
                    record = await analyses.latest_for_deck(deck_id, owner_id=principal.id)
                except RecordNotFound:
                    raise HTTPException(404) from None
                result = await analyses.read(record.id, owner_id=principal.id)
                if not isinstance(result, AnalysisResult):
                    raise HTTPException(415, "Use the PowerPoint analysis read contract.")
                return AnalysisResponse(data=result, request_id=request.state.request_id)
        except (PersistenceError, SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    zoom_router = APIRouter(
        prefix="/api/v2/analyses", tags=["analysis"], responses=router.responses
    )

    @zoom_router.get("/powerpoint/by-deck/{deck_id}", response_model=PowerPointAnalysisResponseV2)
    async def powerpoint_for_deck_v2(
        deck_id: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> PowerPointAnalysisResponseV2:
        response.headers["Cache-Control"] = "no-store"
        try:
            async with request.app.state.database.sessions() as session, session.begin():
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                await owned_resource(session, principal, ResourceKind.DECKS, deck_id)
                analyses = Analyses(session)
                try:
                    record = await analyses.latest_for_deck(deck_id, owner_id=principal.id)
                except RecordNotFound:
                    raise HTTPException(404) from None
                result = await analyses.read(record.id, owner_id=principal.id)
                if not isinstance(result, PowerPointAnalysisResult):
                    raise HTTPException(415, "This deck has no PowerPoint analysis.")
                return PowerPointAnalysisResponseV2(
                    data=result, request_id=request.state.request_id
                )
        except (PersistenceError, SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    @router.get("/powerpoint/by-deck/{deck_id}", response_model=PowerPointAnalysisResponse)
    async def powerpoint_for_deck(
        deck_id: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> PowerPointAnalysisResponse:
        result = await powerpoint_for_deck_v2(deck_id, request, response, principal)
        try:
            legacy = LegacyPowerPointAnalysisResult.model_validate(result.data.model_dump())
        except ValueError:
            raise HTTPException(
                409, "Upgrade Buddy to read this analysis with the v2 endpoint."
            ) from None
        return PowerPointAnalysisResponse(data=legacy, request_id=result.request_id)

    root = APIRouter()
    root.include_router(router)
    root.include_router(zoom_router)
    return root
