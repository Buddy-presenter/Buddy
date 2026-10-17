"""Version 1 slide inputs. Packaged JSON schemas are also usable without Python/Qt."""

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]


class ContractError(ValueError):
    """Invalid, unsupported or stale slide input."""


def _finite(value: object) -> None:
    # Also bound already-decoded caller inputs, including cyclic/deep Python containers.
    stack = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if depth > 12 or visited > 50_000:
            raise ContractError("Input exceeds the structure limit.")
        if isinstance(item, float) and not math.isfinite(item):
            raise ContractError("Numbers must be finite.")
        if isinstance(item, dict):
            if len(item) > 16 or any(not isinstance(key, str) for key in item):
                raise ContractError("Invalid JSON object.")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > 1000:
                raise ContractError("Input exceeds the array limit.")
            stack.extend((child, depth + 1) for child in item)
        elif not isinstance(item, str | int | float | bool | type(None)):
            raise ContractError("Input contains a non-JSON value.")


@lru_cache(maxsize=3)
def _schema(schema: str) -> Any:
    definition = json.loads((Path(__file__).parent / "slide_contracts" / schema).read_text())
    return Draft202012Validator(definition)


def validate(value: object, schema: str) -> dict[str, Any]:
    _finite(value)
    if not _schema(schema).is_valid(value):
        raise ContractError("Input does not match the supported slide contract.")
    assert isinstance(value, dict)
    return value


def read_json(path: Path, limit: int = 2 * 1024 * 1024) -> object:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ContractError("Duplicate JSON field.")
            result[key] = value
        return result

    try:
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ContractError("JSON input exceeds its size limit.")
        value = json.loads(content, object_pairs_hook=pairs)
        _finite(value)
        return value
    except (OSError, ValueError, RecursionError) as exc:
        raise ContractError("Cannot read valid bounded JSON input.") from exc


@dataclass(frozen=True)
class Region:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Slide:
    slide_id: str
    width: int
    height: int
    image: str
    raster_width: int
    raster_height: int
    sha256: str

    @property
    def full(self) -> Region:
        return Region(0, 0, self.width, self.height)


@dataclass(frozen=True)
class Manifest:
    deck_id: str
    render_id: str
    slides: tuple[Slide, ...]
    root: Path
    pdf_content: bytes | None = None
    account_bound: bool = False

    def index(self, slide_id: str) -> int:
        for index, slide in enumerate(self.slides):
            if slide.slide_id == slide_id:
                return index
        raise ContractError("Unknown slide ID.")


def parse_manifest(value: object, root: Path) -> Manifest:
    data = validate(value, "manifest-v1.schema.json")
    slides = []
    for item in data["slides"]:
        source, raster = item["source"], item["raster"]
        if source["width"] * raster["height"] != source["height"] * raster["width"]:
            raise ContractError("Raster and source aspect ratios must match.")
        if raster["width"] * raster["height"] > 16_777_216:
            raise ContractError("Raster exceeds the decoded pixel limit.")
        slides.append(
            Slide(
                item["slide_id"],
                int(source["width"]),
                int(source["height"]),
                raster["path"],
                int(raster["width"]),
                int(raster["height"]),
                raster["sha256"],
            )
        )
    if len({slide.slide_id for slide in slides}) != len(slides):
        raise ContractError("Slide IDs must be unique.")
    return Manifest(data["deck_id"], data["render_id"], tuple(slides), root.resolve())


@dataclass(frozen=True)
class Action:
    instance_id: str
    purpose: str
    type: str
    slide_id: str
    target_index: int
    region: Region


def parse_action(value: object, manifest: Manifest, current: int) -> Action:
    if type(current) is not int or not 0 <= current < len(manifest.slides):
        raise ContractError("Invalid current slide index.")
    schema = (
        "action-v1.1.schema.json"
        if isinstance(value, dict) and value.get("version") == "1.1"
        else "action-v1.schema.json"
    )
    data = validate(value, schema)
    if (data["deck_id"], data["render_id"]) != (manifest.deck_id, manifest.render_id):
        raise ContractError("Stale deck or render reference.")
    slide = manifest.slides[current]
    if data["slide_id"] != slide.slide_id:
        raise ContractError("Action does not reference the current slide.")
    kind, params = data["type"], data["params"]
    target = current
    if kind == "next_slide":
        target = min(current + 1, len(manifest.slides) - 1)
    elif kind == "previous_slide":
        target = max(0, current - 1)
    elif kind == "go_to_slide":
        target = manifest.index(params["target_slide_id"])
    region = manifest.slides[target].full
    if kind in {"zoom_region", "highlight_region"}:
        region = Region(**params["region"])
        if region.x + region.width > slide.width or region.y + region.height > slide.height:
            raise ContractError("Region exceeds source slide bounds.")
        if kind == "zoom_region" and (
            region.width < slide.width / 32 or region.height < slide.height / 32
        ):
            raise ContractError("Region exceeds the maximum 32x magnification.")
    return Action(data["instance_id"], data["purpose"], kind, slide.slide_id, target, region)
