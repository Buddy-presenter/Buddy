"""An owner requests saved actions; only the trusted operator publishes results."""

import base64
import binascii
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.errors import PersistenceConflict, RecordNotFound
from buddy_api.preflight.contracts import (
    PreflightCapture,
    PreflightFailure,
    PreflightRequest,
    PreflightResponse,
    PreflightResult,
    PreflightSnapshotResponse,
    PreflightVerdictResponse,
)
from buddy_api.preflight.service import PreflightService
from buddy_api.uploads.contracts import UploadError


def decode_png(value: str) -> bytes:
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise UploadError(422, "invalid_capture", "Send base64 PNG screenshots.") from None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise UploadError(422, "invalid_capture", "Send base64 PNG screenshots.")
    return data


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

    @router.post("/preflights/{identifier}/claim", response_model=PreflightSnapshotResponse)
    async def claim(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> PreflightSnapshotResponse:
        """The owner's desktop starts capturing a PowerPoint check."""
        response.headers["Cache-Control"] = "no-store"
        try:
            snapshot = await service(request).claim(principal.id, identifier)
        except (RecordNotFound, StopIteration):
            raise UploadError(404, "not_found", "Preflight check not found.") from None
        except PersistenceConflict:
            raise UploadError(409, "stale_analysis", "Refresh the saved analysis.") from None
        return PreflightSnapshotResponse(data=snapshot, request_id=request.state.request_id)

    @router.post("/preflights/{identifier}/judge", response_model=PreflightVerdictResponse)
    async def judge(
        identifier: UUID,
        values: PreflightCapture,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> PreflightVerdictResponse:
        """The server judges one captured attempt; the desktop never decides."""
        response.headers["Cache-Control"] = "no-store"
        verdict = await service(request).judge(
            principal.id,
            identifier,
            values.sequence,
            values.target,
            decode_png(values.before),
            decode_png(values.after),
        )
        return PreflightVerdictResponse(data=verdict, request_id=request.state.request_id)

    @router.post("/preflights/{identifier}/result", response_model=PreflightResponse)
    async def result(
        identifier: UUID,
        values: PreflightResult,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> PreflightResponse:
        response.headers["Cache-Control"] = "no-store"
        artifacts = {name: decode_png(data) for name, data in values.images.items()}
        artifacts["report"] = json.dumps(values.report, allow_nan=False).encode()
        try:
            report = await service(request).complete_desktop(
                principal.id, identifier, values.report, artifacts
            )
        except (ValueError, KeyError, TypeError):
            raise UploadError(409, "report_mismatch", "The report does not match.") from None
        return PreflightResponse(data=report, request_id=request.state.request_id)

    @router.post("/preflights/{identifier}/fail", response_model=PreflightResponse)
    async def fail(
        identifier: UUID,
        values: PreflightFailure,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> PreflightResponse:
        response.headers["Cache-Control"] = "no-store"
        report = await service(request).fail_desktop(principal.id, identifier, values.reason)
        return PreflightResponse(data=report, request_id=request.state.request_id)

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
