"""ASGI request context, explicit host validation, and redacted failures."""

import json
import logging
import os
import re
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from buddy_api.contracts import ErrorData, ErrorResponse
from buddy_api.observability import emit_metric
from buddy_identity.config import BROWSER_CALLBACK_PATH, BROWSER_SIGNOUT_PATH

logger = logging.getLogger(__name__)


def error_response(code: str, message: str, request_id: str, status: int) -> JSONResponse:
    body = ErrorResponse(error=ErrorData(code=code, message=message), request_id=request_id)
    return JSONResponse(body.model_dump(), status_code=status)


_PRODUCT = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,39}(?:/[A-Za-z0-9._-]{1,20})?")


def client_product(user_agent: str | None) -> str:
    """The first product token of a User-Agent ("Python-urllib/3.14"), nothing else."""
    match = _PRODUCT.match(user_agent or "")
    return match.group(0) if match else "unknown"


def report_request(scope: Scope, request_id: str, status: int | None, started_at: float) -> None:
    """One content-free line per HTTP request, so CloudWatch shows every call within seconds.

    It records the matched route template rather than the URL, so no identifiers,
    query strings, tokens, headers or client addresses are written.
    """
    route = getattr(scope.get("route"), "path", None)
    line = {
        "event": "api_request",
        "request_id": request_id,
        "method": str(scope.get("method", ""))[:10],
        "route": route if isinstance(route, str) else "unmatched",
        "status": status if status is not None else 0,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 1),
        "client": client_product(Headers(scope=scope).get("user-agent")),
    }
    try:
        sys.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except OSError:
        pass  # Request logging must never fail a request.


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] in {BROWSER_CALLBACK_PATH, BROWSER_SIGNOUT_PATH}:
            # The static page reads OAuth parameters from the browser's URL, not
            # the server request. Strip them from the ASGI access-log scope, even
            # when an operator runs Uvicorn with access logging enabled.
            scope["query_string"] = b""
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = False
        response_status: int | None = None
        started_at = time.perf_counter()

        def report_upload() -> None:
            path = scope.get("path", "")
            method = scope.get("method", "")
            upload_route = (method == "POST" and path.rstrip("/") == "/api/v1/uploads") or (
                method == "PUT"
                and path.startswith("/api/v1/uploads/")
                and path.endswith("/content")
            )
            if not upload_route or response_status is None:
                return
            if 200 <= response_status < 300:
                outcome = "success"
            elif 400 <= response_status < 500:
                outcome = "rejected"
            elif 500 <= response_status < 600:
                outcome = "failed"
            else:
                # Redirects are intermediate responses, not a final upload outcome.
                return
            for metric, value in (
                ("UploadRequest", 1),
                ("DurationMs", (time.perf_counter() - started_at) * 1000),
            ):
                emit_metric(
                    environment=os.environ.get("BUDDY_ENVIRONMENT", "development"),
                    service="api",
                    stage="upload",
                    outcome=outcome,
                    metric=metric,
                    value=value,
                    correlation_ids={"request_id": request_id},
                )

        async def send_response(message: Message) -> None:
            nonlocal started, response_status
            if message["type"] == "http.response.start":
                started = True
                response_status = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        hosts = Headers(scope=scope).getlist("host")
        try:
            host = urlsplit("//" + hosts[0]) if len(hosts) == 1 else None
            valid_host = (
                host is not None
                and host.hostname in self.allowed_hosts
                and host.username is None
                and host.password is None
                and not host.path
                and not host.query
                and not host.fragment
                and (host.port is None or 1 <= host.port <= 65535)
            )
        except ValueError:
            valid_host = False
        try:
            if not valid_host:
                response = error_response(
                    "invalid_host", "Request host is not allowed.", request_id, 400
                )
                await response(scope, receive, send_response)
                return
            try:
                await self.app(scope, receive, send_response)
                report_upload()
            except Exception:
                logger.error("Unexpected API failure request_id=%s", request_id)
                if started:
                    # Cannot safely replace a streamed response already sent to the client.
                    raise
                response = error_response(
                    "internal_error", "An unexpected error occurred.", request_id, 500
                )
                await response(scope, receive, send_response)
                report_upload()
        finally:
            report_request(scope, request_id, response_status, started_at)
