"""An owner requests saved actions; only the trusted operator publishes results."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.errors import PersistenceConflict, RecordNotFound
from buddy_api.preflight.contracts import PreflightRequest, PreflightResponse
from buddy_api.preflight.service import PreflightService
from buddy_api.uploads.contracts import UploadError


def service(request: Request) -> PreflightService:
    value: PreflightService | None = request.app.state.preflight
    if value is None:
        raise UploadError(503, "preflight_unavailable", "Preflight storage is not configured.")
    return value


def preflight_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["preflight"],
        responses={code: {"model": ErrorResponse} for code in (401, 404, 409, 422, 429, 503)},
    )

    @router.post(
        "/analyses/{analysis_id}/preflights", response_model=PreflightResponse, status_code=202
    )
    async def create(
        analysis_id: UUID,
        values: PreflightRequest,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
        idempotency_key: Annotated[UUID, Header()],
    ) -> PreflightResponse:
        if len(request.headers.getlist("idempotency-key")) != 1:
            raise UploadError(422, "invalid_retry_key", "Send one Idempotency-Key header.")
        response.headers["Cache-Control"] = "no-store"
        try:
            result = await service(request).request(
                principal.id, analysis_id, idempotency_key, values
            )
        except RecordNotFound:
            raise UploadError(404, "not_found", "Saved analysis not found.") from None
        except PersistenceConflict:
            raise UploadError(409, "stale_analysis", "Refresh the saved analysis.") from None
        return PreflightResponse(data=result, request_id=request.state.request_id)

    @router.get("/preflights/{identifier}", response_model=PreflightResponse)
    async def read(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> PreflightResponse:
        response.headers["Cache-Control"] = "no-store"
        return PreflightResponse(
            data=service(request).public(await service(request).owned(principal.id, identifier)),
            request_id=request.state.request_id,
        )

    @router.get("/preflights/{identifier}/evidence/{artifact}", response_class=Response)
    async def evidence(
        identifier: UUID,
        artifact: UUID,
        request: Request,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> Response:
        content, mime = await service(request).evidence(principal.id, identifier, artifact)
        return Response(
            content,
            media_type=mime,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "attachment",
            },
        )

    return router
