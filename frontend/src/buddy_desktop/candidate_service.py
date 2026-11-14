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

    def prepare(self, access: str, session: PresentationSession) -> None:
        """Prepare once before starting live candidate queries; run off GUI thread."""
        request = Request(
            self.config.api_url + f"/api/v1/sessions/{UUID(session.id)}/candidates/prepare",
            data=b"",
            method="POST",
            headers={"Authorization": "Bearer " + access},
        )
        try:
            with build_opener(NoRedirects()).open(request, timeout=125) as reply:
                result = json.loads(reply.read(4097))
                if not isinstance(result, dict) or result.get("ready") is not True:
                    raise ValueError
        except (HTTPError, URLError, OSError, ValueError):
            raise ServiceError(
                "Semantic preparation unavailable; retry before presenting."
            ) from None

    def resolve(
        self, access: str, session: PresentationSession, context: ContextSnapshot
    ) -> dict[str, object]:
        """Call off the GUI thread. Recheck context epoch/state before using results."""
        now = time.monotonic_ns()
        segments = context.candidate_segments
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
        if not segments:
            raise ServiceError("Wait for a completed speech segment before searching.")
        if now - max(s.received_ns for s in segments) < 200_000_000:
            raise ServiceError("Wait briefly for completed speech context to settle.")
        payload = {
            "protocol": "presentation-candidates.v1",
            "expected_state_version": context.state_version,
            "context_epoch": context.epoch,
            "stream_id": context.stream_id,
            "sequence": max(segment.sequence for segment in segments),
            "sent_at": datetime.now(UTC).isoformat(),
            "text": " ".join(segment.text for segment in segments),
            "latest_utterance": segments[-1].text,
        }
        request = Request(
            self.config.api_url + f"/api/v1/sessions/{UUID(session.id)}/candidates",
            data=json.dumps(payload, ensure_ascii=False).encode(),
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
                or result.get("resolver_version")
                not in {
                    "summary-overlap-1",
                    "summary-overlap-2",
                    "cohere-embed5-v1",
                    "cohere-embed5-v2",
                }
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
            try:
                error = json.loads(exc.read(4096)).get("error", {}).get("code")
            except (ValueError, AttributeError):
                error = None
            finally:
                exc.close()
            messages = {
                "embedding_budget_exhausted": "Embedding allowance exhausted.",
                "embedding_index_not_ready": "Prepare this presentation before semantic retrieval.",
                "embedding_query_busy": "Wait for the current speech query before retrying.",
                "embedding_rate_limited": "The embedding provider is busy; wait before retrying.",
            }
            if isinstance(error, str) and error in messages:
                raise ServiceError(messages[error]) from None
            raise ServiceError(
                f"Candidate request failed ({status}); refresh session and speech."
            ) from None
        except (URLError, OSError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
            raise ServiceError("Candidates are unavailable; retry with fresh speech.") from None
