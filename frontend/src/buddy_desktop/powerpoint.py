"""Windows desktop PowerPoint launch and slideshow discovery.

All COM work is performed inside the calling background task. COM interfaces are never
shared with Qt widgets or retained between tasks; a connected slideshow is identified by
its canonical source path and is revalidated before Buddy reports it as connected.
"""

from __future__ import annotations

import importlib
import math
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
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
from buddy_desktop.slide_contract import Region


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
    def _delete_named_shapes(cls, slide: Any, names: list[str]) -> list[str]:
        """Delete named shapes and return names still present after attempted deletion."""
        shapes = slide.Shapes
        requested = set(names)
        found: dict[str, Any] = {}
        for index in range(shapes.Count, 0, -1):
            shape = shapes.Item(index)
            name = str(shape.Name)
            if name in requested:
                found[name] = shape

        for name in reversed(names):
            shape = found.get(name)
            if shape is None:
                # A presenter may have removed this annotation directly in PowerPoint.
                continue
            with suppress(Exception):
                shape.Delete()
        remaining = {str(shapes.Item(index).Name) for index in range(1, shapes.Count + 1)}
        return [name for name in names if name in remaining]

    @classmethod
    def _delete_all_managed_shapes(cls, slide: Any) -> list[str]:
        shapes = slide.Shapes
        names = []
        for index in range(shapes.Count, 0, -1):
            shape = shapes.Item(index)
            name = str(shape.Name)
            if name.startswith(cls._MANAGED_ANNOTATION_PREFIX):
                names.append(name)
        return cls._delete_named_shapes(slide, names)

    def _retain_annotation_groups(
        self, key: tuple[str, int, int, float, float], names: list[str]
    ) -> None:
        groups = self._annotation_groups.get(key)
        if groups is None:
            return
        remaining = set(names)
        groups[:] = [[name for name in group if name in remaining] for group in groups]
        groups[:] = [group for group in groups if group]

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
        remaining = self._delete_named_shapes(slide, names)
        attempted = set(names)
        still_present = set(remaining) | {
            name
            for group in self._annotation_groups.get(key, [])
            for name in group
            if name not in attempted
        }
        self._retain_annotation_groups(key, list(still_present))
        if remaining:
            raise PowerPointError(
                "PowerPoint could not erase every Buddy annotation in the selected area. "
                "Remaining annotations are still tracked; retry the erase."
            )
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
        shape.Fill.Solid()
        shape.Fill.ForeColor.RGB = color
        shape.Shadow.Visible = 0
        shape.Fill.Transparency = transparency if filled else 1
        if filled:
            shape.Line.Visible = 0
        else:
            shape.Line.ForeColor.RGB = color
            shape.Line.Weight = 2.25
        return name

    @classmethod
    def _source_shape(cls, shapes: Any, identifier: int) -> Any:
        pending = [shapes]
        visited = 0
        while pending:
            collection = pending.pop()
            for index in range(1, collection.Count + 1):
                visited += 1
                if visited > 25000:
                    raise PowerPointError("The slide exceeds the source object limit.")
                item = collection.Item(index)
                if int(getattr(item, "Id", -1)) == identifier:
                    return item
                if getattr(item, "Type", None) == 6:  # msoGroup
                    pending.append(item.GroupItems)
        raise PowerPointError("The source object is no longer available on this slide.")

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
        points = action.points
        if action.source_shape_id is not None:
            source = cls._source_shape(shapes, action.source_shape_id)
            if source.HasTextFrame and source.TextFrame.HasText:
                if action.kind in {"draw_diamond", "draw_triangle", "draw_hexagon", "draw_star"}:
                    raise PowerPointError("This geometric outline cannot safely frame text.")
                tr = source.TextFrame.TextRange
                if action.target_text:
                    text = str(tr.Text).replace("\r", "\n").replace("\v", "\n")
                    if text.count(action.target_text) != 1:
                        raise PowerPointError("The selected source phrase is missing or ambiguous.")
                    text_start = text.index(action.target_text)
                    offset = len(text[:text_start].encode("utf-16-le")) // 2
                    length = len(action.target_text.encode("utf-16-le")) // 2
                    tr = tr.Characters(offset + 1, length)
                x, y, width, height = map(
                    float, (tr.BoundLeft, tr.BoundTop, tr.BoundWidth, tr.BoundHeight)
                )
                if not all(math.isfinite(v) for v in (x, y, width, height)):
                    raise PowerPointError("PowerPoint could not measure the selected text.")
                right = min(action.target.slide_width, x + width + 3)
                bottom = min(action.target.slide_height, y + height + 2)
                x, y = max(0.0, x - 3), max(0.0, y - 2)
                if right <= x or bottom <= y:
                    raise PowerPointError("The selected text is outside the visible slide.")
                r = Region(x, y, right - x, bottom - y)
                if action.kind in {"draw_arrow", "draw_line"}:
                    points = ((r.x, r.y), (r.x + r.width, r.y + r.height))
            elif action.target_text:
                raise PowerPointError("The source object no longer contains the selected text.")
        created: list[Any] = []
        names: list[str] = []
        try:
            if action.kind in {"draw_line", "draw_arrow", "draw_freehand", "draw_underline"}:
                if action.kind == "draw_underline" and r is not None:
                    points = (
                        (r.x, r.y + r.height),
                        (r.x + r.width, r.y + r.height),
                    )
                for index, (start, end) in enumerate(zip(points, points[1:], strict=False)):
                    validate_target()
                    line = shapes.AddLine(*start, *end)
                    created.append(line)
                    name = cls._configure_line(
                        line,
                        action.color,
                        arrow=action.kind == "draw_arrow" and index == len(points) - 2,
                    )
                    names.append(name)
            elif (
                action.kind
                in {
                    "draw_rectangle",
                    "draw_rounded_rectangle",
                    "draw_diamond",
                    "draw_triangle",
                    "draw_hexagon",
                    "draw_star",
                }
                and r is not None
            ):
                validate_target()
                shape_type = {
                    "draw_rectangle": 1,
                    "draw_rounded_rectangle": 5,
                    "draw_diamond": 4,
                    "draw_triangle": 7,
                    "draw_hexagon": 10,
                    "draw_star": 92,
                }[action.kind]
                shape = shapes.AddShape(shape_type, r.x, r.y, r.width, r.height)
                created.append(shape)
                names.append(cls._configure_box(shape, action.color, filled=False, transparency=1))
            elif action.kind in {"draw_oval", "filled_oval"} and r is not None:
                validate_target()
                shape = shapes.AddShape(9, r.x, r.y, r.width, r.height)  # msoShapeOval
                created.append(shape)
                names.append(
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
                created.append(shape)
                names.append(
                    cls._configure_box(
                        shape, action.color, filled=True, transparency=action.fill_transparency
                    )
                )
            elif action.kind in {"text_box", "callout"} and r is not None:
                target_region = r
                if action.auto_place:
                    from buddy_desktop.powerpoint_layout import annotation_box

                    occupied = []
                    for index in range(1, shapes.Count + 1):
                        item = shapes.Item(index)
                        left, top, width, height = map(
                            float, (item.Left, item.Top, item.Width, item.Height)
                        )
                        is_target = (
                            abs(left - target_region.x) < 0.1
                            and abs(top - target_region.y) < 0.1
                            and abs(width - target_region.width) < 0.1
                            and abs(height - target_region.height) < 0.1
                        )
                        if (
                            width * height
                            >= action.target.slide_width * action.target.slide_height * 0.85
                            and not (item.HasTextFrame and item.TextFrame.HasText)
                        ):
                            continue  # Full-slide background artwork is not foreground text.
                        try:
                            if item.HasTextFrame and item.TextFrame.HasText:
                                tr = item.TextFrame.TextRange
                                left, top, width, height = map(
                                    float,
                                    (tr.BoundLeft, tr.BoundTop, tr.BoundWidth, tr.BoundHeight),
                                )
                                if is_target and width > 0 and height > 0:
                                    target_region = Region(left, top, width, height)
                        except (AttributeError, TypeError):
                            pass
                        if width > 0 and height > 0:
                            occupied.append(Region(left, top, width, height))
                    r = annotation_box(
                        target_region,
                        action.target.slide_width,
                        action.target.slide_height,
                        action.text,
                        tuple(occupied),
                    )
                validate_target()
                shape = (
                    shapes.AddShape(5, r.x, r.y, r.width, r.height)
                    if action.kind == "callout"
                    else shapes.AddTextbox(1, r.x, r.y, r.width, r.height)
                )
                created.append(shape)
                names.append(cls._name_shape(shape))
                shape.Fill.Solid()
                shape.Fill.ForeColor.RGB = 0x000000
                shape.Shadow.Visible = 0
                shape.Fill.Transparency = 0
                shape.Line.Visible = -1 if action.kind == "callout" else 0
                shape.Line.ForeColor.RGB = action.color
                shape.Line.Weight = 1.5
                shape.TextFrame.AutoSize = 0
                shape.TextFrame.WordWrap = -1
                shape.TextFrame.MarginLeft = shape.TextFrame.MarginRight = 8
                shape.TextFrame.MarginTop = shape.TextFrame.MarginBottom = 6
                shape.TextFrame.TextRange.Text = action.text
                shape.TextFrame.TextRange.Font.Color.RGB = action.color
                shape.TextFrame.TextRange.Font.Size = 16
                shape.TextFrame2.WordWrap = -1
                shape.TextFrame2.VerticalAnchor = 3  # msoAnchorMiddle
                shape.TextFrame2.AutoSize = 2  # msoAutoSizeTextToFitShape
                if float(shape.TextFrame2.TextRange.BoundHeight) > r.height - 10:
                    raise PowerPointError("The annotation text does not fit safely inside its box.")
                if action.auto_place and action.kind == "callout":
                    end_x = min(
                        max(r.x + r.width / 2, target_region.x),
                        target_region.x + target_region.width,
                    )
                    end_y = min(
                        max(r.y + r.height / 2, target_region.y),
                        target_region.y + target_region.height,
                    )
                    start_x = min(max(end_x, r.x), r.x + r.width)
                    start_y = min(max(end_y, r.y), r.y + r.height)
                    validate_target()
                    leader = shapes.AddLine(start_x, start_y, end_x, end_y)
                    created.append(leader)
                    names.append(cls._configure_line(leader, action.color, arrow=True))
            else:
                raise PowerPointError("Unsupported PowerPoint annotation.")
            return names
        except Exception as exc:
            cleanup_failed = False
            for shape in reversed(created):
                try:
                    shape.Delete()
                except Exception:
                    cleanup_failed = True
            if isinstance(exc, PowerPointError):
                if cleanup_failed:
                    raise PowerPointError(
                        f"{exc} A partially configured shape may remain on the slide; "
                        "inspect it before retrying."
                    ) from None
                raise
            raise PowerPointError(
                "PowerPoint could not create the annotation. "
                + (
                    "A partially configured shape may remain on the slide; inspect it before "
                    "retrying. "
                    if cleanup_failed
                    else "Any partially created shapes were removed. "
                )
                + "The action was not retried."
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
                remaining = self._delete_all_managed_shapes(slide)
                self._retain_annotation_groups(info.slide_key, remaining)
                if remaining:
                    raise PowerPointError(
                        "PowerPoint could not erase every Buddy annotation. Remaining "
                        "annotations are still tracked; retry the erase."
                    )
                self._annotation_groups.pop(info.slide_key, None)
            elif action.kind == "undo_annotation":
                groups = self._annotation_groups.get(info.slide_key, [])
                if not groups:
                    raise PowerPointError("There is no Buddy annotation to undo on this slide.")
                remaining = self._delete_named_shapes(
                    window.Presentation.Slides.Item(info.slide_index), groups[-1]
                )
                if remaining:
                    groups[-1] = remaining
                    raise PowerPointError(
                        "PowerPoint could not undo this annotation. It is still tracked; "
                        "retry the undo."
                    )
                groups.pop()
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
