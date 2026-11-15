"""Authenticated proposal client. Returned suggestions are never executor commands."""

import json
import math
import re
import time
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import UUID

from buddy_desktop.candidate_service import CandidateService, completed_payload
from buddy_desktop.config import AppConfig
from buddy_desktop.presentation_session_service import PresentationSession
from buddy_desktop.services import NoRedirects, ServiceError
from buddy_desktop.transcript_context import ContextSnapshot

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
        payload = completed_payload(session, context, current_context)
        payload["protocol"] = "presentation-agent.v1"
        payload["segments"] = [
            {"sequence": s.sequence, "text": s.text} for s in context.candidate_segments
        ]
        request = Request(
            self.config.api_url + f"/api/v1/sessions/{UUID(session.id)}/agent",
            method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Authorization": "Bearer " + access, "Content-Type": "application/json"},
        )
        try:
            with build_opener(NoRedirects()).open(request, timeout=6) as reply:
                raw = reply.read(32769)
                if len(raw) > 32768:
                    raise ValueError
                result = json.loads(raw)
            if not isinstance(result, dict) or set(result) != {
                "protocol",
                "decision_id",
                "session_id",
                "analysis_id",
                "state_version",
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
                    type(i) is not int or not 0 <= i < len(context.candidate_segments)
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
            }:
                raise ValueError
            if result["reason"] == "paced" and result["decision_source"] != "fallback":
                raise ValueError
            if target["tool"] in {"no_action", "reset_view"} and (
                session.state is None or target["slide_id"] != session.state.active_slide_id
            ):
                raise ValueError
            if (
                not context.matches(current_context())
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
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
        ):
            raise ServiceError("Suggestions unavailable; continue presenting manually.") from None
