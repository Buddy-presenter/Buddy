"""Plain-data, allowlisted PowerPoint actions shared by manual and future agent input."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from buddy_desktop.services import ServiceError
from buddy_desktop.slide_contract import Region


class PowerPointError(ServiceError):
    """A recoverable automation failure without raw provider details."""


def canonical_path(value: str | Path) -> str:
    path = os.fspath(value)
    if path.lower().startswith("https://"):
        url = urlsplit(path)
        return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path, url.query, ""))
    return os.path.normcase(os.path.realpath(path))


@dataclass(frozen=True)
class PresentationInfo:
    path: str
    name: str
    slide_index: int
    slide_count: int
    window_id: int = 0
    slide_id: int = 0
    slide_width: float = 0
    slide_height: float = 0

    @property
    def identity(self) -> str:
        return canonical_path(self.path)

    @property
    def slide_key(self) -> tuple[str, int, int, float, float]:
        return (self.identity, self.window_id, self.slide_id, self.slide_width, self.slide_height)

    @property
    def choice_label(self) -> str:
        return f"{self.name} — slide {self.slide_index} of {self.slide_count} ({self.path})"


@dataclass(frozen=True)
class PowerPointFrame:
    info: PresentationInfo
    png: bytes


@dataclass(frozen=True)
class PowerPointAction:
    kind: str
    target: PresentationInfo
    slide_number: int | None = None
    points: tuple[tuple[float, float], ...] = ()
    region: Region | None = None
    color: int = 0x202020
    fill_transparency: float = 0.62
    text: str = ""
    pointer_tool: str | None = None
    auto_place: bool = False

    def validate(self) -> None:
        allowed = {
            "next_slide",
            "previous_slide",
            "go_to_slide",
            "draw_line",
            "draw_arrow",
            "draw_rectangle",
            "draw_rounded_rectangle",
            "draw_diamond",
            "draw_triangle",
            "draw_hexagon",
            "draw_star",
            "draw_oval",
            "draw_underline",
            "draw_freehand",
            "highlight_region",
            "filled_rectangle",
            "filled_oval",
            "text_box",
            "callout",
            "set_pointer_tool",
            "erase_region",
            "undo_annotation",
            "erase_drawing",
            "reset_view",
        }
        if (
            not isinstance(self.kind, str)
            or self.kind not in allowed
            or not isinstance(self.target, PresentationInfo)
        ):
            raise PowerPointError("Unsupported PowerPoint action.")
        target = self.target
        if (
            not isinstance(target.path, str)
            or not target.path
            or any(
                type(v) is not int or v <= 0
                for v in (
                    target.window_id,
                    target.slide_id,
                    target.slide_index,
                    target.slide_count,
                )
            )
            or not 1 <= target.slide_index <= target.slide_count
            or not self._number(target.slide_width)
            or not self._number(target.slide_height)
            or min(target.slide_width, target.slide_height) <= 0
        ):
            raise PowerPointError("A current PowerPoint slideshow connection is required.")
        if not isinstance(self.points, tuple):
            raise PowerPointError("Drawing points must be an immutable tuple.")
        if self.kind == "go_to_slide":
            if (
                type(self.slide_number) is not int
                or not 1 <= self.slide_number <= target.slide_count
            ):
                raise PowerPointError("Choose a slide number within this presentation.")
        elif self.slide_number is not None:
            raise PowerPointError("This action does not accept a slide number.")

        point_kinds = {"draw_line", "draw_arrow", "draw_freehand"}
        if self.kind in point_kinds:
            minimum = 2
            maximum = 256 if self.kind == "draw_freehand" else 2
            if not minimum <= len(self.points) <= maximum:
                raise PowerPointError("Choose two or more points inside the slide.")
            for point in self.points:
                if not isinstance(point, tuple) or len(point) != 2:
                    raise PowerPointError("Every drawing point must have an x and y value.")
                if not all(self._number(v) for v in point):
                    raise PowerPointError("Drawing coordinates must be finite numbers.")
                if (
                    not 0 <= point[0] <= target.slide_width
                    or not 0 <= point[1] <= target.slide_height
                ):
                    raise PowerPointError("Drawing coordinates must stay inside the slide.")
            if any(a == b for a, b in zip(self.points, self.points[1:], strict=False)):
                raise PowerPointError("Choose distinct drawing points.")
        elif self.points:
            raise PowerPointError("This action does not accept drawing points.")

        region_kinds = {
            "draw_rounded_rectangle",
            "draw_diamond",
            "draw_triangle",
            "draw_hexagon",
            "draw_star",
            "draw_rectangle",
            "draw_oval",
            "draw_underline",
            "erase_region",
            "highlight_region",
            "filled_rectangle",
            "filled_oval",
            "text_box",
            "callout",
        }
        if self.kind in region_kinds:
            r = self.region
            if not isinstance(r, Region) or not all(
                self._number(v) for v in (r.x, r.y, r.width, r.height)
            ):
                raise PowerPointError("Choose a valid annotation area.")
            if (
                min(r.x, r.y) < 0
                or min(r.width, r.height) < 12
                or r.x + r.width > target.slide_width
                or r.y + r.height > target.slide_height
            ):
                raise PowerPointError(
                    "The annotation area must be at least 12 points and stay inside the slide."
                )
        elif self.region is not None:
            raise PowerPointError("This action does not accept an annotation area.")

        if type(self.color) is not int or not 0 <= self.color <= 0xFFFFFF:
            raise PowerPointError("Choose a valid annotation color.")
        if type(self.auto_place) is not bool or (
            self.auto_place and self.kind not in {"callout", "text_box"}
        ):
            raise PowerPointError("Automatic placement is only available for text annotations.")
        if not self._number(self.fill_transparency) or not 0 <= self.fill_transparency <= 1:
            raise PowerPointError("Fill transparency must be between 0 and 1.")
        if self.kind in {"text_box", "callout"}:
            if (
                not isinstance(self.text, str)
                or not self.text.strip()
                or len(self.text) > 300
                or any(ord(ch) < 32 and ch not in "\t\n" for ch in self.text)
            ):
                raise PowerPointError("Enter annotation text (up to 300 characters).")
        elif self.text:
            raise PowerPointError("This action does not accept text.")

        if self.kind == "set_pointer_tool":
            if self.pointer_tool not in {"arrow", "pen"}:
                raise PowerPointError("Choose the arrow or pen pointer.")
        elif self.pointer_tool is not None:
            raise PowerPointError("This action does not accept a pointer setting.")

    @staticmethod
    def _number(value: object) -> bool:
        return (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and abs(value) <= 1e9
            and math.isfinite(value)
        )

    def segments(self) -> tuple[tuple[float, float, float, float], ...]:
        self.validate()
        if self.kind in {"draw_line", "draw_arrow"}:
            (x1, y1), (x2, y2) = self.points
            return ((x1, y1, x2, y2),)
        if self.kind == "draw_freehand":
            return tuple((*a, *b) for a, b in zip(self.points, self.points[1:], strict=False))
        if self.kind == "draw_underline" and self.region is not None:
            r = self.region
            return ((r.x, r.y + r.height, r.x + r.width, r.y + r.height),)
        if self.kind in {"draw_rectangle", "highlight_region", "filled_rectangle"} and self.region:
            r = self.region
            x, y, right, bottom = r.x, r.y, r.x + r.width, r.y + r.height
            return (
                (x, y, right, y),
                (right, y, right, bottom),
                (right, bottom, x, bottom),
                (x, bottom, x, y),
            )
        if self.kind in {"draw_oval", "filled_oval"} and self.region:
            r = self.region
            return tuple(
                (
                    r.x + r.width / 2 + r.width / 2 * math.cos(a),
                    r.y + r.height / 2 + r.height / 2 * math.sin(a),
                    r.x + r.width / 2 + r.width / 2 * math.cos(b),
                    r.y + r.height / 2 + r.height / 2 * math.sin(b),
                )
                for a, b in zip(
                    (2 * math.pi * i / 32 for i in range(32)),
                    (2 * math.pi * (i + 1) / 32 for i in range(32)),
                    strict=True,
                )
            )
        return ()


@dataclass(frozen=True)
class PowerPointActionResult:
    action: PowerPointAction
    info: PresentationInfo
