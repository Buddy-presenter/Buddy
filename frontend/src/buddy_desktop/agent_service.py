"""Authenticated proposal client. Returned suggestions are never executor commands."""

import http.client
import io
import json
import math
import re
import ssl
import threading
import time
from collections.abc import Callable
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from uuid import UUID

from buddy_desktop.candidate_service import CandidateService, completed_payload
from buddy_desktop.config import AppConfig
from buddy_desktop.presentation_session_service import PresentationSession
from buddy_desktop.services import NoRedirects, ServiceError
from buddy_desktop.transcript_context import ContextSnapshot


class KeptConnection:
    """One reusable HTTPS connection per worker thread for live decisions (#237).

    urllib opens a new TCP and TLS connection for every request, adding about 0.15 s to
    each live suggestion. This keeps it open between requests. It never follows
    redirects, verifies certificates with the default context, bounds the response,
    and reports HTTP errors as urllib's HTTPError so callers keep their handling.
    """

    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            raise ValueError("Unsupported API URL")
        self.scheme, self.host, self.port = parts.scheme, parts.hostname, parts.port
        self.base_path = parts.path.rstrip("/")
        self._local = threading.local()
        self._context = ssl.create_default_context()

    def _connection(self, fresh: bool) -> http.client.HTTPConnection:
        connection: http.client.HTTPConnection | None = getattr(self._local, "connection", None)
        if connection is not None and not fresh:
            return connection
        if connection is not None:
            connection.close()
        connection = (
            http.client.HTTPSConnection(self.host, self.port, context=self._context)
            if self.scheme == "https"
            else http.client.HTTPConnection(self.host, self.port)
        )
        self._local.connection = connection
        return connection

    def post(
        self, path: str, body: bytes, headers: dict[str, str], *, timeout: float, limit: int
    ) -> bytes:
        url = f"{self.scheme}://{self.host}{self.base_path}{path}"
        for attempt in range(2):
            reused = getattr(self._local, "connection", None) is not None and attempt == 0
            connection = self._connection(fresh=attempt > 0)
            connection.timeout = timeout
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            try:
                connection.request("POST", self.base_path + path, body=body, headers=headers)
                response = connection.getresponse()
                raw = response.read(limit + 1)
                if response.will_close:
                    connection.close()
                    self._local.connection = None
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                # Only a connection kept from an earlier request may have been closed
                # by the server meanwhile; retry that case once on a new connection.
                self._local.connection = None
                connection.close()
                if reused:
                    continue
                raise
            except BaseException:
                self._local.connection = None
                connection.close()
                raise
            if response.status >= 300:
                header_message = Message()
                for key, value in response.getheaders():
                    header_message[key] = value
                error_body = io.BytesIO(raw[:limit])
                raise HTTPError(url, response.status, response.reason, header_message, error_body)
            if len(raw) > limit:
                raise ValueError
            return raw
        raise URLError("connection closed")


TOOLS = {
    "no_action",
    "next_slide",
    "previous_slide",
    "go_to_slide",
    "zoom_region",
    "highlight_region",
    "reset_view",
}
REASONS = {
    "explicit",
    "topic",
    "correction",
    "stay",
    "uncertain",
    "disabled",
    "busy",
    "budget",
    "paced",
    "deadline",
    "provider_unavailable",
    "rate_limited",
    "invalid_output",
    "stale",
    "candidates_unavailable",
}
CONFLICT_MESSAGES = {
    "speech_context_expired": "Speech context expired; wait for fresh speech.",
    "session_inactive": "Presentation session ended; reopen it.",
    "session_state_changed": "Presentation changed; wait for sync, then speak again.",
    "analysis_changed": "Saved analysis changed; reopen the presentation.",
    "analysis_unavailable": (
        "Saved analysis unavailable for live suggestions; reopen the presentation."
    ),
}


def conflict_error(exc: HTTPError) -> ServiceError:
    """Translate only known safe 409 fields; never surface arbitrary server text."""
    request_id = exc.headers.get("X-Request-ID") if exc.headers else None
    message = "Operation conflicted; wait for fresh speech and try again."
    try:
        raw = exc.read(8193)
        if len(raw) > 8192:
            raise ValueError
        payload = json.loads(raw)
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code in CONFLICT_MESSAGES:
            message = CONFLICT_MESSAGES[code]
        body_request_id = payload.get("request_id") if isinstance(payload, dict) else None
        if request_id is None and isinstance(body_request_id, str):
            request_id = body_request_id
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    finally:
        exc.close()
    try:
        validated_id = str(UUID(request_id)) if isinstance(request_id, str) else None
    except (ValueError, AttributeError):
        validated_id = None
    if validated_id is not None:
        message += f" Reference: {validated_id}."
    return ServiceError(message)


class AgentService:
    def __init__(self, config: AppConfig):
        self.config = config
        self._connection: KeptConnection | None = None
        self._connection_url = ""

    def _kept(self) -> KeptConnection:
        url = self.config.api_url
        if self._connection is None or self._connection_url != url:
            self._connection, self._connection_url = KeptConnection(url), url
        return self._connection

    def prepare(self, access: str, session: PresentationSession) -> None:
        CandidateService(self.config).prepare(access, session)

    def check_transition(
        self,
        access: str,
        session: PresentationSession,
        target: dict[str, object],
    ) -> dict[str, object]:
        request = Request(
            self.config.api_url + f"/api/v1/sessions/{UUID(session.id)}/transition-check",
            method="POST",
            data=json.dumps(
                {
                    "expected_state_version": session.state_version,
                    "analysis_id": session.analysis_version_id,
                    "active_slide_id": session.state.active_slide_id if session.state else None,
                    "focus_view": session.state.focus_view if session.state else None,
                    "focus_region": session.state.focus_region if session.state else None,
                    "target": target,
                },
                allow_nan=False,
            ).encode(),
            headers={"Authorization": "Bearer " + access, "Content-Type": "application/json"},
        )
        try:
            with build_opener(NoRedirects()).open(request, timeout=4) as response:
                raw = response.read(8193)
                if len(raw) > 8192 or response.headers.get_content_type() != "application/json":
                    raise ValueError
                result = json.loads(raw)
            expected = {
                "session_id": session.id,
                "analysis_id": session.analysis_version_id,
                "state_version": session.state_version,
                "target": target,
            }
            if result != expected or type(result["state_version"]) is not int:
                raise ValueError
            return expected
        except HTTPError as exc:
            exc.close()
            raise ServiceError("Automatic movement stopped. Sync the session and retry.") from None
        except (URLError, OSError, TimeoutError, ValueError, TypeError, KeyError):
            raise ServiceError(
                "Automatic movement stopped. Continue presenting manually."
            ) from None

    def propose(
        self,
        access: str,
        session: PresentationSession,
        context: ContextSnapshot,
        *,
        current_context: Callable[[], ContextSnapshot],
    ) -> dict[str, object]:
        payload = completed_payload(session, context, current_context, allow_provisional=True)
        payload["protocol"] = "presentation-agent.v1"
        payload["focus_view"] = session.state.focus_view if session.state else None
        payload["focus_region"] = session.state.focus_region if session.state else None
        payload["segments"] = [
            {"sequence": s.sequence, "text": s.text, "final": s.final}
            for s in context.decision_segments
        ]
        try:
            raw = self._kept().post(
                f"/api/v1/sessions/{UUID(session.id)}/agent",
                json.dumps(payload, ensure_ascii=False).encode(),
                {"Authorization": "Bearer " + access, "Content-Type": "application/json"},
                timeout=6,
                limit=32768,
            )
            result = json.loads(raw)
            if not isinstance(result, dict) or set(result) != {
                "protocol",
                "decision_id",
                "session_id",
                "analysis_id",
                "state_version",
                "active_slide_id",
                "context_epoch",
                "stream_id",
                "sequence",
                "input_sha256",
                "target",
                "decision_source",
                "reason",
                "confidence",
                "evidence",
                "elapsed_ms",
                "retry_after_ms",
            }:
                raise ValueError
            for key, value in (
                ("protocol", payload["protocol"]),
                ("session_id", session.id),
                ("analysis_id", session.analysis_version_id),
                ("state_version", context.state_version),
                ("active_slide_id", payload["active_slide_id"]),
                ("context_epoch", context.epoch),
                ("stream_id", context.stream_id),
                ("sequence", payload["sequence"]),
            ):
                if result[key] != value:
                    raise ValueError
            UUID(result["decision_id"])
            if (
                any(
                    type(result[k]) is not int
                    for k in ("state_version", "context_epoch", "sequence", "elapsed_ms")
                )
                or not 0 <= result["elapsed_ms"] <= 6000
                or not isinstance(result["input_sha256"], str)
                or not re.fullmatch("[0-9a-f]{64}", result["input_sha256"])
                or result["reason"] not in REASONS
                or result["decision_source"] not in {"model", "navigation", "fallback"}
            ):
                raise ValueError
            confidence = result["confidence"]
            retry_after = result["retry_after_ms"]
            if result["reason"] == "paced":
                if type(retry_after) is not int or not 1 <= retry_after <= 5000:
                    raise ValueError
            elif retry_after is not None:
                raise ValueError
            if confidence is not None and (
                type(confidence) not in {int, float}
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                raise ValueError
            evidence = result["evidence"]
            if (
                not isinstance(evidence, list)
                or len(evidence) > 8
                or any(
                    type(i) is not int or not 0 <= i < len(context.decision_segments)
                    for i in evidence
                )
                or len(set(evidence)) != len(evidence)
            ):
                raise ValueError
            target = result["target"]
            if (
                not isinstance(target, dict)
                or set(target) != {"tool", "slide_id", "action_id", "region"}
                or target["tool"] not in TOOLS
                or not isinstance(target["slide_id"], str)
                or not 1 <= len(target["slide_id"]) <= 128
            ):
                raise ValueError
            region = target["region"]
            if target["tool"] in {"zoom_region", "highlight_region"}:
                if (
                    not isinstance(target["action_id"], str)
                    or not 1 <= len(target["action_id"]) <= 128
                    or not isinstance(region, dict)
                    or set(region) != {"x", "y", "width", "height"}
                    or any(
                        type(v) not in {int, float} or not math.isfinite(v) or v < 0
                        for v in region.values()
                    )
                    or region["width"] <= 0
                    or region["height"] <= 0
                ):
                    raise ValueError
            elif region is not None or target["action_id"] is not None:
                raise ValueError
            if target["tool"] != "no_action" and (not evidence or confidence is None):
                raise ValueError
            if result["decision_source"] == "fallback" and (
                target["tool"] != "no_action" or confidence is not None or evidence
            ):
                raise ValueError
            if result["decision_source"] == "navigation" and target["tool"] not in {
                "no_action",
                "next_slide",
                "previous_slide",
                "go_to_slide",
            }:
                raise ValueError
            if (
                result["decision_source"] == "navigation"
                and result["reason"] == "topic"
                and (target["tool"] not in {"next_slide", "previous_slide", "go_to_slide"})
            ):
                raise ValueError
            if result["reason"] == "paced" and result["decision_source"] != "fallback":
                raise ValueError
            if target["tool"] in {"no_action", "reset_view"} and (
                session.state is None or target["slide_id"] != session.state.active_slide_id
            ):
                raise ValueError
            if (
                not context.matches_decision(current_context())
                or time.monotonic_ns() - context.segments[-1].capture_end_ns > 5_000_000_000
            ):
                raise ServiceError("Suggestion expired; waiting for fresh speech.")
            return result
        except HTTPError as exc:
            status = exc.code
            if status == 409:
                raise conflict_error(exc) from None
            exc.close()
            raise ServiceError(
                f"Suggestions unavailable ({status}); continue presenting manually."
            ) from None
        except (
            URLError,
            OSError,
            TimeoutError,
            http.client.HTTPException,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
        ):
            raise ServiceError("Suggestions unavailable; continue presenting manually.") from None
