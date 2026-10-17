"""Standalone catalog metadata and generator subset; no frontend or Qt imports."""

import json
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
from typing import Any, Literal

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

CATALOG: dict[str, Literal["slide_content", "general_control"]] = {
    "zoom_region": "slide_content",
    "highlight_region": "slide_content",
    "next_slide": "general_control",
    "previous_slide": "general_control",
    "go_to_slide": "general_control",
    "reset_view": "general_control",
}


@lru_cache(maxsize=2)
def schema(name: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(
        files("buddy_api.analysis").joinpath("schemas", name).read_text(encoding="utf-8")
    )
    return value


def generation_schema() -> dict[str, Any]:
    value = deepcopy(schema("action-v1.1.schema.json"))
    value["title"] = "Buddy generated slide-content action v1.1"
    value["oneOf"] = [
        item
        for item in value["oneOf"]
        if CATALOG[item["properties"]["type"]["const"]] == "slide_content"
    ]
    return value


def validate_catalog(value: object, *, manifest: bool = False) -> None:
    definition = schema("manifest-v1.schema.json") if manifest else generation_schema()
    if not Draft202012Validator(definition).is_valid(value):
        raise ValueError("Input does not match the catalog contract.")


def general_controls(slide_ids: tuple[str, ...], current_slide: str) -> dict[str, object]:
    if (
        not slide_ids
        or len(slide_ids) > 1000
        or any(not isinstance(s, str) or not s.strip() or len(s) > 128 for s in slide_ids)
        or len(set(slide_ids)) != len(slide_ids)
        or current_slide not in slide_ids
    ):
        raise ValueError("A unique ordered slide list and current slide are required.")
    current = slide_ids.index(current_slide)
    return {
        "next_slide": current < len(slide_ids) - 1,
        "previous_slide": current > 0,
        "go_to_slide": slide_ids,
        "reset_view": True,
    }
