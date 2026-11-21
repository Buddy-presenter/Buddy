"""Request saved-action preflight; verdicts can only come from the trusted operator."""

import base64
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

    def _post(self, path: str, body: dict[str, Any], *, limit: int = 65536) -> Any:
        """POST to a desktop-capture endpoint and return its bounded ``data``."""
        content = json.dumps(body, allow_nan=False).encode()
        request = Request(
            self.config.api_url + "/api/v1/" + path,
            data=content,
            headers={
                "Authorization": "Bearer " + self.access,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        # Screenshot uploads take longer than ordinary API calls.
        timeout = max(120.0, self.config.request_timeout) if len(content) > 65536 else None
        try:
            with build_opener(NoRedirects()).open(
                request, timeout=timeout or self.config.request_timeout
            ) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit or response.headers.get_content_type() != "application/json":
                    raise ValueError
                return json.loads(raw)["data"]
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code in {401, 403}:
                raise LibraryAccessExpired("Sign in again to check actions.") from None
            if code == 409:
                raise ServiceError("The saved analysis changed. Reopen the deck.") from None
            if code == 503:
                raise ServiceError("Action checks are unavailable on this server.") from None
            raise ServiceError("The action check could not be saved. Retry it.") from None
        except (URLError, OSError, http.client.HTTPException):
            raise ServiceError("Cannot reach Buddy to check actions. Retry.") from None
        except (ValueError, TypeError, KeyError):
            raise ServiceError("The backend returned an invalid check response.") from None

    def run_on_desktop(
        self,
        analysis_id: UUID,
        instance_id: str,
        source: bytes,
        *,
        profile: str,
        viewport: tuple[int, int],
        cancelled: Event,
        progress: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        """Check one PowerPoint action here; the server judges and saves the result."""
        from buddy_desktop.desktop_capture import capture_check

        report = self.request(
            f"analyses/{analysis_id}/preflights",
            dict(
                instance_id=instance_id,
                profile=profile,
                viewport=viewport,
                runtime_platform=sys.platform,
                correct=True,
            ),
        )
        identifier = UUID(report["id"])
        if report["state"] != "pending":
            return report  # An identical finished check already exists.
        snapshot = self._post(f"preflights/{identifier}/claim", {}, limit=1024 * 1024)
        if not isinstance(snapshot, dict):
            raise ServiceError("The backend returned an invalid check response.")

        def judge(sequence: int, target: dict[str, Any], before: bytes, after: bytes) -> object:
            return self._post(
                f"preflights/{identifier}/judge",
                dict(
                    sequence=sequence,
                    target=target,
                    before=base64.b64encode(before).decode(),
                    after=base64.b64encode(after).decode(),
                ),
            )

        try:
            captured = capture_check(snapshot, source, judge, cancelled, progress)
        except (OSError, ValueError, KeyError, TypeError):
            captured = None
        report_body = captured.report if captured else None
        if (
            captured is None
            or report_body is None
            or report_body.get("state") not in {"validated", "rejected"}
        ):
            reason = (
                (captured.failure if captured else None)
                or (report_body or {}).get("reason")
                or "worker_failed"
            )
            result = self._post(f"preflights/{identifier}/fail", dict(reason=str(reason)[:64]))
        else:
            result = self._post(
                f"preflights/{identifier}/result",
                dict(
                    report=report_body,
                    images={
                        name: base64.b64encode(data).decode()
                        for name, data in captured.artifacts.items()
                    },
                ),
            )
        if not isinstance(result, dict) or result.get("instance_id") != instance_id:
            raise ServiceError("The saved check belongs to another action.")
        return result

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
