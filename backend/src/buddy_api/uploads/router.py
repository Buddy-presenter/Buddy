"""Small upload control messages plus bounded raw-PPTX transfer through the API."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse
from buddy_api.uploads.contracts import PPTX_TYPE, UploadError, UploadMetadata, UploadResponse
from buddy_api.uploads.service import UploadService


class UploadMetadataLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"].rstrip("/") != "/api/v1/uploads"
        ):
            await self.app(scope, receive, send)
            return
        size = 0

        async def bounded_receive() -> Message:
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > 8192:
                    raise HTTPException(413)
            return message

        await self.app(scope, bounded_receive, send)


def service(request: Request) -> UploadService:
    result: UploadService | None = request.app.state.uploads
    if result is None:
        raise UploadError(
            503, "uploads_unavailable", "Uploads are not configured on this server.", 30
        )
    return result


def upload_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/uploads",
        tags=["uploads"],
        responses={
            code: {"model": ErrorResponse, "description": description}
            for code, description in {
                400: "Malformed request.",
                401: "Authentication required.",
                403: "Another account owns this upload.",
                404: "Upload not found.",
                408: "Upload interrupted or timed out.",
                409: "Retry or upload state conflicts.",
                413: "File or storage quota limit exceeded.",
                415: "Unsupported media type.",
                422: "Invalid upload metadata or PPTX.",
                429: "Upload capacity exhausted.",
                503: "Upload service or cleanup temporarily unavailable.",
            }.items()
        },
    )

    @router.post("", response_model=UploadResponse, status_code=200)
    async def initialize(
        request: Request,
        metadata: UploadMetadata,
        principal: Annotated[Principal, Depends(authenticated)],
        idempotency_key: Annotated[UUID, Header()],
    ) -> UploadResponse:
        if len(request.headers.getlist("idempotency-key")) != 1:
            raise UploadError(422, "invalid_retry_key", "Send one UUID Idempotency-Key header.")
        return UploadResponse(
            data=await service(request).initialize(principal.id, idempotency_key, metadata),
            request_id=request.state.request_id,
        )

    @router.get("/{identifier}", response_model=UploadResponse)
    async def status(
        identifier: UUID,
        request: Request,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> UploadResponse:
        return UploadResponse(
            data=await service(request).get(principal.id, identifier),
            request_id=request.state.request_id,
        )

    @router.put(
        "/{identifier}/content",
        response_model=UploadResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {PPTX_TYPE: {"schema": {"type": "string", "format": "binary"}}},
            }
        },
    )
    async def transfer(
        identifier: UUID,
        request: Request,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> UploadResponse:
        return UploadResponse(
            data=await service(request).receive(principal.id, identifier, request),
            request_id=request.state.request_id,
        )

    return router
