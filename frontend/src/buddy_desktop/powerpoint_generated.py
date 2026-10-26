"""Versioned generated PowerPoint catalog; never accepts PDF action instances."""

import json
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from buddy_desktop.powerpoint_contract import PowerPointAction, PowerPointError, PresentationInfo
from buddy_desktop.slide_contract import Region


def generated_action(
    raw: dict[str, Any],
    target: PresentationInfo,
    *,
    deck_id: str,
    render_id: str,
    slide_id: str,
    native_slide_id: int,
    width: float,
    height: float,
) -> PowerPointAction:
    definition = json.loads(
        files("buddy_desktop").joinpath("powerpoint_contracts", "action-v1.schema.json").read_text()
    )
    try:
        Draft202012Validator(definition).validate(raw)
    except Exception:
        raise PowerPointError("The saved action does not match the PowerPoint catalog.") from None
    if (raw["deck_id"], raw["render_id"], raw["slide_id"]) != (deck_id, render_id, slide_id):
        raise PowerPointError("The saved PowerPoint action belongs to another revision.")
    if (
        target.slide_id != native_slide_id
        or abs(target.slide_width - width) > 0.01
        or abs(target.slide_height - height) > 0.01
    ):
        raise PowerPointError("The connected PowerPoint slide does not match the saved analysis.")
    params = raw["params"]
    r = Region(**params["region"])
    action = PowerPointAction(
        raw["type"],
        target,
        points=((r.x, r.y), (r.x + r.width, r.y + r.height)) if raw["type"] == "draw_arrow" else (),
        region=None if raw["type"] == "draw_arrow" else r,
        text=params.get("text", ""),
        color=0x3D84F5,
    )
    action.validate()
    return action
