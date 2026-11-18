"""Versioned bounded correction attempts through the real isolated QA player."""

import json
from copy import deepcopy
from pathlib import Path
from threading import Event
from typing import Any, Literal

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from buddy_desktop.action_correction import corrected_action, source_target
from buddy_desktop.pdf_document import open_pdf
from buddy_desktop.preflight import PreflightRunner, _private
from buddy_desktop.slide_contract import ContractError
from buddy_desktop.visual_qa import VERDICT_SCHEMA, Judge, _digest, inspect_action

CORRECTION_SCHEMA: dict[str, Any] = deepcopy(VERDICT_SCHEMA)
CORRECTION_SCHEMA["required"] = ["verdict", "confidence", "issue", "observation", "correction"]
CORRECTION_SCHEMA["properties"]["correction"] = {
    "anyOf": [
        {"type": "null"},
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["x", "y", "width", "height"],
            "properties": {key: {"type": "number"} for key in ("x", "y", "width", "height")},
        },
    ]
}


def inspect_with_corrections(
    runner: PreflightRunner,
    owner: str,
    *,
    source: bytes,
    source_format: Literal["pdf", "pptx"],
    deck_id: str,
    revision: str,
    slides: dict[str, dict[str, Any]],
    action: dict[str, Any],
    model_id: str,
    judge: Judge,
    output: Path,
    profile: str = "wide-1x",
    viewport: tuple[int, int] | None = None,
    cancelled: Event | None = None,
    max_corrections: int = 2,
) -> dict[str, Any]:
    if type(max_corrections) is not int or not 0 <= max_corrections <= 2:
        raise ContractError("At most two corrections are permitted.")
    runner._authorize(owner)
    output.mkdir(mode=0o700)
    _private(output)
    manifest = None
    if source_format == "pdf":
        manifest, _ = open_pdf(source, deck_id=deck_id, revision=revision)
    target = dimensions = None
    target_reason = None
    try:
        target, dimensions = source_target(source, action, manifest, slides)
    except (ContractError, KeyError, ValueError, OSError):
        target_reason = "source_target_unavailable"
    current = deepcopy(action)
    history = []
    final: dict[str, Any] = {}
    stop_reason = None
    for attempt in range(max_corrections + 1):
        proposal: object = None

        def inspect(target_context: dict[str, Any], before: bytes, after: bytes) -> object:
            nonlocal proposal
            response = judge(target_context, before, after)
            if not Draft202012Validator(CORRECTION_SCHEMA).is_valid(response):
                raise ContractError("Invalid correction verdict.")
            assert isinstance(response, dict)
            proposal = response["correction"]
            return {key: value for key, value in response.items() if key != "correction"}

        context = None
        if target is not None and dimensions is not None:
            context = {
                "source_target_bounds": target,
                "slide_width": dimensions[0],
                "slide_height": dimensions[1],
                "current_region": current["params"]["region"],
                "remaining_attempts": max_corrections - attempt,
            }
        final = inspect_action(
            runner,
            owner,
            source=source,
            source_format=source_format,
            deck_id=deck_id,
            revision=revision,
            slides=slides,
            action=current,
            model_id=model_id,
            judge=inspect,
            output=output / f"attempt-{attempt}",
            profile=profile,
            viewport=viewport,
            cancelled=cancelled,
            correction_context=context,
        )
        entry: dict[str, Any] = {
            "version": attempt,
            "action_sha256": _digest(current),
            "parameters": deepcopy(current["params"]),
            "state": final["state"],
            "reason": final["reason"],
            "report": f"attempt-{attempt}/report.json",
            "observation": (final.get("model_verdict") or {}).get("observation"),
            "proposed_region": proposal,
        }
        history.append(entry)
        if final["state"] != "rejected" or (cancelled and cancelled.is_set()):
            break
        if attempt == max_corrections:
            stop_reason = "attempt_limit"
            break
        if target_reason or target is None or dimensions is None:
            stop_reason = target_reason
            break
        if proposal is None:
            stop_reason = "no_safe_correction"
            break
        try:
            candidate = corrected_action(current, proposal, target, dimensions, manifest)
            if any(_digest(candidate) == item["action_sha256"] for item in history):
                raise ContractError("Correction repeated an earlier attempt.")
        except (ContractError, KeyError, ValueError):
            stop_reason = "correction_outside_supported_bounds"
            break
        entry["corrected_parameters"] = deepcopy(candidate["params"])
        current = candidate
        # Persist every completed version before executing the next native attempt.
        _save(output, action, current, final, history, "running")
    return _save(output, action, current, final, history, stop_reason)


def _save(
    output: Path,
    original: dict[str, Any],
    current: dict[str, Any],
    final: dict[str, Any],
    history: list[dict[str, Any]],
    stop_reason: str | None,
) -> dict[str, Any]:
    result = {
        **final,
        "schema_version": "buddy.action-correction.v1",
        "original_action_sha256": _digest(original),
        "corrected_action": current if final.get("state") == "validated" else None,
        "history": history,
        "evidence": {
            role: {**item, "file": f"attempt-{len(history) - 1}/" + item["file"]}
            for role, item in final.get("evidence", {}).items()
        },
        "stop_reason": stop_reason,
    }
    temporary = output / "report.tmp"
    temporary.write_text(json.dumps(result, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(output / "report.json")
    return result
