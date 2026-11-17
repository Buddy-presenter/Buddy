"""Allowlisted slide execution; file IO/decoding in one worker, commits on Qt's thread."""

import hashlib
import logging
import time
from collections import OrderedDict
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

logger = logging.getLogger(__name__)
_IMAGE_CACHE_LIMIT_BYTES = 320 * 1024 * 1024
_PDF_WARMUP_LIMIT_BYTES = _IMAGE_CACHE_LIMIT_BYTES
_PDF_WARMUP_MAX_PAGES = 30


def _guard_allows(guard: Callable[[], bool] | None) -> bool:
    try:
        return guard is None or guard()
    except Exception:
        return False  # Callback failure cannot grant an automatic player commit.


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
    pdf_warmup_progress = Signal(int, int)
    pdf_warmup_finished = Signal(str)

    def __init__(self, surface: SlideSurface) -> None:
        super().__init__(surface)
        self.surface = surface
        self.manifest: Manifest | None = None
        self.current_index = 0
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.prefetch_pool = QThreadPool(self)
        # Two isolated PDF workers shorten the pre-presentation warmup without
        # letting a deck's full raster set decode concurrently without bounds.
        self.prefetch_pool.setMaxThreadCount(2)
        self._worker: _Work | None = None
        self._prefetch_worker: _Work | None = None
        self._prefetch_key: tuple[str, str, str] | None = None
        self._warmup_workers: set[_Work] = set()
        self._warmup_generation = 0
        self._warmup_queue: list[Slide] = []
        self._warmup_active = False
        self._warmup_total = 0
        self._warmup_completed = 0
        self._warmup_failed = False
        self._warmup_started_ns = 0
        self._image_cache: OrderedDict[tuple[str, str, str], QImage] = OrderedDict()
        self._image_cache_bytes = 0
        self._pending: tuple[int, str | None, Action | None] | None = None
        self._commit_guard: Callable[[], bool] | None = None
        self._sequence = 0
        self._closed = False

    @property
    def busy(self) -> bool:
        return self._worker is not None or self._warmup_active

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

    @staticmethod
    def _cache_key(manifest: Manifest, slide: Slide) -> tuple[str, str, str]:
        return manifest.deck_id, manifest.render_id, slide.slide_id

    def _cached_image(self, key: tuple[str, str, str]) -> QImage | None:
        image = self._image_cache.pop(key, None)
        if image is not None:
            self._image_cache[key] = image
        return image

    def _remember_image(self, key: tuple[str, str, str], image: QImage) -> None:
        size = int(image.sizeInBytes())
        if size <= 0 or size > _IMAGE_CACHE_LIMIT_BYTES:
            return
        previous = self._image_cache.pop(key, None)
        if previous is not None:
            self._image_cache_bytes -= int(previous.sizeInBytes())
        while self._image_cache and self._image_cache_bytes + size > _IMAGE_CACHE_LIMIT_BYTES:
            _old_key, old_image = self._image_cache.popitem(last=False)
            self._image_cache_bytes -= int(old_image.sizeInBytes())
        self._image_cache[key] = image
        self._image_cache_bytes += size

    def prefetch_slide(self, slide_id: str) -> None:
        """Render one selected PDF destination while its server preflight is running."""
        manifest = self.manifest
        if (
            self._closed
            or self._warmup_active
            or manifest is None
            or manifest.pdf_content is None
            or self._prefetch_worker is not None
        ):
            return
        slide = next((item for item in manifest.slides if item.slide_id == slide_id), None)
        if slide is None:
            return
        key = self._cache_key(manifest, slide)
        if key in self._image_cache or self._prefetch_key == key:
            return
        self._prefetch_key = key
        started = time.monotonic_ns()
        worker = _Work(0, lambda: (key, load_image(manifest, slide)))
        worker.signals.done.connect(
            lambda _request, result, error: self._prefetched(key, started, result, error)
        )
        self._prefetch_worker = worker
        self.prefetch_pool.start(worker, priority=-1)

    def _cancel_pdf_warmup(self) -> None:
        was_active = self._warmup_active
        self._warmup_generation += 1
        self._warmup_queue.clear()
        self._warmup_active = False
        self._warmup_total = 0
        self._warmup_completed = 0
        if was_active:
            self.pdf_warmup_finished.emit("cancelled")

    def _start_pdf_warmup(self, manifest: Manifest) -> None:
        if manifest.pdf_content is None:
            self.pdf_warmup_finished.emit("not_applicable")
            return
        decoded_bytes = sum(
            slide.raster_width * slide.raster_height * 4 for slide in manifest.slides
        )
        if len(manifest.slides) > _PDF_WARMUP_MAX_PAGES or decoded_bytes > _PDF_WARMUP_LIMIT_BYTES:
            logger.info(
                "presentation_latency event=pdf_warmup outcome=skipped pages=%d "
                "estimated_bytes=%d cache_limit_bytes=%d",
                len(manifest.slides),
                decoded_bytes,
                _PDF_WARMUP_LIMIT_BYTES,
            )
            self.pdf_warmup_finished.emit("skipped")
            return
        self._warmup_generation += 1
        self._warmup_active = True
        self._warmup_failed = False
        self._warmup_total = len(manifest.slides)
        self._warmup_completed = 1  # The first page was rendered to open the PDF.
        self._warmup_started_ns = time.monotonic_ns()
        self._warmup_queue = list(manifest.slides[1:])
        self.pdf_warmup_progress.emit(self._warmup_completed, self._warmup_total)
        if not self._warmup_queue:
            self._finish_pdf_warmup(self._warmup_generation)
            return
        self._start_next_pdf_warmup(self._warmup_generation)

    def _start_next_pdf_warmup(self, generation: int) -> None:
        if generation != self._warmup_generation or not self._warmup_active:
            return
        manifest = self.manifest
        if manifest is None or manifest.pdf_content is None:
            self._finish_pdf_warmup(generation)
            return
        while self._warmup_queue and len(self._warmup_workers) < 2:
            slide = self._warmup_queue.pop(0)
            key = self._cache_key(manifest, slide)
            if self._cached_image(key) is not None:
                self._warmup_completed += 1
                self.pdf_warmup_progress.emit(self._warmup_completed, self._warmup_total)
                continue

            def render_page(
                current_key: tuple[str, str, str] = key,
                current_slide: Slide = slide,
                current_manifest: Manifest = manifest,
            ) -> tuple[tuple[str, str, str], QImage]:
                return current_key, load_image(current_manifest, current_slide)

            worker = _Work(0, render_page)
            self._warmup_workers.add(worker)
            worker.signals.done.connect(
                lambda _request, result, error, worker=worker, key=key: self._pdf_warmup_page_done(
                    generation, worker, key, result, error
                )
            )
            self.prefetch_pool.start(worker)
        if not self._warmup_queue and not self._warmup_workers:
            self._finish_pdf_warmup(generation)

    def _pdf_warmup_page_done(
        self,
        generation: int,
        worker: _Work,
        key: tuple[str, str, str],
        result: Any,
        error: str,
    ) -> None:
        self.surface.check_thread()
        self._warmup_workers.discard(worker)
        if generation != self._warmup_generation:
            if self._warmup_active:
                self._start_next_pdf_warmup(self._warmup_generation)
            return
        manifest = self.manifest
        valid_manifest = (
            not self._closed
            and manifest is not None
            and (manifest.deck_id, manifest.render_id) == key[:2]
        )
        image = result[1] if isinstance(result, tuple) and len(result) == 2 else None
        if valid_manifest and not error and isinstance(image, QImage) and not image.isNull():
            self._remember_image(key, image)
        else:
            self._warmup_failed = True
        self._warmup_completed += 1
        self.pdf_warmup_progress.emit(self._warmup_completed, self._warmup_total)
        self._start_next_pdf_warmup(generation)

    def _finish_pdf_warmup(self, generation: int) -> None:
        if (
            generation != self._warmup_generation
            or not self._warmup_active
            or self._warmup_queue
            or self._warmup_workers
        ):
            return
        self._warmup_active = False
        manifest = self.manifest
        if manifest is not None:
            current = manifest.slides[self.current_index]
            self._cached_image(self._cache_key(manifest, current))
        outcome = "partial" if self._warmup_failed else "ready"
        logger.info(
            "presentation_latency event=pdf_warmup outcome=%s pages=%d elapsed_ms=%d "
            "cache_bytes=%d",
            outcome,
            self._warmup_total,
            max(0, (time.monotonic_ns() - self._warmup_started_ns) // 1_000_000),
            self._image_cache_bytes,
        )
        self.pdf_warmup_finished.emit(outcome)

    def _prefetched(
        self,
        key: tuple[str, str, str],
        started_ns: int,
        result: Any,
        error: str,
    ) -> None:
        self.surface.check_thread()
        self._prefetch_worker = None
        if self._prefetch_key == key:
            self._prefetch_key = None
        manifest = self.manifest
        valid_manifest = (
            not self._closed
            and manifest is not None
            and (manifest.deck_id, manifest.render_id) == key[:2]
        )
        image = result[1] if isinstance(result, tuple) and len(result) == 2 else None
        if valid_manifest and not error and isinstance(image, QImage) and not image.isNull():
            self._remember_image(key, image)
            outcome = "ready"
        else:
            outcome = "discarded" if not valid_manifest else "unavailable"
        logger.info(
            "presentation_latency event=pdf_prefetch outcome=%s elapsed_ms=%d cache_bytes=%d",
            outcome,
            max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
            self._image_cache_bytes,
        )

    def load_manifest(self, path: Path) -> int:
        self._cancel_pdf_warmup()
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

        self._cancel_pdf_warmup()
        request = self._request()
        if self._closed or self.busy:
            self._finish(request, None, "rejected", "Executor is busy or closed.")
            return request
        self._start(
            request, None, None, lambda: open_pdf(source, deck_id=deck_id, revision=revision)
        )
        return request

    def dispatch(self, value: object, *, commit_guard: Callable[[], bool] | None = None) -> int:
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
            if not _guard_allows(commit_guard):
                raise ContractError("The automatic transition expired before execution.")
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
            slide = manifest.slides[action.target_index]
            key = self._cache_key(manifest, slide)
            cached = self._cached_image(key)
            if cached is not None:
                self.current_index = action.target_index
                self.surface.commit(slide, cached, action.region)
                self._finish(request, instance, "completed")
                logger.info(
                    "presentation_latency event=pdf_prefetch outcome=hit cache_bytes=%d",
                    self._image_cache_bytes,
                )
                return request
            self._start(
                request,
                instance,
                action,
                lambda: load_image(manifest, slide),
            )
            self._commit_guard = commit_guard
        return request

    @Slot(int, object, str)
    def _loaded(self, request: int, result: Any, error: str) -> None:
        self.surface.check_thread()
        self._worker = None
        pending, self._pending = self._pending, None
        guard, self._commit_guard = self._commit_guard, None
        if pending is not None:
            _, instance, action = pending
            if not _guard_allows(guard):
                # A stale automatic load leaves the last committed view untouched.
                self._finish(request, instance, "rejected", "The automatic transition expired.")
            elif error:
                self.surface.reset()
                self._finish(request, instance, "failed", error)
            elif action is None:
                manifest, image = result
                self.manifest, self.current_index = manifest, 0
                slide = manifest.slides[0]
                self._remember_image(self._cache_key(manifest, slide), image)
                self.surface.commit(slide, image, slide.full)
                self._start_pdf_warmup(manifest)
                self._finish(request, instance, "completed")
            else:
                assert self.manifest is not None
                slide = self.manifest.slides[action.target_index]
                self.current_index = action.target_index
                if isinstance(result, QImage):
                    self._remember_image(self._cache_key(self.manifest, slide), result)
                self.surface.commit(slide, result, action.region)
                self._finish(request, instance, "completed")
        if not self.busy:
            self.idle.emit()

    def cancel(self) -> None:
        self.surface.check_thread()
        pending, self._pending = self._pending, None
        self._commit_guard = None
        self.surface.reset()
        if pending is not None:
            self._finish(pending[0], pending[1], "cancelled")

    def close(self) -> None:
        """Call before destroying the surface; wait_for_shutdown after the event loop exits."""
        self.surface.check_thread()
        self._closed = True
        self._cancel_pdf_warmup()
        self._image_cache.clear()
        self._image_cache_bytes = 0
        self.cancel()

    def clear(self) -> None:
        self._cancel_pdf_warmup()
        self.cancel()
        self.manifest = None
        self.current_index = 0
        self._image_cache.clear()
        self._image_cache_bytes = 0
        self.surface.clear()

    def wait_for_shutdown(self) -> None:
        self.pool.waitForDone()
        self.prefetch_pool.waitForDone()
