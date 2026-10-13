"""Factory for a small real service; no private domain workflows in this increment."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from buddy_api import __version__
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
from buddy_api.middleware import RequestContextMiddleware
from buddy_api.persistence.database import Database, DatabaseProbe
from buddy_api.readiness import DependencyProbe, check_dependencies, validate_probes

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
    settings: Settings | None = None, *, probes: Sequence[DependencyProbe] = ()
) -> FastAPI:
    config = settings if settings is not None else Settings.from_environment()
    database = Database(config.database) if config.database is not None else None
    dependencies = (DatabaseProbe(database), *probes)
    validate_probes(dependencies)
    started = False

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal started
        started = True
        try:
            yield
        finally:
            started = False
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
    app.add_middleware(RequestContextMiddleware, allowed_hosts=config.allowed_hosts)
    app.state.database = database

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "request_rejected"
        )
        message = {404: "Resource not found.", 405: "Method not allowed."}.get(
            exc.status_code, "Request rejected."
        )
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
