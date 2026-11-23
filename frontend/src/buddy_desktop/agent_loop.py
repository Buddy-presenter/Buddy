"""Latest-only, bounded native scheduling. All mutable scheduling state belongs to Qt."""

import hashlib
import logging
import re
import time
import weakref
from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from buddy_desktop.agent_service import AgentService
from buddy_desktop.library_service import AccessProvider, access_provider
from buddy_desktop.presentation_session_service import PresentationSession
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.transcript_context import ContextSnapshot

logger = logging.getLogger(__name__)


def _incomplete_numbered_navigation(text: str) -> bool:
    normalized = text.casefold().replace("’", "'").strip()
    normalized = re.sub(r"\blet's\b", "let us", normalized)
    normalized = " ".join(re.findall(r"[^\W_]+", normalized))
    prefixes = (
        r"(?:please )?(?:let us )?(?:go|move|jump|navigate|switch|turn) "
        r"(?:to|over to) (?:the )?(?:slide|page)(?: number)?",
        r"(?:please )?(?:let us )?(?:go|move|return) back to "
        r"(?:the )?(?:slide|page)(?: number)?",
        r"(?:please )?show (?:me )?(?:the )?(?:slide|page)(?: number)?",
        r"(?:please )?take me to (?:the )?(?:slide|page)(?: number)?",
        r"(?:please )?i want to (?:go|move|jump) to (?:the )?(?:slide|page)(?: number)?",
    )
    return any(re.fullmatch(prefix, normalized) is not None for prefix in prefixes)


class AgentLoop(QObject):
    status = Signal(str)
    proposed = Signal(object)
    unavailable = Signal()

    def __init__(
        self,
        service: AgentService,
        parent: QObject | None = None,
        current_context: Callable[[], ContextSnapshot] | None = None,
        local_navigation: Callable[[PresentationSession, ContextSnapshot], dict[str, object] | None]
        | None = None,
    ):
        super().__init__(parent)
        self.service = service
        self.current_context = current_context
        self._local_navigation_method = (
            weakref.WeakMethod(local_navigation)
            if local_navigation is not None
            and getattr(local_navigation, "__self__", None) is not None
            else None
        )
        self.local_navigation = (
            None if self._local_navigation_method is not None else local_navigation
        )
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self._succeeded)
        self.runner.failed.connect(self._failed)
        self.runner.idle.connect(self._drain_pending)
        self.enabled = False
        self._generation = 0
        self._current: ContextSnapshot | None = None
        self._submitted: ContextSnapshot | None = None
        self._last_revision: tuple[object, ...] | None = None
        self._not_before = float("-inf")
        self._deferred_revision: tuple[object, ...] | None = None
        self._deferrals = 0
        self._request_generation = -1
        self._prepared_for: str | None = None
        self._preparing_for: str | None = None
        self._prepare_failed_for: str | None = None
        self._partial_attempts: dict[tuple[int, str | None, str], tuple[int, int]] = {}
        self._pending_update = False
        self._latest_access: AccessProvider | None = None
        self._latest_session: PresentationSession | None = None
        self._submitted_at_ns: int | None = None
        self._submitted_capture_end_ns: int | None = None

    def set_enabled(self, enabled: bool) -> None:
        if self.enabled != enabled:
            self.enabled = enabled
            self._generation += 1
            self._last_revision = None
            self._deferred_revision = None
            self._deferrals = 0
            self._current = None
            self._submitted = None
            self._pending_update = False
            self._latest_access = None
            self._latest_session = None
            self._prepare_failed_for = None
            self.proposed.emit(None)
            self.status.emit("Listening for a clear phrase…" if enabled else "Suggestions off")

    def update(
        self,
        access: AccessProvider | str | None,
        session: PresentationSession | None,
        context: ContextSnapshot,
    ) -> None:
        access = access_provider(access) if access else None
        # Publish immutable snapshots on the GUI thread. Worker reads only this reference.
        previous, self._current = self._current, context
        self._latest_access, self._latest_session = access, session
        if previous is not None and not previous.matches_decision(context):
            self.proposed.emit(None)
        if not self.enabled:
            return
        segments = context.decision_segments
        if session is None or not context.fresh or not segments or not context.segments:
            self._pending_update = False
            self.proposed.emit(None)
            self.status.emit("Listening for a clear phrase…")
            return
        revision = (
            context.session_id,
            context.state_version,
            context.epoch,
            context.stream_id,
            context.language,
            tuple(
                (s.segment_id, s.sequence, s.final, hashlib.sha256(s.text.encode()).hexdigest())
                for s in segments
            ),
        )
        if revision != self._deferred_revision:
            # A server wait applies to that input, not a new command. The API
            # enforces the shared model budget; free navigation must not wait.
            self._not_before = float("-inf")
        partial_key: tuple[int, str | None, str] | None = None
        partial_length = 0
        correction_since_previous = previous is not None and context.has_correction_since(previous)
        if not segments[-1].final:
            if _incomplete_numbered_navigation(segments[-1].text):
                self.status.emit("Waiting for the slide number…")
                return
            partial_key = (context.epoch, context.stream_id, segments[-1].segment_id)
            partial_length = len(segments[-1].text)
            if previous is not None:
                previous_partial = next(
                    (
                        item
                        for item in previous.decision_segments
                        if item.segment_id == segments[-1].segment_id and not item.final
                    ),
                    None,
                )
                if previous_partial is not None and not segments[-1].text.startswith(
                    previous_partial.text
                ):
                    prior_length, attempts = self._partial_attempts.get(partial_key, (0, 0))
                    self._partial_attempts[partial_key] = (
                        min(prior_length, partial_length - 20),
                        attempts,
                    )
            prior_length, attempts = self._partial_attempts.get(partial_key, (0, 0))
            if (
                partial_length < 12
                or attempts >= 3
                or (attempts and partial_length <= prior_length and not correction_since_previous)
            ):
                self.status.emit("Waiting for the next stable word…")
                return
        if revision == self._last_revision:
            return
        # Explicit slide commands are cheap to resolve from the locally loaded
        # presentation. They can also supersede an older remote decision in flight.
        local_navigation = (
            self._local_navigation_method()
            if self._local_navigation_method is not None
            else self.local_navigation
        )
        local = local_navigation(session, context) if local_navigation else None
        if local is not None:
            self._generation += 1
            self._last_revision = revision
            self._pending_update = False
            self._deferred_revision = None
            self._deferrals = 0
            proposed_ns = time.monotonic_ns()
            target = local.get("target")
            tool = target.get("tool", "unknown") if isinstance(target, dict) else "unknown"
            logger.info(
                "presentation_latency event=local_navigation decision_id=%s tool=%s "
                "capture_to_proposal_ms=%d",
                local.get("decision_id", "unknown"),
                tool,
                max(0, (proposed_ns - segments[-1].capture_end_ns) // 1_000_000),
            )
            self.proposed.emit(local)
            self.status.emit("Suggestion ready • slide controls remain manual")
            return
        if time.monotonic() < self._not_before:
            return
        if not access:
            self._pending_update = False
            self.proposed.emit(None)
            self.status.emit("Sign in to request live suggestions…")
            return
        if session.status == "active" and self._prepared_for != session.id:
            if not self.runner.busy and self._prepare_failed_for != session.id:
                self._preparing_for = session.id
                self._request_generation = self._generation
                self.status.emit("Preparing presentation suggestions…")
                self.runner.submit(lambda: self.service.prepare(access(), session))
            return
        if self.runner.busy:
            if self._extends_submitted_decision(context):
                self._pending_update = True
            return
        self._last_revision, self._submitted = revision, context
        if partial_key is not None:
            _, attempts = self._partial_attempts.get(partial_key, (0, 0))
            self._partial_attempts[partial_key] = (partial_length, attempts + 1)
            if len(self._partial_attempts) > 128:
                oldest = next(iter(self._partial_attempts))
                if oldest != partial_key:
                    del self._partial_attempts[oldest]
        self._request_generation = self._generation
        self._submitted_at_ns = time.monotonic_ns()
        submitted_segment = segments[-1]
        self._submitted_capture_end_ns = submitted_segment.capture_end_ns
        if not submitted_segment.final:
            assert partial_key is not None
            _, attempt = self._partial_attempts[partial_key]
            logger.warning(
                "presentation_speech event=interim_submitted attempt=%d stable_chars=%d "
                "capture_age_ms=%d",
                attempt,
                len(submitted_segment.text),
                max(0, (self._submitted_at_ns - submitted_segment.capture_end_ns) // 1_000_000),
            )
            self.status.emit("Checking stable speech while you continue…")
        else:
            self.status.emit("Checking your completed phrase…")

        def current() -> ContextSnapshot:
            # Never call a widget or TranscriptContext from this worker.
            return self._current if self._current is not None else replace(context, fresh=False)

        self.runner.submit(
            lambda: self.service.propose(access(), session, context, current_context=current)
        )

    def _extends_submitted_decision(self, context: ContextSnapshot) -> bool:
        if self._submitted is None:
            return True
        previous = self._submitted.decision_segments
        current = context.decision_segments
        if not current:
            return False
        if not previous:
            return True
        old, new = previous[-1], current[-1]
        return (
            old.segment_id != new.segment_id
            or old.sequence != new.sequence
            or old.final != new.final
            or old.text != new.text
        )

    def _drain_pending(self) -> None:
        if not self._pending_update or self.runner.busy:
            return
        self._pending_update = False
        if self.enabled and self._current is not None:
            self.update(self._latest_access, self._latest_session, self._current)

    def _succeeded(self, _identifier: int, result: object) -> None:
        if self._preparing_for is not None:
            if self.enabled and self._request_generation == self._generation:
                self._prepared_for = self._preparing_for
                self.status.emit("Ready • listening for a clear phrase…")
            self._preparing_for = None
            return
        submitted, self._submitted = self._submitted, None
        submitted_at_ns, self._submitted_at_ns = self._submitted_at_ns, None
        capture_end_ns, self._submitted_capture_end_ns = (
            self._submitted_capture_end_ns,
            None,
        )
        current = self.current_context() if self.current_context is not None else self._current
        valid = (
            self.enabled
            and self._request_generation == self._generation
            and submitted is not None
            and current is not None
            and submitted.matches_decision(current)
        )
        age_ms = (
            max(0, (time.monotonic_ns() - submitted_at_ns) // 1_000_000)
            if submitted_at_ns is not None
            else 0
        )
        if not valid:
            mismatch = (
                submitted.decision_mismatch_reason(current)
                if submitted is not None and current is not None
                else "request_disabled_or_context_missing"
            )
            logger.warning(
                "presentation_speech event=decision_discarded reason=%s request_age_ms=%d",
                mismatch,
                age_ms,
            )
            return
        assert submitted is not None
        if isinstance(result, dict):
            target = result.get("target")
            now_ns = time.monotonic_ns()
            capture_to_submit_ms = (
                max(0, (submitted_at_ns - capture_end_ns) // 1_000_000)
                if submitted_at_ns is not None and capture_end_ns is not None
                else -1
            )
            capture_to_proposal_ms = (
                max(0, (now_ns - capture_end_ns) // 1_000_000) if capture_end_ns is not None else -1
            )
            logger.warning(
                "presentation_latency event=proposal_received decision_id=%s tool=%s "
                "reason=%s capture_to_submit_ms=%d submit_to_proposal_ms=%d "
                "capture_to_proposal_ms=%d server_elapsed_ms=%s",
                result.get("decision_id", "unknown"),
                target.get("tool", "unknown") if isinstance(target, dict) else "unknown",
                result.get("reason", "unknown"),
                capture_to_submit_ms,
                age_ms,
                capture_to_proposal_ms,
                result.get("elapsed_ms", -1),
            )
        if self._pending_update and isinstance(result, dict):
            target = result.get("target")
            if isinstance(target, dict) and target.get("tool") == "no_action":
                logger.warning("presentation_speech event=decision_superseded_by_newer_speech")
                return
            if (
                isinstance(target, dict)
                and target.get("tool") != "no_action"
                and submitted is not None
                and current is not None
                and submitted.decision_segments
                and current.decision_segments
                and submitted.decision_segments[-1].segment_id
                == current.decision_segments[-1].segment_id
            ):
                # This stable utterance already produced an actionable decision. A
                # longer interim revision of that same utterance must not trigger a
                # second selection while the first action is being admitted.
                self._pending_update = False
        if valid:
            if isinstance(result, dict) and result.get("reason") == "paced":
                # No provider attempt was spent. Retain only the live latest input.
                if self._deferred_revision != self._last_revision:
                    self._deferred_revision, self._deferrals = self._last_revision, 0
                self._deferrals += 1
                delay = result.get("retry_after_ms")
                if self._deferrals <= 2 and type(delay) is int and 1 <= delay <= 5000:
                    self._last_revision = None
                    self._not_before = time.monotonic() + delay / 1000
                    self.proposed.emit(None)
                    self.status.emit("Waiting for the next suggestion…")
                    return
            if isinstance(result, dict):
                result = {
                    **result,
                    "_client_context": submitted,
                    "_client_segment_ids": [
                        segment.segment_id for segment in submitted.decision_segments
                    ],
                }
            self.proposed.emit(result)
            reason = result.get("reason") if isinstance(result, dict) else None
            self.status.emit(
                "Decision timed out; Buddy is still listening."
                if reason == "deadline"
                else "Suggestion ready • slide controls remain manual"
            )

    def _failed(self, _identifier: int, message: str) -> None:
        submitted_at_ns, self._submitted_at_ns = self._submitted_at_ns, None
        capture_end_ns, self._submitted_capture_end_ns = (
            self._submitted_capture_end_ns,
            None,
        )
        if submitted_at_ns is not None:
            now_ns = time.monotonic_ns()
            logger.warning(
                "presentation_latency event=proposal_failed submit_to_failure_ms=%d "
                "capture_to_failure_ms=%d",
                max(0, (now_ns - submitted_at_ns) // 1_000_000),
                max(0, (now_ns - capture_end_ns) // 1_000_000)
                if capture_end_ns is not None
                else -1,
            )
        self._submitted = None
        if self._preparing_for is not None:
            self._prepare_failed_for, self._preparing_for = self._preparing_for, None
        if self.enabled and self._request_generation == self._generation:
            self.unavailable.emit()
            self.proposed.emit(None)
            self.status.emit(message)

    def stop(self) -> None:
        self.set_enabled(False)
        self._current = self._submitted = None
        self._pending_update = False
        self._latest_access = None
        self._latest_session = None
        self._last_revision = self._deferred_revision = None
        self.runner.stop()

    def wait_for_shutdown(self) -> None:
        self.stop()
        self.runner.wait_for_shutdown()
