"""Authenticated candidate reads from a fresh live speech context; no execution."""

import json
import re
import time
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import UUID

from buddy_desktop.config import AppConfig
from buddy_desktop.presentation_session_service import PresentationSession
from buddy_desktop.services import NoRedirects, ServiceError
from buddy_desktop.transcript_context import ContextSnapshot


class CandidateService:
    def __init__(self, config: AppConfig):
        self.config = config

    def resolve(
        self, access: str, session: PresentationSession, context: ContextSnapshot
    ) -> dict[str, object]:
        """Call off the GUI thread. Recheck context epoch/state before using results."""
        now = time.monotonic_ns()
        if (
            not context.fresh
            or not context.segments
            or not context.stream_id
            or session.status != "active"
            or session.id != context.session_id
            or session.state_version != context.state_version
            or not 0 <= now - context.observed_ns <= 1_000_000_000
            or not 0 <= now - context.segments[-1].capture_end_ns <= 5_000_000_000
            or any(
                now - (s.capture_end_ns - round((s.end_seconds - s.start_seconds) * 1e9))
                > 30_000_000_000
                for s in context.segments
            )
        ):
            raise ServiceError("Speech context changed or expired; wait for fresh speech.")
        payload = {
            "protocol": "presentation-candidates.v1",
            "expected_state_version": context.state_version,
            "context_epoch": context.epoch,
            "stream_id": context.stream_id,
            "sequence": max(segment.sequence for segment in context.segments),
            "sent_at": datetime.now(UTC).isoformat(),
            "text": context.text,
        }
        request = Request(
            self.config.api_url + f"/api/v1/sessions/{UUID(session.id)}/candidates",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + access, "Content-Type": "application/json"},
        )
        try:
            with build_opener(NoRedirects()).open(
                request, timeout=self.config.request_timeout
            ) as reply:
                raw = reply.read(32769)
                if len(raw) > 32768:
                    raise ValueError
                result = json.loads(raw)
            if (
                not isinstance(result, dict)
                or set(result)
                != {
                    "protocol",
                    "resolver_version",
                    "session_id",
                    "analysis_id",
                    "state_version",
                    "context_epoch",
                    "stream_id",
                    "sequence",
                    "input_sha256",
                    "candidates",
                }
                or result.get("protocol") != payload["protocol"]
                or result.get("resolver_version") != "summary-overlap-1"
            ):
                raise ValueError
            for key, value in (
                ("session_id", session.id),
                ("analysis_id", session.analysis_version_id),
                ("state_version", context.state_version),
                ("context_epoch", context.epoch),
                ("stream_id", context.stream_id),
                ("sequence", payload["sequence"]),
            ):
                if result.get(key) != value:
                    raise ValueError
            if any(
                type(result.get(key)) is not int
                for key in ("state_version", "context_epoch", "sequence")
            ):
                raise ValueError
            if not isinstance(result.get("input_sha256"), str) or not re.fullmatch(
                "[0-9a-f]{64}", result["input_sha256"]
            ):
                raise ValueError
            candidates = result.get("candidates")
            if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
                raise ValueError
            if candidates[0].get("kind") != "no_change":
                raise ValueError
            for target in candidates:
                if (
                    not isinstance(target, dict)
                    or target.get("kind")
                    not in {"no_change", "slide", "zoom_region", "highlight_region"}
                    or not isinstance(target.get("slide_id"), str)
                    or not 1 <= len(target["slide_id"]) <= 128
                ):
                    raise ValueError
            if candidates[0]["slide_id"] != (
                session.state.active_slide_id if session.state else ""
            ):
                raise ValueError
            for target in candidates:
                if set(target) != {"kind", "slide_id", "action_id", "reason", "score"}:
                    raise ValueError
                if (
                    type(target["score"]) is not int
                    or not 0 <= target["score"] <= 8192
                    or target["reason"]
                    not in {"stay", "current", "explicit_navigation", "topic", "purpose"}
                ):
                    raise ValueError
                action = target["action_id"]
                if target["kind"] in {"zoom_region", "highlight_region"}:
                    if not isinstance(action, str) or not 1 <= len(action) <= 128:
                        raise ValueError
                elif action is not None:
                    raise ValueError
            received_ns = time.monotonic_ns()
            if not 0 <= received_ns - context.segments[-1].capture_end_ns <= 5_000_000_000 or any(
                received_ns
                - (
                    segment.capture_end_ns
                    - round((segment.end_seconds - segment.start_seconds) * 1_000_000_000)
                )
                > 30_000_000_000
                for segment in context.segments
            ):
                raise ValueError
            return result
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ServiceError(
                f"Candidate request failed ({status}); refresh session and speech."
            ) from None
        except (URLError, OSError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
            raise ServiceError("Candidates are unavailable; retry with fresh speech.") from None
