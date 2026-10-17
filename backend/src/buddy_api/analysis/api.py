"""Owner-authorized typed analysis reads. Validation is never a public assertion."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from buddy_api.analysis.contracts import AnalysisResult, Model
from buddy_api.analysis.store import Analyses
from buddy_api.auth import Principal, authenticated
from buddy_api.authorization import ResourceKind, owned_resource
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.errors import PersistenceError


class AnalysisResponse(Model):
    data: AnalysisResult
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
                return AnalysisResponse(data=result, request_id=request.state.request_id)
        except (PersistenceError, SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None

    return router
