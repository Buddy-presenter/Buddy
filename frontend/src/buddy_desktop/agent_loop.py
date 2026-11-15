"""Latest-only, bounded native scheduling. All mutable scheduling state belongs to Qt."""

import time
from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from buddy_desktop.agent_service import AgentService
from buddy_desktop.presentation_session_service import PresentationSession
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.transcript_context import ContextSnapshot


class AgentLoop(QObject):
    status = Signal(str)
    proposed = Signal(object)

    def __init__(
        self,
        service: AgentService,
        parent: QObject | None = None,
        current_context: Callable[[], ContextSnapshot] | None = None,
    ):
        super().__init__(parent)
        self.service = service
        self.current_context = current_context
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self._succeeded)
        self.runner.failed.connect(self._failed)
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

    def set_enabled(self, enabled: bool) -> None:
        if self.enabled != enabled:
            self.enabled = enabled
            self._generation += 1
            self._last_revision = None
            self._deferred_revision = None
            self._deferrals = 0
            self._current = None
            self._submitted = None
            self._prepare_failed_for = None
            self.proposed.emit(None)
            self.status.emit("Waiting for completed speech…" if enabled else "Suggestions off")

    def update(
        self, access: str | None, session: PresentationSession | None, context: ContextSnapshot
    ) -> None:
        # Publish immutable snapshots on the GUI thread. Worker reads only this reference.
        previous, self._current = self._current, context
        if previous is not None and not previous.matches(context):
            self.proposed.emit(None)
        if not self.enabled:
            return
        if (
            access
            and session is not None
            and session.status == "active"
            and self._prepared_for != session.id
        ):
            if not self.runner.busy and self._prepare_failed_for != session.id:
                self._preparing_for = session.id
                self._request_generation = self._generation
                self.status.emit("Preparing presentation suggestions…")
                self.runner.submit(lambda: self.service.prepare(access, session))
            return
        if not access or session is None or not context.fresh or not context.candidate_segments:
            self.proposed.emit(None)
            self.status.emit("Waiting for completed speech…")
            return
        revision = (
            context.session_id,
            context.state_version,
            context.epoch,
            context.stream_id,
            context.language,
            # Revisions are immutable under the speech contract. Dedupe needs ids,
            # not another long-lived copy of private transcript text.
            tuple((s.segment_id, s.sequence) for s in context.segments),
        )
        if (
            self.runner.busy
            or revision == self._last_revision
            or time.monotonic() < self._not_before
        ):
            return
        self._last_revision, self._submitted = revision, context
        self._not_before, self._request_generation = time.monotonic() + 3.2, self._generation
        self.status.emit("Considering your latest speech…")

        def current() -> ContextSnapshot:
            # Never call a widget or TranscriptContext from this worker.
            return self._current if self._current is not None else replace(context, fresh=False)

        self.runner.submit(
            lambda: self.service.propose(access, session, context, current_context=current)
        )

    def _succeeded(self, _identifier: int, result: object) -> None:
        if self._preparing_for is not None:
            if self.enabled and self._request_generation == self._generation:
                self._prepared_for = self._preparing_for
                self.status.emit("Ready • waiting for completed speech…")
            self._preparing_for = None
            return
        submitted, self._submitted = self._submitted, None
        current = self.current_context() if self.current_context is not None else self._current
        if (
            self.enabled
            and self._request_generation == self._generation
            and submitted is not None
            and current is not None
            and submitted.matches(current)
        ):
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
            self.proposed.emit(result)
            self.status.emit("Suggestion ready • slide controls remain manual")

    def _failed(self, _identifier: int, message: str) -> None:
        self._submitted = None
        if self._preparing_for is not None:
            self._prepare_failed_for, self._preparing_for = self._preparing_for, None
        if self.enabled and self._request_generation == self._generation:
            self.proposed.emit(None)
            self.status.emit(message)

    def stop(self) -> None:
        self.set_enabled(False)
        self._current = self._submitted = None
        self._last_revision = self._deferred_revision = None
        self.runner.stop()

    def wait_for_shutdown(self) -> None:
        self.stop()
        self.runner.wait_for_shutdown()
