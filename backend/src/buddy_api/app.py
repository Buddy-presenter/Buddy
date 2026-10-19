"""Factory for a small real service; no private domain workflows in this increment."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from buddy_api import __version__
from buddy_api.analysis.api import analysis_router
from buddy_api.auth import Authentication, auth_router
from buddy_api.authorization import private_router
from buddy_api.browser_return import browser_return_router
from buddy_api.config import Environment, Settings
from buddy_api.contracts import (
    ErrorData,
    ErrorDetail,
    ErrorResponse,
    HealthData,
    HealthResponse,
    LegacyHealthResponse,
    ReadyData,
    ReadyResponse,
    VersionData,
    VersionResponse,
)
from buddy_api.handoff import HandoffLimitMiddleware, handoff_router
from buddy_api.library import library_router
from buddy_api.middleware import RequestContextMiddleware
from buddy_api.persistence.database import Database, DatabaseProbe
from buddy_api.processing.api import processing_router
from buddy_api.processing.queue import Queue
from buddy_api.processing.service import Processor
from buddy_api.readiness import DependencyProbe, check_dependencies, validate_probes
from buddy_api.uploads.contracts import UploadError
from buddy_api.uploads.router import UploadMetadataLimitMiddleware, upload_router
from buddy_api.uploads.service import UploadService
from buddy_api.uploads.storage import SourceStorage, StorageProbe
from buddy_api.validation.api import validation_router
from buddy_api.validation.service import ValidationService

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Malformed or disallowed request."},
    404: {"model": ErrorResponse, "description": "Resource not found."},
    405: {"model": ErrorResponse, "description": "Method not allowed."},
    422: {"model": ErrorResponse, "description": "Request validation failed."},
    500: {"model": ErrorResponse, "description": "An unexpected error occurred."},
    503: {"model": ErrorResponse, "description": "Service is not ready."},
}


def request_id(request: Request) -> str:
    identifier: str = request.state.request_id
    return identifier


def create_app(
    settings: Settings | None = None,
    *,
    probes: Sequence[DependencyProbe] = (),
    upload_storage: SourceStorage | None = None,
) -> FastAPI:
    config = settings if settings is not None else Settings.from_environment()
    database = Database(config.database) if config.database is not None else None
    storage = upload_storage or (SourceStorage(config.uploads) if config.uploads else None)
    uploads = (
        UploadService(
            database,
            config.uploads,
            storage,
            owner_job_quota=config.worker.owner_quota if config.worker else 4,
        )
        if database and config.uploads and storage
        else None
    )
    dependencies = (
        DatabaseProbe(database),
        *((StorageProbe(storage),) if storage else ()),
        *probes,
    )
    validate_probes(dependencies)
    validator = (
        ValidationService(uploads, config.validator) if uploads and config.validator else None
    )
    started = False
    queue = Queue(config.worker) if config.worker and uploads else None
    processor = (
        Processor(database, storage, config.worker, queue)
        if database and storage and config.worker and queue
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal started
        started = True
        reconciler = asyncio.create_task(uploads.run_reconciliation()) if uploads else None

        async def reconcile_validation() -> None:
            while True:
                try:
                    assert validator is not None
                    await validator.reconcile()
                except UploadError:
                    pass  # Durable cleanup reservations remain for the next bounded retry.
                await asyncio.sleep(30)

        validation_cleanup = asyncio.create_task(reconcile_validation()) if validator else None
        dispatch = asyncio.create_task(processor.dispatch()) if processor else None
        try:
            yield
        finally:
            started = False
            if dispatch:
                dispatch.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatch
            if queue:
                queue.close()
            if reconciler:
                reconciler.cancel()
                with suppress(asyncio.CancelledError):
                    await reconciler
            if validation_cleanup:
                validation_cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await validation_cleanup
            if storage:
                await asyncio.to_thread(storage.close)
            if database is not None:
                await database.close()

    app = FastAPI(
        title="Buddy API",
        version=__version__,
        description="Versioned service foundation for the Buddy desktop client and workers.",
        lifespan=lifespan,
        docs_url="/docs" if config.environment is not Environment.PRODUCTION else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.environment is not Environment.PRODUCTION else None,
    )
    app.add_middleware(UploadMetadataLimitMiddleware)
    app.add_middleware(HandoffLimitMiddleware)
    app.add_middleware(RequestContextMiddleware, allowed_hosts=config.allowed_hosts)
    app.state.database = database
    app.state.authentication = Authentication(config.identity)
    app.state.uploads = uploads
    app.state.validator = validator
    app.state.worker_settings = config.worker
    app.include_router(auth_router(config.identity))
    app.include_router(browser_return_router())
    app.include_router(handoff_router(storage))
    app.include_router(private_router())
    app.include_router(library_router())
    app.include_router(analysis_router())
    app.include_router(upload_router())
    app.include_router(validation_router())
    app.include_router(processing_router())

    @app.exception_handler(UploadError)
    async def upload_error(request: Request, exc: UploadError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else {}
        return JSONResponse(
            exc.body(request_id(request)).model_dump(), status_code=exc.status, headers=headers
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = {
            401: "unauthenticated",
            403: "forbidden",
            409: "conflict",
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.status_code, "request_rejected")
        message = {
            401: "Authentication required.",
            403: "Access denied.",
            409: "Operation conflicts with stored data.",
            404: "Resource not found.",
            405: "Method not allowed.",
        }.get(exc.status_code, "Request rejected.")
        body = ErrorResponse(
            error=ErrorData(code=code, message=message), request_id=request_id(request)
        )
        return JSONResponse(body.model_dump(), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Keep locations/types, never echo a submitted value or arbitrary validator message.
        details = [
            ErrorDetail(location=".".join(map(str, item["loc"])), code=item["type"])
            for item in exc.errors()
        ]
        body = ErrorResponse(
            error=ErrorData(
                code="validation_error", message="Request validation failed.", details=details
            ),
            request_id=request_id(request),
        )
        return JSONResponse(body.model_dump(), status_code=422)

    router = APIRouter(prefix="/api/v1", responses=ERROR_RESPONSES)

    @router.get("/health", response_model=HealthResponse, tags=["operations"])
    async def health(request: Request) -> HealthResponse:
        return HealthResponse(data=HealthData(), request_id=request_id(request))

    @router.get("/ready", response_model=ReadyResponse, tags=["operations"])
    async def ready(request: Request) -> ReadyResponse | JSONResponse:
        results = (
            await check_dependencies(dependencies, config.readiness_timeout) if started else []
        )
        if not started or any(result.status == "unavailable" for result in results):
            details = [
                ErrorDetail(location=result.name, code="dependency_unavailable")
                for result in results
                if result.status == "unavailable"
            ]
            if not started:
                details.insert(0, ErrorDetail(location="application", code="not_started"))
            body = ErrorResponse(
                error=ErrorData(code="not_ready", message="Service is not ready.", details=details),
                request_id=request_id(request),
            )
            return JSONResponse(body.model_dump(), status_code=503)
        return ReadyResponse(data=ReadyData(dependencies=results), request_id=request_id(request))

    @router.get("/version", response_model=VersionResponse, tags=["operations"])
    async def version(
        request: Request, api_version: str = Query("v1", pattern="^v1$")
    ) -> VersionResponse:
        # A small, useful typed query exercises request validation without adding a domain workflow.
        return VersionResponse(
            data=VersionData(version=__version__), request_id=request_id(request)
        )

    app.include_router(router)

    @app.get(
        "/health",
        response_model=LegacyHealthResponse,
        tags=["compatibility"],
        responses=ERROR_RESPONSES,
    )
    async def desktop_health(request: Request) -> LegacyHealthResponse:
        return LegacyHealthResponse(request_id=request_id(request))

    return app
