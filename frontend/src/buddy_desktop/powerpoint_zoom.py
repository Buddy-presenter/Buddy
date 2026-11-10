"""Direct Windows magnification of a source-bound PowerPoint slideshow."""

from __future__ import annotations

import ctypes
import importlib
import math
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

from buddy_desktop.powerpoint_contract import PowerPointError, PresentationInfo
from buddy_desktop.powerpoint_direct_zoom import MagnifierSession, direct_zoom_plan
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
    """Focus the selected slide and apply one owned Windows magnification transform."""

    gui: Any
    api: Any
    types: Any
    uia: Any
    user: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise PowerPointError("PowerPoint zoom is available on Windows only.")
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
        self.magnifier = MagnifierSession(self.gui)

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

    def reset(self, info: PresentationInfo, validate: Callable[[], Any]) -> None:
        self.magnifier.release()
        window = validate()
        window.View.GotoSlide(info.slide_index, 0)
        validate()

    def focus(self, info: PresentationInfo) -> None:
        if self.gui.GetForegroundWindow() != info.window_id:
            with suppress(Exception):
                self.gui.SetForegroundWindow(info.window_id)
            if self.gui.GetForegroundWindow() != info.window_id:
                with suppress(Exception):
                    self.uia.ElementFromHandle(info.window_id).SetFocus()
        self._guard(info)

    def zoom(self, info: PresentationInfo, region: Region, validate: Callable[[], Any]) -> None:
        validate()
        hwnd = info.window_id
        if any(self.api.GetAsyncKeyState(k) & 0x8000 for k in (1, 2, 4, 16, 17, 18, 91, 92)):
            raise PowerPointError("Release the mouse buttons and modifier keys, then try zoom.")
        if not self.gui.IsWindow(hwnd) or self.gui.IsIconic(hwnd):
            raise PowerPointError("Restore the PowerPoint slideshow before zooming.")
        self.magnifier.release()
        self.focus(info)
        self._guard(info, validate)
        with self._physical_pixels():
            if self.api.GetSystemMetrics(80) != 1:  # SM_CMONITORS
                raise PowerPointError("Direct zoom currently requires a single display.")
            bounds = self.gui.GetClientRect(hwnd)
            origin = self.gui.ClientToScreen(hwnd, (bounds[0], bounds[1]))
            corner = self.gui.ClientToScreen(hwnd, (bounds[2], bounds[3]))
            screen = (self.api.GetSystemMetrics(0), self.api.GetSystemMetrics(1))
            plan = direct_zoom_plan(info, region, (*origin, *corner), screen)
            target = focus_point(info, region, (*origin, *corner))
            hit = self.gui.WindowFromPoint(target)
            if hit != hwnd and not self.gui.IsChild(hwnd, hit):
                raise PowerPointError("The PowerPoint focus area is covered by another window.")
            self._guard(info, validate)
            self.magnifier.apply(hwnd, plan)
            try:
                self._guard(info, validate)
                if (
                    self.gui.GetClientRect(hwnd) != bounds
                    or self.gui.ClientToScreen(hwnd, (bounds[0], bounds[1])) != origin
                ):
                    raise PowerPointError("The PowerPoint window moved. Select the area again.")
            except Exception:
                self.magnifier.release()
                raise
