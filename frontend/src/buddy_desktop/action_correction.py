"""Bounded corrections retain the saved action's source identity and target."""

import io
import math
import zipfile
from copy import deepcopy
from typing import Any
from xml.etree import ElementTree as ET

import pypdfium2 as pdfium  # type: ignore[import-untyped]

from buddy_desktop.preflight_powerpoint import safe_source
from buddy_desktop.slide_contract import ContractError, Manifest, parse_action

Rect = dict[str, float]


def rect(value: object) -> Rect:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise ContractError("Correction requires one bounded region.")
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in value.values()):
        raise ContractError("Correction coordinates must be finite numbers.")
    result = {key: float(number) for key, number in value.items()}
    if min(result["width"], result["height"]) <= 0:
        raise ContractError("Correction dimensions must be positive.")
    return result


def overlap(first: Rect, second: Rect) -> float:
    width = max(
        0.0,
        min(first["x"] + first["width"], second["x"] + second["width"])
        - max(first["x"], second["x"]),
    )
    height = max(
        0.0,
        min(first["y"] + first["height"], second["y"] + second["height"])
        - max(first["y"], second["y"]),
    )
    return width * height


def source_target(
    source: bytes,
    action: dict[str, Any],
    manifest: Manifest | None,
    slides: dict[str, dict[str, Any]],
) -> tuple[Rect, tuple[float, float]]:
    """Resolve the original target from source geometry, never model coordinates."""
    original = rect(action["params"]["region"])
    if manifest is not None:
        index = manifest.index(action["slide_id"])
        slide = manifest.slides[index]
        width, height = float(slide.width), float(slide.height)
        with pdfium.PdfDocument(source) as document:
            page = document[index]
            try:
                if page.get_rotation():
                    raise ContractError("Rotated PDF correction requires a target mapping.")
                scale_x, scale_y = width / page.get_width(), height / page.get_height()
                crop_left, _, _, crop_top = page.get_bbox()
                candidates = []
                for obj in page.get_objects(max_depth=1):
                    left, bottom, right, top = obj.get_bounds()
                    candidate = dict(
                        x=(left - crop_left) * scale_x,
                        y=(crop_top - top) * scale_y,
                        width=(right - left) * scale_x,
                        height=(top - bottom) * scale_y,
                    )
                    area = candidate["width"] * candidate["height"]
                    if area > 0 and area < width * height * 0.9:
                        score = overlap(original, candidate) / (
                            original["width"] * original["height"]
                            + area
                            - overlap(original, candidate)
                        )
                        if score > 0.1:
                            candidates.append((score, candidate))
                candidates.sort(key=lambda item: item[0], reverse=True)
                if not candidates or (
                    len(candidates) > 1 and candidates[1][0] >= candidates[0][0] * 0.95
                ):
                    raise ContractError("The saved source target is missing or ambiguous.")
                return candidates[0][1], (width, height)
            finally:
                page.close()
    safe_source(source)
    native_slide = slides[action["slide_id"]]
    width, height = float(native_slide["width"]), float(native_slide["height"])
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        # Native slide IDs establish the source mapping, independent of filename numbering.
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        ns = {
            "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        records = presentation.findall("p:sldIdLst/p:sldId", ns)
        record = records[native_slide["number"] - 1]
        if int(record.attrib["id"]) != native_slide["native_id"]:
            raise ContractError("The native slide identity changed.")
        relations = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
        relation = next(
            item for item in relations if item.attrib["Id"] == record.attrib[f"{{{ns['r']}}}id"]
        )
        target = relation.attrib["Target"].lstrip("/")
        if target.startswith("ppt/"):
            name = target
        elif target.startswith("slides/"):
            name = "ppt/" + target
        else:
            raise ContractError("Unsupported native slide source.")
        tree = ET.fromstring(archive.read(name))
        shape_id = action["params"].get("source_shape_id")
        candidates = []
        for shape in tree.findall("p:cSld/p:spTree/*", ns):
            identifier = shape.find(".//p:cNvPr", ns)
            transform = shape.find("p:spPr/a:xfrm", ns)
            if identifier is None or transform is None:
                continue
            if shape_id is not None and int(identifier.attrib["id"]) != shape_id:
                continue
            rotation = int(transform.attrib.get("rot", "0"))
            if (
                rotation
                or transform.attrib.get("flipH") == "1"
                or transform.attrib.get("flipV") == "1"
            ):
                continue
            offset, extent = transform.find("a:off", ns), transform.find("a:ext", ns)
            if offset is None or extent is None:
                continue
            candidate = dict(
                x=int(offset.attrib["x"]) / 12700,
                y=int(offset.attrib["y"]) / 12700,
                width=int(extent.attrib["cx"]) / 12700,
                height=int(extent.attrib["cy"]) / 12700,
            )
            area = candidate["width"] * candidate["height"]
            if area <= 0:
                continue
            score = overlap(original, candidate) / (
                original["width"] * original["height"] + area - overlap(original, candidate)
            )
            if score > 0.1:
                candidates.append((score, candidate))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates or (len(candidates) > 1 and candidates[1][0] >= candidates[0][0] * 0.95):
            raise ContractError("The saved native target is missing or ambiguous.")
        return candidates[0][1], (width, height)


def corrected_action(
    original: dict[str, Any],
    proposal: object,
    target: Rect,
    dimensions: tuple[float, float],
    manifest: Manifest | None,
) -> dict[str, Any]:
    """Clamp only region coordinates; all action and semantic identity stays unchanged."""
    region = rect(proposal)
    width, height = dimensions
    # Padding is permitted around the source target while limiting cross-target movement.
    px, py = max(2.0, target["width"] * 0.15), max(2.0, target["height"] * 0.15)
    left, top = max(0.0, target["x"] - px), max(0.0, target["y"] - py)
    right = min(width, target["x"] + target["width"] + px)
    bottom = min(height, target["y"] + target["height"] + py)
    x = max(left, min(region["x"], right - 1))
    y = max(top, min(region["y"], bottom - 1))
    w = min(region["width"], right - x)
    h = min(region["height"], bottom - y)
    if w <= 0 or h <= 0:
        raise ContractError("Correction is outside the target bounds.")
    # Moving away from most of the source content cannot preserve the intended target.
    bounded = dict(x=x, y=y, width=w, height=h)
    if overlap(bounded, target) < target["width"] * target["height"] * 0.8:
        raise ContractError("Correction would exclude the saved target.")
    candidate = deepcopy(original)
    candidate["params"]["region"] = bounded
    if candidate == original:
        raise ContractError("Correction did not change the action.")
    if manifest is not None:
        parse_action(candidate, manifest, manifest.index(original["slide_id"]))
    else:
        # Native executor and shape/text bindings are rechecked inside each isolated run.
        if original["type"] == "zoom_region":
            raise ContractError("Native zoom is pinned to the complete source shape.")
        if original["params"].get("source_shape_id") is not None:
            raise ContractError("Native text geometry is measured by PowerPoint.")
    return candidate
