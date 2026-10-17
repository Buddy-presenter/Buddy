"""Temporary per-user Windows URL registration for explicit source-run testing."""

import secrets
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from subprocess import list2cmdline
from typing import Any

from buddy_identity.config import IdentityError


@contextmanager
def windows_source_callback(
    command: Sequence[str], *, scheme: str = "org.buddy.desktop"
) -> Iterator[None]:
    """Restore only our own changes; never overwrite a concurrent installer."""
    if sys.platform != "win32":
        raise IdentityError("Use the installed macOS app for browser sign-in.")
    import ctypes
    import re
    import winreg

    if not re.fullmatch(r"[a-z][a-z0-9.-]+", scheme) or not command:
        raise ValueError("Invalid callback registration.")
    base = "Software\\Classes\\" + scheme
    paths = (base, base + "\\shell", base + "\\shell\\open", base + "\\shell\\open\\command")
    saved: dict[tuple[str, str], tuple[Any, int] | None] = {}
    created: list[str] = []
    registered = False
    value = list2cmdline([*command, "--auth-callback"]) + ' "%1"'
    # Every source launcher writes the same command, so the command alone cannot tell
    # an older launcher that a newer one has since taken over the registration.
    owner = secrets.token_hex(16)

    def notify() -> None:
        # Complete the association notification before a browser can query it.
        # SHCNF_DWORD | SHCNF_FLUSH is Microsoft's registration recommendation.
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1003, None, None)

    def put(path: str, name: str, content: str) -> None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            try:
                saved[path, name] = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                saved[path, name] = None
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, content)

    try:
        for path in paths:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path):
                    pass
            except FileNotFoundError:
                created.append(path)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path):
                pass
        put(base, "", "URL:Buddy source sign-in")
        put(base, "URL Protocol", "")
        put(base, "BuddySourceOwner", owner)
        put(paths[-1], "", value)
        registered = True
        notify()
        yield
    finally:
        try:
            with (
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root,
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, paths[-1]) as key,
            ):
                still_ours = (
                    winreg.QueryValueEx(key, "")[0] == value
                    and winreg.QueryValueEx(root, "BuddySourceOwner")[0] == owner
                )
        except FileNotFoundError:
            still_ours = False
        if not registered or still_ours:
            for (path, name), previous in reversed(list(saved.items())):
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS
                ) as key:
                    if previous is None:
                        winreg.DeleteValue(key, name)
                    else:
                        winreg.SetValueEx(key, name, 0, previous[1], previous[0])
            for path in reversed(created):
                # Preserve unrelated keys/values created concurrently.
                with suppress(OSError):
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            notify()
