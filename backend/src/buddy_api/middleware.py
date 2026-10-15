"""ASGI request context, explicit host validation, and redacted failures."""

import logging
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from buddy_api.contracts import ErrorData, ErrorResponse

logger = logging.getLogger(__name__)


def error_response(code: str, message: str, request_id: str, status: int) -> JSONResponse:
    body = ErrorResponse(error=ErrorData(code=code, message=message), request_id=request_id)
    return JSONResponse(body.model_dump(), status_code=status)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = False

        async def send_response(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
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
        if not valid_host:
            response = error_response(
                "invalid_host", "Request host is not allowed.", request_id, 400
            )
            await response(scope, receive, send_response)
            return
        try:
            await self.app(scope, receive, send_response)
        except Exception:
            logger.error("Unexpected API failure request_id=%s", request_id)
            if started:
                # Cannot safely replace a streamed response already sent to the client.
                raise
            response = error_response(
                "internal_error", "An unexpected error occurred.", request_id, 500
            )
            await response(scope, receive, send_response)
