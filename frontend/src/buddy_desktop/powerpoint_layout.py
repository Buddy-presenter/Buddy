"""Bounded annotation placement in slide points, independent of Office/Qt."""

import math

from buddy_desktop.powerpoint_contract import PowerPointError
from buddy_desktop.slide_contract import Region


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
