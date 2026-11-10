"""Bounded annotation placement in slide points, independent of Office/Qt."""

import math

from buddy_desktop.powerpoint_contract import PowerPointError
from buddy_desktop.slide_contract import Region


def pointer_points(
    target: Region, width: float, height: float, occupied: tuple[Region, ...]
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Point to the target edge from clear space, never diagonally through its text."""
    x, y, w, h = target.x, target.y, target.width, target.height
    for length in (42.0, 28.0, 18.0):
        for start, end in (
            ((x - length - 3, y + h / 2), (x - 3, y + h / 2)),
            ((x + w + length + 3, y + h / 2), (x + w + 3, y + h / 2)),
            ((x + w / 2, y - length - 3), (x + w / 2, y - 3)),
            ((x + w / 2, y + h + length + 3), (x + w / 2, y + h + 3)),
        ):
            left, right = sorted((start[0], end[0]))
            top, bottom = sorted((start[1], end[1]))
            if left < 6 or top < 6 or right > width - 6 or bottom > height - 6:
                continue
            if any(
                left < r.x + r.width + 3
                and right + 3 > r.x
                and top < r.y + r.height + 3
                and bottom + 3 > r.y
                for r in occupied
            ):
                continue
            return start, end
    raise PowerPointError("No clear space is available for an arrow pointing to this target.")


def annotation_box(
    target: Region, width: float, height: float, text: str, occupied: tuple[Region, ...]
) -> Region:
    """Find clear space near the target; refuse to cover source content."""
    candidates: list[Region] = []
    for box_width in (min(280.0, width * 0.36), min(210.0, width * 0.3), 150.0):
        if box_width > width - 24:
            continue
        lines = max(1, math.ceil(len(text) / max(10, int((box_width - 20) / 8))))
        box_height = max(52.0, lines * 20.0 + 20)
        if box_height > height - 24:
            continue
        positions = [
            (target.x + target.width + 14, target.y),
            (target.x - box_width - 14, target.y),
            (target.x, target.y + target.height + 14),
            (target.x, target.y - box_height - 14),
        ]
        positions.extend(
            (12 + (width - box_width - 24) * x / 16, 12 + (height - box_height - 24) * y / 12)
            for x in range(17)
            for y in range(13)
        )
        for x, y in positions:
            if x < 12 or y < 12 or x + box_width > width - 12 or y + box_height > height - 12:
                continue
            if any(
                x < r.x + r.width + 8
                and x + box_width + 8 > r.x
                and y < r.y + r.height + 8
                and y + box_height + 8 > r.y
                for r in occupied
            ):
                continue
            candidates.append(Region(x, y, box_width, box_height))
        if candidates:
            return min(candidates, key=lambda r: (r.x - target.x) ** 2 + (r.y - target.y) ** 2)
    raise PowerPointError(
        "No clear space is available for this text annotation without covering slide content."
    )
