"""Launch safe Google searches in the user's installed Google Chrome browser."""

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from PySide6.QtCore import QProcess


@dataclass(frozen=True)
class BrowserLaunchResult:
    opened: bool
    message: str


def google_search_url(query: str, *, images: bool = False) -> str:
    parameters = {"q": query}
    if images:
        parameters["tbm"] = "isch"
    return "https://www.google.com/search?" + urlencode(parameters)


def _registered_windows_chrome() -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    registry: Any = winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for hive in (registry.HKEY_CURRENT_USER, registry.HKEY_LOCAL_MACHINE):
        views = (
            0,
            getattr(registry, "KEY_WOW64_64KEY", 0),
            getattr(registry, "KEY_WOW64_32KEY", 0),
        )
        for view in views:
            try:
                with registry.OpenKey(hive, key_path, 0, registry.KEY_READ | view) as key:
                    executable, _ = registry.QueryValueEx(key, "")
                if isinstance(executable, str) and Path(executable).is_file():
                    return executable
            except OSError:
                continue
    return None


def find_chrome(*, system: str | None = None, environ: dict[str, str] | None = None) -> str | None:
    platform = system or sys.platform
    env = os.environ if environ is None else environ
    if platform == "win32":
        registered = _registered_windows_chrome()
        if registered:
            return registered
        roots = [
            env.get("PROGRAMFILES"),
            env.get("PROGRAMFILES(X86)"),
            env.get("LOCALAPPDATA"),
        ]
        for root in roots:
            if root:
                candidate = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if candidate.is_file():
                    return str(candidate)
        return shutil.which("chrome.exe")
    if platform == "darwin":
        for candidate in (
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ):
            if candidate.is_file():
                return str(candidate)
        return shutil.which("google-chrome")
    return shutil.which("google-chrome") or shutil.which("google-chrome-stable")


def launch_chrome_search(
    query: str,
    *,
    images: bool = False,
    system: str | None = None,
    environ: dict[str, str] | None = None,
) -> BrowserLaunchResult:
    """Open a new Chrome tab with an encoded search URL; never invoke a shell."""
    normalized = " ".join(query.split())
    if not normalized or len(normalized) > 300:
        return BrowserLaunchResult(False, "That search query is empty or too long.")
    platform = system or sys.platform
    executable = find_chrome(system=platform, environ=environ)
    if executable is None:
        return BrowserLaunchResult(False, "Google Chrome could not be found on this computer.")
    url = google_search_url(normalized, images=images)
    if platform == "darwin":
        program, arguments = "open", ["-a", "Google Chrome", "--args", "--new-tab", url]
    else:
        program, arguments = executable, ["--new-tab", url]
    try:
        result = QProcess.startDetached(program, arguments)
        started = result[0] if isinstance(result, tuple) else bool(result)
    except (OSError, RuntimeError):
        started = False
    if not started:
        return BrowserLaunchResult(False, "Google Chrome could not be opened.")
    return BrowserLaunchResult(True, "")
