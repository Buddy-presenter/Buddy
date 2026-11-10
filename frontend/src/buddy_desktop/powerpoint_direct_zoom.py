"""Direct, owned Windows fullscreen magnification for a PowerPoint slideshow."""

from __future__ import annotations

import ctypes
import math
import threading
from dataclasses import dataclass
from typing import Any

from buddy_desktop.powerpoint_contract import PowerPointError, PresentationInfo
from buddy_desktop.slide_contract import Region


@dataclass(frozen=True)
class ZoomPlan:
    scale: float
    x: int
    y: int


def direct_zoom_plan(
    info: PresentationInfo,
    region: Region,
    client: tuple[int, int, int, int],
    screen: tuple[int, int],
) -> ZoomPlan:
    """Fit a slide region into a single primary display, with a visible margin."""
    left, top, right, bottom = client
    width, height = screen
    if client != (0, 0, width, height) or min(width, height) <= 0:
        raise PowerPointError("Direct zoom requires a fullscreen slideshow on the primary display.")
    if min(info.slide_width, info.slide_height, region.width, region.height) <= 0:
        raise PowerPointError("The PowerPoint zoom region is invalid.")
    slide_scale = min((right - left) / info.slide_width, (bottom - top) / info.slide_height)
    slide_x = left + ((right - left) - info.slide_width * slide_scale) / 2
    slide_y = top + ((bottom - top) - info.slide_height * slide_scale) / 2
    cx = slide_x + (region.x + region.width / 2) * slide_scale
    cy = slide_y + (region.y + region.height / 2) * slide_scale
    scale = min(
        4.0,
        0.94 * width / (region.width * slide_scale),
        0.94 * height / (region.height * slide_scale),
    )
    if not all(math.isfinite(value) for value in (cx, cy, scale)) or scale <= 1.01:
        raise PowerPointError("This region is too large to fit at a magnified level.")
    x = max(0, min(round(width - width / scale), round(cx - width / (2 * scale))))
    y = max(0, min(round(height - height / scale), round(cy - height / (2 * scale))))
    return ZoomPlan(scale, x, y)


class MagnifierSession:
    """Own only our transform; release it on focus loss, reset or process exit."""

    def __init__(self, gui: Any, library: Any | None = None) -> None:
        self.gui = gui
        self.mag = library or getattr(ctypes, "WinDLL")("Magnification.dll", use_last_error=True)
        if library is None:
            self.mag.MagInitialize.restype = ctypes.c_int
            self.mag.MagUninitialize.restype = ctypes.c_int
            self.mag.MagSetFullscreenTransform.argtypes = [
                ctypes.c_float,
                ctypes.c_int,
                ctypes.c_int,
            ]
            self.mag.MagSetFullscreenTransform.restype = ctypes.c_int
            self.mag.MagGetFullscreenTransform.argtypes = [
                ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
            ]
            self.mag.MagGetFullscreenTransform.restype = ctypes.c_int
        self.lock = threading.RLock()
        self.owned: ZoomPlan | None = None
        self.initialized = False
        self.generation = 0

    def _get(self) -> ZoomPlan:
        scale, x, y = ctypes.c_float(), ctypes.c_int(), ctypes.c_int()
        if not self.mag.MagGetFullscreenTransform(
            ctypes.byref(scale), ctypes.byref(x), ctypes.byref(y)
        ):
            raise PowerPointError("Windows could not read the current magnification.")
        return ZoomPlan(scale.value, x.value, y.value)

    def _set(self, plan: ZoomPlan) -> None:
        if not self.mag.MagSetFullscreenTransform(ctypes.c_float(plan.scale), plan.x, plan.y):
            raise PowerPointError("Windows could not set the slideshow magnification.")

    @staticmethod
    def _matches(first: ZoomPlan, second: ZoomPlan) -> bool:
        return (
            abs(first.scale - second.scale) < 0.001 and first.x == second.x and first.y == second.y
        )

    def release(self) -> None:
        with self.lock:
            self.generation += 1
            try:
                if self.owned is not None and self._matches(self._get(), self.owned):
                    self._set(ZoomPlan(1.0, 0, 0))
            finally:
                self.owned = None
                if self.initialized:
                    self.mag.MagUninitialize()
                    self.initialized = False

    def apply(self, hwnd: int, plan: ZoomPlan) -> None:
        with self.lock:
            self.release()
            if not self.mag.MagInitialize():
                raise PowerPointError("Windows magnification is unavailable.")
            self.initialized = True
            try:
                existing = self._get()
                if abs(existing.scale - 1.0) > 0.001:
                    raise PowerPointError(
                        "Another screen magnifier is active. Close it before zooming."
                    )
                self._set(plan)
                self.owned = plan
                if not self._matches(self._get(), plan):
                    raise PowerPointError("Windows did not confirm the requested magnification.")
                self.generation += 1
                generation = self.generation
            except Exception:
                self.release()
                raise

        def watch() -> None:
            while True:
                threading.Event().wait(0.15)
                with self.lock:
                    if self.generation != generation or self.owned is None:
                        return
                    if not self.gui.IsWindow(hwnd) or self.gui.GetForegroundWindow() != hwnd:
                        self.release()
                        return

        threading.Thread(target=watch, name="buddy-zoom-cleanup", daemon=True).start()
