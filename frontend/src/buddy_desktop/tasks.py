"""Bounded background work with queued Qt signal delivery and shutdown control."""

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from buddy_desktop.services import ServiceError

logger = logging.getLogger(__name__)


class TaskSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)
    finished = Signal(int)


class Task(QRunnable):
    def __init__(self, identifier: int, operation: Callable[[], object]) -> None:
        super().__init__()
        self.identifier = identifier
        self.operation = operation
        self.signals = TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.operation()
        except ServiceError as exc:
            self.signals.failed.emit(self.identifier, str(exc))
        except Exception:
            # Avoid logging exception messages: a future provider may include private data.
            logger.error("Background operation failed unexpectedly")
            self.signals.failed.emit(
                self.identifier, "Unexpected service failure. Please try again."
            )
        else:
            self.signals.succeeded.emit(self.identifier, result)
        finally:
            self.signals.finished.emit(self.identifier)


class TaskRunner(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)
    idle = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._tasks: dict[int, Task] = {}
        self._next_id = 0
        self._stopping = False

    @property
    def busy(self) -> bool:
        return bool(self._tasks)

    def submit(self, operation: Callable[[], object]) -> int:
        if self._stopping or self.busy:
            raise RuntimeError("The task runner is busy or shutting down.")
        self._next_id += 1
        task = Task(self._next_id, operation)
        task.signals.succeeded.connect(self._succeeded)
        task.signals.failed.connect(self._failed)
        task.signals.finished.connect(self._finished)
        self._tasks[self._next_id] = task
        self.pool.start(task)
        return self._next_id

    @Slot(int, object)
    def _succeeded(self, identifier: int, result: Any) -> None:
        if not self._stopping:
            self.succeeded.emit(identifier, result)

    @Slot(int, str)
    def _failed(self, identifier: int, message: str) -> None:
        if not self._stopping:
            self.failed.emit(identifier, message)

    @Slot(int)
    def _finished(self, identifier: int) -> None:
        self._tasks.pop(identifier, None)
        if not self.busy:
            self.idle.emit()

    def stop(self) -> None:
        # An active HTTP check completes within its timeout; no GUI-thread wait.
        self._stopping = True

    def wait_for_shutdown(self) -> None:
        """Called only after the event loop exits, as a final lifetime safeguard."""
        self.stop()
        self.pool.waitForDone()
