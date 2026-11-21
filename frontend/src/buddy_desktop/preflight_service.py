"""Request saved-action preflight; verdicts can only come from the trusted operator."""

import http.client
import json
import sys
import time
from collections.abc import Callable
from threading import Event
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import UUID, uuid4

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.services import NoRedirects, ServiceError


class PreflightService:
    def __init__(self, config: AppConfig, access: str):
        self.config, self.access = config, access

    def request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(
            self.config.api_url + "/api/v1/" + path,
            data=json.dumps(body, allow_nan=False).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.access,
                "Accept": "application/json",
                "Content-Type": "application/json",
                **({"Idempotency-Key": str(uuid4())} if body is not None else {}),
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with build_opener(NoRedirects()).open(
                request, timeout=self.config.request_timeout
            ) as response:
                raw = response.read(65537)
                if len(raw) > 65536 or response.headers.get_content_type() != "application/json":
                    raise ValueError
                result = json.loads(raw)["data"]
                if (
                    not isinstance(result, dict)
                    or result.get("state")
                    not in {"pending", "validated", "rejected", "unavailable"}
                    or result.get("phase")
                    not in {"queued", "running", "complete", "failed", "stale"}
                ):
                    raise ValueError
                UUID(result["id"])
                return result
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code in {401, 403}:
                raise LibraryAccessExpired("Sign in again to run visual QA.") from None
            if code == 404:
                raise ServiceError("This backend does not provide saved visual QA yet.") from None
            if code == 409:
                raise ServiceError(
                    "The saved analysis changed. Reopen the deck and retry."
                ) from None
            if code == 429:
                raise ServiceError("The preflight queue is full. Retry shortly.") from None
            raise ServiceError("Visual QA is temporarily unavailable. Retry this action.") from None
        except (URLError, OSError, http.client.HTTPException):
            raise ServiceError("Cannot reach visual QA. Check the backend and retry.") from None
        except (ValueError, TypeError, KeyError):
            raise ServiceError("The backend returned an invalid visual QA report.") from None

    def run(
        self,
        analysis_id: UUID,
        instance_id: str,
        *,
        profile: str,
        viewport: tuple[int, int],
        correct: bool,
        cancelled: Event,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        report = self.request(
            f"analyses/{analysis_id}/preflights",
            dict(
                instance_id=instance_id,
                profile=profile,
                viewport=viewport,
                runtime_platform=sys.platform,
                correct=correct,
            ),
        )
        identifier = UUID(report["id"])
        deadline = time.monotonic() + 600
        while report["state"] == "pending":
            if progress:
                progress(report)
            if cancelled.wait(0.75):
                raise ServiceError(
                    "Stopped waiting. The current check will finish and remain saved."
                )
            if time.monotonic() > deadline:
                raise ServiceError(
                    "The check is still running. Refresh checks to see its saved result."
                )
            report = self.request(f"preflights/{identifier}")
        if report.get("instance_id") != instance_id or report.get("analysis_id") != str(
            analysis_id
        ):
            raise ServiceError("The saved visual QA report belongs to another action.")
        return report
