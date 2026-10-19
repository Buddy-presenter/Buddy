"""Windows desktop PowerPoint launch and slideshow discovery.

All COM work is performed inside the calling background task. COM interfaces are never
shared with Qt widgets or retained between tasks; a connected slideshow is identified by
its canonical source path and is revalidated before Buddy reports it as connected.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psutil  # type: ignore[import-untyped]

from buddy_desktop.services import ServiceError


class PowerPointError(ServiceError):
    """A user-actionable PowerPoint operation failure without private path details."""


@dataclass(frozen=True)
class PresentationInfo:
    """A snapshot of an active PowerPoint slideshow."""

    path: str
    name: str
    slide_index: int
    slide_count: int

    @property
    def identity(self) -> str:
        """Canonical local identity used to avoid attaching to a different deck."""
        return canonical_path(self.path)

    @property
    def choice_label(self) -> str:
        return f"{self.name} — slide {self.slide_index} of {self.slide_count} ({self.path})"


def canonical_path(value: str | Path) -> str:
    """Return a normalized absolute path, with Windows case folding when applicable."""
    path = os.path.realpath(os.fspath(value))
    return os.path.normcase(path)


class PowerPointAutomation:
    """Small late-bound COM adapter; methods return plain data only."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        com_loader: Callable[[], tuple[Any, Any]] | None = None,
        progid_probe: Callable[[], None] | None = None,
        process_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._platform_name = platform_name or sys.platform
        self._com_loader = com_loader
        self._progid_probe = progid_probe
        self._process_probe = process_probe or self._powerpoint_process_running

    @staticmethod
    def _powerpoint_process_running() -> bool:
        try:
            for process in psutil.process_iter(["name"]):
                try:
                    if (process.info["name"] or "").casefold() == "powerpnt.exe":
                        return True
                except psutil.Error:
                    continue
        except psutil.Error:
            return False
        return False

    def _com_modules(self) -> tuple[Any, Any]:
        if self._platform_name != "win32":
            raise PowerPointError("Desktop PowerPoint control is available on Windows only.")
        if self._com_loader is not None:
            return self._com_loader()
        try:
            modules = (
                importlib.import_module("pythoncom"),
                importlib.import_module("win32com.client"),
            )
        except ImportError as exc:
            raise PowerPointError(
                "PowerPoint support is unavailable. Restart Buddy or reinstall its Windows app."
            ) from exc
        return cast(tuple[Any, Any], modules)

    def _start_com(self) -> tuple[Any, Any]:
        pythoncom, client = self._com_modules()
        try:
            if self._progid_probe is not None:
                self._progid_probe()
            else:
                import winreg

                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "PowerPoint.Application\\CLSID"):
                    pass
        except Exception as exc:
            raise PowerPointError(
                "Desktop Microsoft PowerPoint is not installed or registered on this computer."
            ) from exc
        pythoncom.CoInitialize()
        return pythoncom, client

    @staticmethod
    def _end_com(pythoncom: Any) -> None:
        pythoncom.CoUninitialize()

    @staticmethod
    def _snapshot(slideshow: Any) -> PresentationInfo:
        presentation = slideshow.Presentation
        path = str(presentation.FullName)
        name = str(presentation.Name)
        view = slideshow.View
        try:
            slide_index = int(view.Slide.SlideIndex)
        except Exception:
            # Some PowerPoint documents expose the current show position even when the
            # late-bound Slide object is unavailable during slideshow startup.
            try:
                slide_index = int(view.CurrentShowPosition)
            except Exception as exc:
                raise PowerPointError("PowerPoint has not displayed a slide yet.") from exc
        slide_count = int(presentation.Slides.Count)
        if not path or not name or slide_count < 1 or not 1 <= slide_index <= slide_count:
            raise PowerPointError("PowerPoint has not displayed a slide yet.")
        return PresentationInfo(path, name, slide_index, slide_count)

    @staticmethod
    def _validate_file(path: str | Path) -> Path:
        try:
            source = Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PowerPointError("Choose an existing PowerPoint presentation file.") from exc
        if source.suffix.casefold() != ".pptx" or not source.is_file():
            raise PowerPointError("Choose a PowerPoint .pptx presentation.")
        return source

    def launch(self, path: str | Path) -> PresentationInfo:
        """Open a local PPTX read-only in a new PowerPoint instance and start its show."""
        source = self._validate_file(path)
        pythoncom, client = self._start_com()
        try:
            return self._launch_in_com(client, source)
        finally:
            self._end_com(pythoncom)

    def _launch_in_com(self, client: Any, source: Path) -> PresentationInfo:
        try:
            # Dispatch uses PowerPoint's registered running instance so later COM calls
            # from Buddy's worker threads can reconnect to this same application.
            application = client.Dispatch("PowerPoint.Application")
            application.Visible = True
            original_security = application.AutomationSecurity
            try:
                # Disable any embedded automation while Office opens the chosen file.
                application.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
                presentation = application.Presentations.Open(
                    str(source), ReadOnly=-1, Untitled=0, WithWindow=-1
                )
                # SharePoint/OneDrive-backed files can be opened as an HTTPS URL even
                # when the user selected a local synced path. Match the slideshow to
                # the identity Office actually assigned to this opened presentation.
                opened_identity = canonical_path(str(presentation.FullName))
            finally:
                application.AutomationSecurity = original_security
            # Timed decks can finish before Buddy attaches and begins polling. Keep
            # playback under the presenter's control for this read-only session.
            presentation.SlideShowSettings.AdvanceMode = 1  # ppSlideShowManualAdvance
            # PowerPoint's Run method does not consistently return a SlideShowWindow.
            # Discover the newly active show from the owning application instead.
            launched_slideshow = presentation.SlideShowSettings.Run()
            deadline = time.monotonic() + 5
            while True:
                try:
                    # PowerPoint normally returns the new window here, but some Office
                    # builds return None or a stale proxy. Prefer a valid returned window,
                    # then fall back to enumerating the application's active shows.
                    if launched_slideshow is not None:
                        try:
                            info = self._snapshot(launched_slideshow)
                            if info.identity == opened_identity:
                                return info
                        except Exception:
                            pass
                    matches = tuple(
                        info
                        for info in self._all_slideshows(application)
                        if info.identity == opened_identity
                    )
                    if len(matches) == 1:
                        return matches[0]
                    if len(matches) > 1:
                        raise PowerPointError(
                            "More than one slideshow is using that file. "
                            "Close the duplicate and reconnect."
                        )
                except PowerPointError as exc:
                    if str(exc).startswith("More than one slideshow"):
                        raise
                except Exception:
                    # Office may reject calls briefly while bringing the show to front.
                    pass
                if time.monotonic() >= deadline:
                    raise PowerPointError(
                        "PowerPoint opened the file but did not show a slide. "
                        "Check for a PowerPoint prompt, then try again."
                    )
                time.sleep(0.1)
        except PowerPointError:
            raise
        except Exception as exc:
            raise PowerPointError(
                "PowerPoint could not open this presentation or start its slideshow."
            ) from exc

    def _running_slideshows(self) -> tuple[PresentationInfo, ...]:
        # Calling Dispatch with no existing PowerPoint process would start an empty Office
        # instance. Avoid that side effect when checking for ended/closed slideshows.
        if not self._process_probe():
            return ()
        pythoncom, client = self._start_com()
        try:
            return self._list_in_com(client)
        finally:
            self._end_com(pythoncom)

    def _list_in_com(self, client: Any) -> tuple[PresentationInfo, ...]:
        try:
            application = client.Dispatch("PowerPoint.Application")
        except Exception:
            return ()
        try:
            return self._all_slideshows(application)
        except PowerPointError:
            raise
        except Exception as exc:
            raise PowerPointError("Cannot read the active PowerPoint slideshow state.") from exc

    def _all_slideshows(self, application: Any) -> tuple[PresentationInfo, ...]:
        windows = application.SlideShowWindows
        snapshots = []
        for index in range(1, windows.Count + 1):
            window = windows.Item(index)
            try:
                snapshots.append(self._snapshot(window))
            except PowerPointError:
                # A just-starting/transitioning window can briefly lack a current slide.
                # Treat a finished show as gone; keep retrying other transient states.
                try:
                    if int(window.View.State) == 5:  # ppSlideShowDone
                        continue
                except Exception as exc:
                    raise PowerPointError(
                        "PowerPoint is temporarily busy. Buddy will retry the status check."
                    ) from exc
                raise PowerPointError(
                    "PowerPoint is temporarily busy. Buddy will retry the status check."
                ) from None
            except Exception as exc:
                raise PowerPointError(
                    "PowerPoint is temporarily busy. Buddy will retry the status check."
                ) from exc
        return tuple(snapshots)

    def _matching_slideshows(self, application: Any, source: Path) -> tuple[PresentationInfo, ...]:
        identity = canonical_path(source)
        return tuple(
            info for info in self._all_slideshows(application) if info.identity == identity
        )

    def list_slideshows(self) -> tuple[PresentationInfo, ...]:
        """List currently running slideshows for an explicit user selection."""
        return self._running_slideshows()

    def connect(self, path: str | Path) -> PresentationInfo:
        """Attach only when exactly one active slideshow matches the selected full path."""
        identity = canonical_path(path)
        matches = [item for item in self._running_slideshows() if item.identity == identity]
        if not matches:
            raise PowerPointError(
                "That presentation is not running as a slideshow. "
                "Start it in PowerPoint and try again."
            )
        if len(matches) > 1:
            raise PowerPointError(
                "More than one slideshow is using that file. Close the duplicate and reconnect."
            )
        return matches[0]

    def status(self, path: str | Path) -> PresentationInfo | None:
        """Return current state only while the exact selected file remains in a slideshow."""
        identity = canonical_path(path)
        matches = [item for item in self._running_slideshows() if item.identity == identity]
        if len(matches) > 1:
            raise PowerPointError(
                "More than one slideshow is using that file. Disconnect and select one explicitly."
            )
        return matches[0] if matches else None
