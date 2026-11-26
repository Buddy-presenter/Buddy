"""Best-effort foreground handoff for voice-driven return from an external browser."""

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Any

from PySide6.QtWidgets import QWidget


def _user32() -> Any:
    # WinDLL is only present on Windows. Treat ctypes dynamically so mypy on
    # macOS/Linux does not reject the Windows-only symbol.
    ctypes_api: Any = ctypes
    user32 = ctypes_api.WinDLL("user32", use_last_error=True)
    user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, wintypes.INT]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    return user32


def allow_current_process_foreground() -> bool:
    """Reserve Windows' foreground permission before Buddy launches Chrome."""
    if sys.platform != "win32":
        return True
    try:
        user32 = _user32()
        return bool(user32.AllowSetForegroundWindow(os.getpid()))
    except (AttributeError, OSError, TypeError):
        return False


def activate_native_window(handle: int) -> bool:
    """Request native activation and report whether Windows actually focused it."""
    if handle <= 0:
        return False
    if sys.platform != "win32":
        return False
    try:
        user32 = _user32()
        hwnd = wintypes.HWND(handle)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        focused = bool(user32.SetForegroundWindow(hwnd))
        foreground = user32.GetForegroundWindow()
        foreground_handle = foreground if isinstance(foreground, int) else foreground.value
        if foreground_handle == handle:
            return True
        if focused or not foreground_handle:
            return False

        # A voice reply arrives while Chrome owns foreground focus, so Windows may
        # reject SetForegroundWindow even though Buddy reserved permission before
        # launching Chrome. Temporarily share the two windows' input queues and try
        # the foreground request once more. Always detach immediately afterward.
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        if not target_thread or not foreground_thread or target_thread == foreground_thread:
            return False
        if not user32.AttachThreadInput(target_thread, foreground_thread, True):
            return False
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            foreground = user32.GetForegroundWindow()
            foreground_handle = foreground if isinstance(foreground, int) else foreground.value
            return foreground_handle == handle
        finally:
            user32.AttachThreadInput(target_thread, foreground_thread, False)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def activate_qt_window(window: QWidget) -> bool:
    """Show and raise a Buddy window, then verify Windows granted foreground focus."""
    try:
        if window.isMinimized():
            window.showNormal()
        elif not window.isVisible():
            window.show()
        window.raise_()
        window.activateWindow()
        if sys.platform == "win32":
            return activate_native_window(int(window.winId()))
        return True
    except (RuntimeError, TypeError, ValueError):
        return False


def native_window_rect(handle: int) -> tuple[int, int, int, int] | None:
    """A native window's (left, top, right, bottom) in physical pixels, or None."""
    if handle <= 0 or sys.platform != "win32":
        return None
    try:
        user32 = _user32()
        hwnd = wintypes.HWND(handle)
        rect = wintypes.RECT()
        if not user32.IsWindow(hwnd) or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except (AttributeError, OSError, TypeError, ValueError):
        return None
