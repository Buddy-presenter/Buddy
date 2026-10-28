"""Server-executed diagnostic validation; clients cannot assert successful validation."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse
from buddy_api.uploads.contracts import UploadError
from buddy_api.validation.contracts import StoredResult, ValidationRequest, ValidationResponse
from buddy_api.validation.service import ValidationService


def service(request: Request, *, read_only: bool = False) -> ValidationService:
    result: ValidationService | None = request.app.state.validator
    if result is None:
        if read_only and request.app.state.uploads is not None:
            return ValidationService(request.app.state.uploads)
        raise UploadError(503, "validator_unavailable", "Action validation is not configured.")
    return result


def validation_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["action-validation"],
        responses={
            code: {"model": ErrorResponse, "description": description}
            for code, description in {
                401: "Authentication required.",
                404: "Owned resource not found.",
                409: "Source or retry state conflicts.",
                413: "Input or storage limit exceeded.",
                422: "Request validation failed.",
                429: "Validation capacity exhausted.",
                503: "Validation service is unavailable.",
            }.items()
        },
    )

    @router.post("/decks/{deck_id}/action-validations", response_model=ValidationResponse)
    async def validate(
        deck_id: UUID,
        request: Request,
        response: Response,
        inputs: ValidationRequest,
        principal: Annotated[Principal, Depends(authenticated)],
        idempotency_key: Annotated[UUID, Header()],
    ) -> ValidationResponse:
        if len(request.headers.getlist("idempotency-key")) != 1:
            raise UploadError(422, "invalid_retry_key", "Send one Idempotency-Key header.")
        response.headers["Cache-Control"] = "no-store"
        result = await service(request).validate(principal.id, deck_id, idempotency_key, inputs)
        if not isinstance(result, StoredResult):
            raise UploadError(
                415, "unsupported_source", "Use a PDF validation request for this endpoint."
            )
        return ValidationResponse(
            data=result,
            request_id=request.state.request_id,
        )

    @router.get("/action-validations/{identifier}", response_model=ValidationResponse)
    async def read(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> ValidationResponse:
        response.headers["Cache-Control"] = "no-store"
        result = await service(request).read(principal.id, identifier)
        if not isinstance(result, StoredResult):
            raise UploadError(
                415,
                "unsupported_source",
                "This is a PowerPoint execution report; use its private evidence link.",
            )
        return ValidationResponse(
            data=result,
            request_id=request.state.request_id,
        )

    @router.get("/action-validations/{identifier}/evidence/{artifact}", response_class=Response)
    async def evidence(
        identifier: UUID,
        artifact: UUID,
        request: Request,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> Response:
        content, mime = await service(request, read_only=True).evidence(
            principal.id, identifier, artifact
        )
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
