"""Manual native PowerPoint zoom; independent of upload/review action contracts."""

from __future__ import annotations

import ctypes
import importlib
import math
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

from buddy_desktop.powerpoint_contract import PowerPointError, PresentationInfo
from buddy_desktop.slide_contract import Region


def focus_point(
    info: PresentationInfo, region: Region, client: tuple[int, int, int, int]
) -> tuple[int, int]:
    """Map slide points to physical screen pixels, excluding letterbox bars."""
    left, top, right, bottom = client
    width, height = right - left, bottom - top
    if min(width, height, info.slide_width, info.slide_height) <= 0:
        raise PowerPointError("The PowerPoint display area is unavailable.")
    scale = min(width / info.slide_width, height / info.slide_height)
    x = left + (width - info.slide_width * scale) / 2 + (region.x + region.width / 2) * scale
    y = top + (height - info.slide_height * scale) / 2 + (region.y + region.height / 2) * scale
    if not all(math.isfinite(v) for v in (x, y)):
        raise PowerPointError("The PowerPoint focus position is invalid.")
    return round(x), round(y)


class WindowsPowerPointZoom:
    """Invoke PowerPoint's OpticalZoomButton by UIA identity, not text or menu position.

    The temporary PPTOpticalZoom child confirms the focus selector is ready.
    Reset re-enters the same slide without resetting builds; it never sends Escape.
    """

    gui: Any
    api: Any
    types: Any
    uia: Any
    user: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise PowerPointError("Native PowerPoint zoom is available on Windows only.")
        self.gui = importlib.import_module("win32gui")
        self.api = importlib.import_module("win32api")
        client = importlib.import_module("comtypes.client")
        try:
            self.types = importlib.import_module("comtypes.gen.UIAutomationClient")
        except ImportError:
            self.types = client.GetModule("UIAutomationCore.dll")
        self.uia = client.CreateObject(
            self.types.CUIAutomation8, interface=self.types.IUIAutomation2
        )
        self.uia.ConnectionTimeout = 1000
        self.uia.TransactionTimeout = 1000
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p

    @contextmanager
    def _physical_pixels(self) -> Iterator[None]:
        previous = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise PowerPointError("Windows could not provide physical display coordinates.")
        try:
            yield
        finally:
            self.user.SetThreadDpiAwarenessContext(previous)

    def _guard(self, info: PresentationInfo, validate: Callable[[], Any] | None = None) -> None:
        if validate is not None:
            validate()
        if (
            not self.gui.IsWindow(info.window_id)
            or self.gui.IsIconic(info.window_id)
            or self.gui.GetForegroundWindow() != info.window_id
        ):
            raise PowerPointError("PowerPoint lost focus. Return to Buddy and try zoom again.")

    def _zoom_window(self, hwnd: int) -> int | None:
        matches = []

        def collect(child: int, _: Any) -> bool:
            if self.gui.GetClassName(child) == "PPTOpticalZoom" and self.gui.IsWindowVisible(child):
                matches.append(child)
            return True

        self.gui.EnumChildWindows(hwnd, collect, None)
        if len(matches) > 1:
            raise PowerPointError(
                "The PowerPoint zoom display is ambiguous. Reset it in PowerPoint."
            )
        return matches[0] if matches else None

    def _wait(self, info: PresentationInfo, predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 2
        while not predicate():
            self._guard(info)
            if time.monotonic() >= deadline:
                raise PowerPointError(
                    "PowerPoint has not confirmed zoom. Reset zoom and try again."
                )
            time.sleep(0.03)

    def reset(self, info: PresentationInfo, validate: Callable[[], Any]) -> None:
        window = validate()
        window.View.GotoSlide(info.slide_index, 0)
        validate()

    def focus(self, info: PresentationInfo) -> None:
        if self.gui.GetForegroundWindow() != info.window_id:
            # Some Windows builds return an error even when activation succeeds.
            with suppress(Exception):
                self.gui.SetForegroundWindow(info.window_id)
            if self.gui.GetForegroundWindow() != info.window_id:
                with suppress(Exception):
                    self.uia.ElementFromHandle(info.window_id).SetFocus()
        # The actual foreground window is authoritative; never send input elsewhere.
        self._guard(info)

    def zoom(self, info: PresentationInfo, region: Region, validate: Callable[[], Any]) -> None:
        validate()
        hwnd = info.window_id
        if any(self.api.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18, 91, 92)):
            raise PowerPointError("Release the mouse buttons and modifier keys, then try zoom.")
        if not self.gui.IsWindow(hwnd) or self.gui.IsIconic(hwnd):
            raise PowerPointError("Restore the PowerPoint slideshow before zooming.")
        self.reset(info, validate)
        self.focus(info)
        self._guard(info, validate)
        with self._physical_pixels():
            original = self.api.GetCursorPos()
            bounds = self.gui.GetClientRect(hwnd)
            origin = self.gui.ClientToScreen(hwnd, (bounds[0], bounds[1]))
            corner = self.gui.ClientToScreen(hwnd, (bounds[2], bounds[3]))
            target = focus_point(info, region, (*origin, *corner))
            try:
                # The slideshow's client area can include black bars. Hover the
                # actual slide, where PowerPoint reveals its native controls.
                inset = min(16.0, info.slide_width / 4, info.slide_height / 4)
                toolbar_point = focus_point(
                    info,
                    Region(0, info.slide_height - inset, inset, inset),
                    (*origin, *corner),
                )
                self.api.SetCursorPos(toolbar_point)
                root = self.uia.ElementFromHandle(hwnd)
                condition = self.uia.CreatePropertyCondition(30011, "OpticalZoomButton")
                button = root.FindFirst(4, condition)
                deadline = time.monotonic() + 5
                while not button or not button.CurrentIsEnabled:
                    self._guard(info, validate)
                    if time.monotonic() >= deadline:
                        raise PowerPointError(
                            "This PowerPoint slideshow does not expose native zoom."
                        )
                    time.sleep(0.05)
                    button = self.uia.ElementFromHandle(hwnd).FindFirst(4, condition)
                self._guard(info, validate)
                pattern = button.GetCurrentPattern(10000).QueryInterface(
                    self.types.IUIAutomationInvokePattern
                )
                pattern.Invoke()
                # COM reads are unavailable inside the native selector. Check the
                # native window/focus here and rebind the source after selection.
                self._wait(info, lambda: self._zoom_window(hwnd) is not None)
                if (
                    self.gui.GetClientRect(hwnd) != bounds
                    or self.gui.ClientToScreen(hwnd, (0, 0)) != origin
                ):
                    raise PowerPointError("The PowerPoint window moved. Select the area again.")
                self.api.SetCursorPos(target)
                self._guard(info)
                hit = self.gui.WindowFromPoint(target)
                if hit != hwnd and not self.gui.IsChild(hwnd, hit):
                    raise PowerPointError(
                        "The PowerPoint focus area is covered by another window. "
                        "Cancel its zoom selector with Esc and try again."
                    )
                self.api.mouse_event(2, 0, 0, 0, 0)
                self.api.mouse_event(4, 0, 0, 0, 0)
                self._wait(info, lambda: self._zoom_window(hwnd) is None)
                time.sleep(0.15)
                self._guard(info, validate)
            finally:
                if self.gui.GetForegroundWindow() == hwnd:
                    self.api.SetCursorPos(original)
