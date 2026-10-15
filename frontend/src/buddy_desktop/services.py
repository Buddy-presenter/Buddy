"""Small synchronous HTTP adapter; callers must run it outside the GUI thread."""

import http.client
import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from buddy_desktop.config import AppConfig

MAX_RESPONSE_BYTES = 64 * 1024


class ServiceError(Exception):
    """Safe, user-facing backend failure; never expose arbitrary response content."""


@dataclass(frozen=True)
class Health:
    status: str


class NoRedirects(HTTPRedirectHandler):
    """Keep checks pinned to the configured backend, without downgrade redirects."""

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


class HealthService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def check(self) -> Health:
        request = Request(
            f"{self.config.api_url}/health",
            headers={"Accept": "application/json", "User-Agent": "BuddyDesktop/0.1"},
        )
        try:
            with build_opener(NoRedirects()).open(
                request, timeout=self.config.request_timeout
            ) as response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ServiceError("The backend health response exceeds the supported size.")
                if response.headers.get_content_type() != "application/json":
                    raise ServiceError("The backend returned an unsupported health response.")
        except HTTPError as exc:
            if exc.code == 404:
                raise ServiceError("Health endpoint not found. Check the backend URL.") from exc
            if exc.code in {401, 403}:
                raise ServiceError("The backend denied access to its health endpoint.") from exc
            raise ServiceError(f"The backend health check failed (HTTP {exc.code}).") from exc
        except (TimeoutError, URLError, OSError, http.client.HTTPException) as exc:
            raise ServiceError(
                "Cannot reach the backend. Check its address and try again."
            ) from exc
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ServiceError("The backend returned an invalid health response.") from exc
        if not isinstance(payload, dict) or payload.get("status") not in {"ok", "healthy"}:
            raise ServiceError("The backend does not report a healthy status.")
        return Health(status=payload["status"])
