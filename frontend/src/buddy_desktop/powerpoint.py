"""Windows desktop PowerPoint launch and slideshow discovery.

All COM work is performed inside the calling background task. COM interfaces are never
shared with Qt widgets or retained between tasks; a connected slideshow is identified by
its canonical source path and is revalidated before Buddy reports it as connected.
"""

from __future__ import annotations

import importlib
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import psutil  # type: ignore[import-untyped]

from buddy_desktop.powerpoint_contract import (
    PowerPointAction,
    PowerPointActionResult,
    PowerPointFrame,
)
from buddy_desktop.powerpoint_contract import PowerPointError as PowerPointError
from buddy_desktop.powerpoint_contract import PresentationInfo as PresentationInfo
from buddy_desktop.powerpoint_contract import canonical_path as canonical_path


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
        # One PowerPointAutomation instance serves a single TaskRunner, so operations are
        # serialized. The slide names themselves survive a Buddy restart while the
        # read-only PowerPoint session remains open; Reset scans by this private prefix.
        self._annotation_groups: dict[tuple[str, int, int, float, float], list[list[str]]] = {}

    _MANAGED_ANNOTATION_PREFIX = "BuddyAnnotation_"

    @classmethod
    def _name_shape(cls, shape: Any) -> str:
        name = cls._MANAGED_ANNOTATION_PREFIX + uuid.uuid4().hex
        shape.Name = name
        return name

    @classmethod
    def _delete_named_shapes(cls, slide: Any, names: list[str]) -> None:
        shapes = slide.Shapes
        for name in reversed(names):
            try:
                shapes.Item(name).Delete()
            except Exception:
                # A user may have deleted a Buddy annotation directly in PowerPoint.
                continue

    @classmethod
    def _delete_all_managed_shapes(cls, slide: Any) -> None:
        shapes = slide.Shapes
        names = []
        for index in range(shapes.Count, 0, -1):
            shape = shapes.Item(index)
            name = str(shape.Name)
            if name.startswith(cls._MANAGED_ANNOTATION_PREFIX):
                names.append(name)
        cls._delete_named_shapes(slide, names)

    def _delete_managed_shapes_in_region(
        self, slide: Any, key: tuple[str, int, int, float, float], region: Any
    ) -> None:
        shapes = slide.Shapes
        names = []
        left, top = region.x, region.y
        right, bottom = left + region.width, top + region.height
        for index in range(shapes.Count, 0, -1):
            shape = shapes.Item(index)
            name = str(shape.Name)
            if not name.startswith(self._MANAGED_ANNOTATION_PREFIX):
                continue
            shape_left, shape_top = float(shape.Left), float(shape.Top)
            shape_right = shape_left + float(shape.Width)
            shape_bottom = shape_top + float(shape.Height)
            if (
                shape_left <= right
                and shape_right >= left
                and shape_top <= bottom
                and shape_bottom >= top
            ):
                names.append(name)
        self._delete_named_shapes(slide, names)
        groups = self._annotation_groups.get(key)
        if groups is not None:
            removed = set(names)
            groups[:] = [[name for name in group if name not in removed] for group in groups]
            groups[:] = [group for group in groups if group]

    @classmethod
    def _configure_line(cls, shape: Any, color: int, *, arrow: bool = False) -> str:
        name = cls._name_shape(shape)
        shape.Line.ForeColor.RGB = color
        shape.Line.Weight = 2.25
        if arrow:
            shape.Line.EndArrowheadStyle = 2  # msoArrowheadTriangle
        return name

    @classmethod
    def _configure_box(
        cls,
        shape: Any,
        color: int,
        *,
        filled: bool,
        transparency: float,
    ) -> str:
        name = cls._name_shape(shape)
        shape.Fill.ForeColor.RGB = color
        shape.Fill.Transparency = transparency if filled else 1
        if filled:
            shape.Line.Visible = 0
        else:
            shape.Line.ForeColor.RGB = color
            shape.Line.Weight = 2.25
        return name

    @classmethod
    def _create_annotation_shapes(
        cls,
        slide: Any,
        action: PowerPointAction,
        validate_target: Callable[[], Any],
    ) -> list[str]:
        """Create editable slide objects so Buddy can remove each annotation precisely."""
        shapes = slide.Shapes
        r = action.region
        created: list[str] = []
        try:
            if action.kind in {"draw_line", "draw_arrow", "draw_freehand", "draw_underline"}:
                if action.kind == "draw_underline" and r is not None:
                    points: tuple[tuple[float, float], ...] = (
                        (r.x, r.y + r.height),
                        (r.x + r.width, r.y + r.height),
                    )
                else:
                    points = action.points
                for index, (start, end) in enumerate(zip(points, points[1:], strict=False)):
                    validate_target()
                    line = shapes.AddLine(*start, *end)
                    name = cls._configure_line(
                        line,
                        action.color,
                        arrow=action.kind == "draw_arrow" and index == len(points) - 2,
                    )
                    created.append(name)
            elif action.kind == "draw_rectangle" and r is not None:
                validate_target()
                shape = shapes.AddShape(1, r.x, r.y, r.width, r.height)  # msoShapeRectangle
                created.append(
                    cls._configure_box(shape, action.color, filled=False, transparency=1)
                )
            elif action.kind in {"draw_oval", "filled_oval"} and r is not None:
                validate_target()
                shape = shapes.AddShape(9, r.x, r.y, r.width, r.height)  # msoShapeOval
                created.append(
                    cls._configure_box(
                        shape,
                        action.color,
                        filled=action.kind == "filled_oval",
                        transparency=action.fill_transparency,
                    )
                )
            elif action.kind in {"highlight_region", "filled_rectangle"} and r is not None:
                validate_target()
                shape = shapes.AddShape(1, r.x, r.y, r.width, r.height)
                created.append(
                    cls._configure_box(
                        shape, action.color, filled=True, transparency=action.fill_transparency
                    )
                )
            elif action.kind == "text_box" and r is not None:
                validate_target()
                shape = shapes.AddTextbox(1, r.x, r.y, r.width, r.height)  # horizontal
                created.append(cls._name_shape(shape))
                shape.Fill.Transparency = 1
                shape.Line.Visible = 0
                shape.TextFrame.WordWrap = -1
                shape.TextFrame.TextRange.Text = action.text
                shape.TextFrame.TextRange.Font.Color.RGB = action.color
                shape.TextFrame.TextRange.Font.Size = 20
            elif action.kind == "callout" and r is not None:
                validate_target()
                shape = shapes.AddCallout(2, r.x, r.y, r.width, r.height)  # msoCalloutTwo
                created.append(cls._name_shape(shape))
                shape.Fill.Transparency = 1
                shape.Callout.Border = True
                shape.TextFrame.TextRange.Text = action.text
                shape.TextFrame.TextRange.Font.Color.RGB = action.color
                shape.TextFrame.TextRange.Font.Size = 18
                shape.Line.ForeColor.RGB = action.color
            else:
                raise PowerPointError("Unsupported PowerPoint annotation.")
            return created
        except Exception as exc:
            cls._delete_named_shapes(slide, created)
            if isinstance(exc, PowerPointError):
                raise
            raise PowerPointError(
                "PowerPoint could not create the annotation. Check the slide before retrying; "
                "the action was not retried."
            ) from None

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
                winreg = importlib.import_module("winreg")

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
    def _window_handle(slideshow: Any, name: str) -> int:
        try:
            handle = slideshow.HWND
            result = int(handle() if callable(handle) else handle)
            if result <= 0:
                raise ValueError("Invalid window handle")
            return result
        except Exception:
            # Some 64-bit Office builds advertise HWND in their type library but
            # reject it through IDispatch. Inspect native window metadata instead;
            # this never sends mouse/keyboard input or uses screen coordinates.
            gui = importlib.import_module("win32gui")
            processes = importlib.import_module("win32process")
            stem = re.escape(Path(name).stem)
            title_pattern = re.compile(
                r"(?:^|\s-\s)\s*" + stem + r"(?:\.pptx)?\s*(?:\s-\s|$)", re.I
            )
            matches: list[int] = []

            def inspect(hwnd: int, _context: object) -> None:
                title = gui.GetWindowText(hwnd)
                kind = gui.GetClassName(hwnd)
                if kind != "screenClass" and not (
                    kind == "PPTFrameClass" and title.startswith("PowerPoint Slide Show")
                ):
                    return
                if not title_pattern.search(title):
                    return
                try:
                    pid = processes.GetWindowThreadProcessId(hwnd)[1]
                    if psutil.Process(pid).name().casefold() == "powerpnt.exe":
                        matches.append(hwnd)
                except psutil.Error:
                    return

            gui.EnumWindows(inspect, None)
            if len(matches) != 1:
                raise PowerPointError(
                    "Cannot uniquely identify the PowerPoint slideshow window. "
                    "Use fullscreen mode or close shows with duplicate filenames, then reconnect."
                ) from None
            return matches[0]

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
        slide = presentation.Slides.Item(slide_index)
        window_id = PowerPointAutomation._window_handle(slideshow, name)
        return PresentationInfo(
            path,
            name,
            slide_index,
            slide_count,
            window_id,
            int(slide.SlideID),
            float(presentation.PageSetup.SlideWidth),
            float(presentation.PageSetup.SlideHeight),
        )

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

    def _target_window(self, application: Any, expected: PresentationInfo) -> Any:
        windows = application.SlideShowWindows
        matches = [
            windows.Item(i)
            for i in range(1, windows.Count + 1)
            if canonical_path(str(windows.Item(i).Presentation.FullName)) == expected.identity
        ]
        if len(matches) != 1:
            raise PowerPointError("The selected slideshow is unavailable or ambiguous. Reconnect.")
        window = matches[0]
        current = self._snapshot(window)
        if current.slide_key != expected.slide_key or current.slide_index != expected.slide_index:
            raise PowerPointError("The slideshow changed. Wait for Buddy to refresh and try again.")
        if int(window.View.State) not in {1, 2}:
            raise PowerPointError(
                "Return PowerPoint to a visible slide before using these controls."
            )
        return window

    def preview(self, expected: PresentationInfo) -> PowerPointFrame:
        """Export a bounded static reference image; never retain private preview files."""
        if not self._process_probe():
            raise PowerPointError("PowerPoint is no longer running. Reconnect to a slideshow.")
        pythoncom, client = self._start_com()
        try:
            application = client.Dispatch("PowerPoint.Application")
            window = self._target_window(application, expected)
            width = 1280
            height = max(1, round(width * expected.slide_height / expected.slide_width))
            if height > 2560:
                height = 2560
                width = max(1, round(height * expected.slide_width / expected.slide_height))
            with tempfile.TemporaryDirectory(prefix="buddy-powerpoint-") as folder:
                image_path = Path(folder) / "slide.png"
                window.Presentation.Slides.Item(expected.slide_index).Export(
                    str(image_path), "PNG", width, height
                )
                with image_path.open("rb") as stream:
                    png = stream.read(16 * 1024 * 1024 + 1)
                if len(png) > 16 * 1024 * 1024:
                    raise PowerPointError("The PowerPoint preview exceeds its size limit.")
            self._target_window(application, expected)
            return PowerPointFrame(expected, png)
        except PowerPointError:
            raise
        except Exception as exc:
            raise PowerPointError(
                "PowerPoint could not export the slide preview. Fully download the file and retry."
            ) from exc
        finally:
            self._end_com(pythoncom)

    def execute(self, action: PowerPointAction) -> PowerPointActionResult:
        """The only action entry point; caller strings never become COM method names."""
        if not isinstance(action, PowerPointAction):
            raise PowerPointError("Unsupported PowerPoint action.")
        action.validate()
        if not self._process_probe():
            raise PowerPointError("PowerPoint is no longer running. Reconnect to a slideshow.")
        pythoncom, client = self._start_com()
        try:
            application = client.Dispatch("PowerPoint.Application")
            window = self._target_window(application, action.target)
            info = self._snapshot(window)
            view = window.View
            if action.kind in {"next_slide", "previous_slide", "go_to_slide"}:
                target = (
                    info.slide_index + 1
                    if action.kind == "next_slide"
                    else info.slide_index - 1
                    if action.kind == "previous_slide"
                    else action.slide_number
                )
                if target is None or not 1 <= target <= info.slide_count:
                    raise PowerPointError("Already at the first or last slide.")
                view.GotoSlide(target, 0)  # Direct slide navigation; preserve animation state.
                deadline = time.monotonic() + 2
                while self._snapshot(window).slide_index != target:
                    if time.monotonic() >= deadline:
                        raise PowerPointError("PowerPoint has not confirmed the requested slide.")
                    time.sleep(0.03)
            elif action.kind == "set_pointer_tool":
                pointer_types = {"arrow": 1, "pen": 2}
                if action.pointer_tool == "pen":
                    view.PointerColor.RGB = action.color
                view.PointerType = pointer_types[action.pointer_tool or "arrow"]
            elif action.kind in {"erase_drawing", "reset_view"}:
                slide = window.Presentation.Slides.Item(info.slide_index)
                # Office 16 can leave native pen marks visible when EraseDrawing is
                # called while the pen pointer remains active. Return to the arrow
                # first so clearing removes the stroke layer before Buddy redraws.
                view.PointerType = 1
                view.EraseDrawing()
                self._delete_all_managed_shapes(slide)
                self._annotation_groups.pop(info.slide_key, None)
            elif action.kind == "undo_annotation":
                groups = self._annotation_groups.get(info.slide_key, [])
                if not groups:
                    raise PowerPointError("There is no Buddy annotation to undo on this slide.")
                self._delete_named_shapes(
                    window.Presentation.Slides.Item(info.slide_index), groups.pop()
                )
            elif action.kind == "erase_region" and action.region is not None:
                self._delete_managed_shapes_in_region(
                    window.Presentation.Slides.Item(info.slide_index), info.slide_key, action.region
                )
            else:
                # Each annotation is a named slide object. This avoids the slideshow
                # DrawLine layer's stale-ink behavior after EraseDrawing, and lets Buddy
                # remove only its own ink while preserving the underlying slide content.
                self._target_window(application, action.target)
                slide = window.Presentation.Slides.Item(info.slide_index)
                names = self._create_annotation_shapes(
                    slide, action, lambda: self._target_window(application, action.target)
                )
                if names:
                    self._annotation_groups.setdefault(info.slide_key, []).append(names)
            if action.kind not in {"next_slide", "previous_slide", "go_to_slide"}:
                self._target_window(application, action.target)
            return PowerPointActionResult(action, self._snapshot(window))
        except PowerPointError:
            raise
        except Exception as exc:
            raise PowerPointError(
                "PowerPoint could not finish the action. Check the slide; erase ink if needed. "
                "The action was not retried."
            ) from exc
        finally:
            self._end_com(pythoncom)
