"""Allowlisted slide execution; file IO/decoding in one worker, commits on Qt's thread."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtGui import QImage, QImageReader

from buddy_desktop.slide_contract import (
    Action,
    ContractError,
    Manifest,
    Slide,
    parse_action,
    parse_manifest,
    read_json,
)
from buddy_desktop.slide_surface import SlideSurface


def load_image(manifest: Manifest, slide: Slide) -> QImage:
    """Read one immutable, digest-verified PNG beneath the manifest directory."""
    if manifest.pdf_content is not None:
        from buddy_desktop.pdf_document import render

        return render(manifest.pdf_content, manifest.index(slide.slide_id))[1]
    try:
        path = (manifest.root / slide.image).resolve()
        if not path.is_relative_to(manifest.root):
            raise ContractError("Image path escapes the manifest directory.")
        with path.open("rb") as stream:
            content = stream.read(64 * 1024 * 1024 + 1)
        if len(content) > 64 * 1024 * 1024:
            raise ContractError("Image exceeds the compressed byte limit.")
        if hashlib.sha256(content).hexdigest() != slide.sha256:
            raise ContractError("Image digest does not match the render reference.")
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ContractError("Slide assets must be PNG images.")
        # Probe before decoding so a forged header cannot allocate an unbounded raster.
        from PySide6.QtCore import QBuffer, QIODevice

        buffer = QBuffer()
        buffer.setData(QByteArray(content))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer, QByteArray(b"png"))
        size = reader.size()
        if (size.width(), size.height()) != (slide.raster_width, slide.raster_height):
            raise ContractError("Image dimensions do not match the manifest.")
        image = reader.read()
        if image.isNull():
            raise ContractError("Slide image cannot be decoded.")
        image.setDevicePixelRatio(1)
        return image
    except OSError as exc:
        raise ContractError("Slide image is missing or unreadable.") from exc


@dataclass(frozen=True)
class Completion:
    request_id: int
    instance_id: str | None
    status: str
    message: str
    slide_id: str | None


class _Signals(QObject):
    done = Signal(int, object, str)


class _Work(QRunnable):
    def __init__(self, request_id: int, operation: Callable[[], object]) -> None:
        super().__init__()
        self.request_id, self.operation = request_id, operation
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.operation()
        except ContractError as exc:
            self.signals.done.emit(self.request_id, None, str(exc))
        except Exception:
            self.signals.done.emit(self.request_id, None, "Slide execution failed.")
        else:
            self.signals.done.emit(self.request_id, result, "")


class SlideExecutor(QObject):
    """No command queue: one bounded load, explicit cancellation, exactly one terminal signal.

    Rejected inputs leave state alone. Asset failures/cancellation restore the previous
    committed slide to full view. A new manifest is committed only with its first image.
    """

    completed = Signal(object)  # Completion, including rejected/failed/cancelled requests
    idle = Signal()

    def __init__(self, surface: SlideSurface) -> None:
        super().__init__(surface)
        self.surface = surface
        self.manifest: Manifest | None = None
        self.current_index = 0
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._worker: _Work | None = None
        self._pending: tuple[int, str | None, Action | None] | None = None
        self._sequence = 0
        self._closed = False

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def _request(self) -> int:
        self.surface.check_thread()
        self._sequence += 1
        return self._sequence

    def _finish(self, request: int, instance: str | None, status: str, message: str = "") -> None:
        slide = self.surface.slide
        self.completed.emit(
            Completion(request, instance, status, message, slide.slide_id if slide else None)
        )

    def _start(
        self,
        request: int,
        instance: str | None,
        action: Action | None,
        operation: Callable[[], object],
    ) -> None:
        self._pending = request, instance, action
        self._worker = _Work(request, operation)
        self._worker.signals.done.connect(self._loaded)
        self.pool.start(self._worker)

    def load_manifest(self, path: Path) -> int:
        request = self._request()
        if self._closed or self.busy:
            self._finish(request, None, "rejected", "Executor is busy or closed.")
            return request

        def load() -> tuple[Manifest, QImage]:
            manifest = parse_manifest(read_json(path), path.parent)
            return manifest, load_image(manifest, manifest.slides[0])

        self._start(request, None, None, load)
        return request

    def load_pdf(
        self, source: Path | bytes, *, deck_id: str | None = None, revision: str | None = None
    ) -> int:
        from buddy_desktop.pdf_document import open_pdf

        request = self._request()
        if self._closed or self.busy:
            self._finish(request, None, "rejected", "Executor is busy or closed.")
            return request
        self._start(
            request, None, None, lambda: open_pdf(source, deck_id=deck_id, revision=revision)
        )
        return request

    def dispatch(self, value: object) -> int:
        request = self._request()
        instance: str | None = None
        try:
            if self._closed or self.manifest is None:
                raise ContractError("Executor is closed or has no loaded deck.")
            action = parse_action(value, self.manifest, self.current_index)
            instance = action.instance_id
            # Reset is always available, even while an image is loading.
            if self.busy and action.type != "reset_view":
                raise ContractError("Executor is busy; retry after idle.")
        except ContractError as exc:
            self._finish(request, instance, "rejected", str(exc))
            return request
        if action.type == "reset_view":
            self.cancel()
        if action.type == "highlight_region":
            self.surface.set_highlight(action.region)
            self._finish(request, instance, "completed")
        elif action.target_index == self.current_index:
            # Uses the committed image; never re-read a file to zoom or recover.
            self.surface.set_region(action.region)
            self._finish(request, instance, "completed")
        else:
            manifest = self.manifest
            self._start(
                request,
                instance,
                action,
                lambda: load_image(manifest, manifest.slides[action.target_index]),
            )
        return request

    @Slot(int, object, str)
    def _loaded(self, request: int, result: Any, error: str) -> None:
        self.surface.check_thread()
        self._worker = None
        pending, self._pending = self._pending, None
        if pending is not None:
            _, instance, action = pending
            if error:
                self.surface.reset()
                self._finish(request, instance, "failed", error)
            elif action is None:
                manifest, image = result
                self.manifest, self.current_index = manifest, 0
                slide = manifest.slides[0]
                self.surface.commit(slide, image, slide.full)
                self._finish(request, instance, "completed")
            else:
                assert self.manifest is not None
                self.current_index = action.target_index
                self.surface.commit(
                    self.manifest.slides[action.target_index], result, action.region
                )
                self._finish(request, instance, "completed")
        if not self.busy:
            self.idle.emit()

    def cancel(self) -> None:
        self.surface.check_thread()
        pending, self._pending = self._pending, None
        self.surface.reset()
        if pending is not None:
            self._finish(pending[0], pending[1], "cancelled")

    def close(self) -> None:
        """Call before destroying the surface; wait_for_shutdown after the event loop exits."""
        self.surface.check_thread()
        self._closed = True
        self.cancel()

    def clear(self) -> None:
        self.cancel()
        self.manifest = None
        self.current_index = 0
        self.surface.clear()

    def wait_for_shutdown(self) -> None:
        self.pool.waitForDone()
