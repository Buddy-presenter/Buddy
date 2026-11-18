"""Owner-scoped visual verdicts from the isolated presentation players."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, Literal

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from buddy_desktop.preflight import PreflightRunner, Tool, _private

ISSUES = (
    "wrong_target",
    "partial_target",
    "clipped",
    "obscured",
    "unreadable",
    "no_visible_effect",
    "uncertain",
)
VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "confidence", "issue", "observation"],
    "properties": {
        "verdict": {"enum": ["validated", "rejected"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "issue": {"enum": [None, *ISSUES]},
        "observation": {"type": "string", "minLength": 1, "maxLength": 400},
    },
}
Judge = Callable[[dict[str, Any], bytes, bytes], object]


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


def inspect_action(
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
) -> dict[str, Any]:
    """Inspect one immutable saved action, retaining its private report and captures."""
    if output.exists():
        raise FileExistsError("Choose a fresh private output directory.")
    action_id, slide_id = action["instance_id"], action["slide_id"]
    action_hash, slide_map_hash = _digest(action), _digest(slides)
    tools: list[Tool] = [
        {"op": "load_slide", "slide_id": slide_id},
        {"op": "capture"},
        {"op": "apply_action", "action_id": action_id},
        {"op": "read_state"},
        {"op": "capture"},
    ]
    run = runner.run(
        owner,
        source=source,
        source_format=source_format,
        deck_id=deck_id,
        revision=revision,
        slides=slides,
        catalog={action_id: action},
        tools=tools,
        profile=profile,
        viewport=viewport,
        cancelled=cancelled,
    )
    output.mkdir(mode=0o700)
    _private(output)
    report: dict[str, Any] = {
        "schema_version": "buddy.visual-qa.v1",
        "deck_id": deck_id,
        "action_id": action_id,
        "action_sha256": action_hash,
        "slide_map_sha256": slide_map_hash,
        "display_profile": profile,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "slide_id": slide_id,
        "target": action["purpose"],
        "model_id": model_id,
        "state": "unavailable",
        "reason": run["reason"],
        "confidence": None,
        "model_verdict": None,
        "semantic_verified": False,
        "preflight": run,
        "evidence": {},
    }
    images: list[bytes] = []
    if run["state"] == "completed":
        try:
            trace = run["trace"]
            if (
                [step["tool"] for step in trace] != tools
                or run.get("source_unchanged") is not True
                or any(step["after"]["slide_id"] != slide_id for step in trace)
                or trace[2]["baseline"] != trace[1]["after"]
                or trace[2]["after"] != trace[3]["after"]
                or trace[3]["after"] != trace[4]["after"]
            ):
                raise ValueError("Unexpected preflight trace")
            hashes = {item["name"]: item["sha256"] for item in run["artifacts"]}
            if set(hashes) != {"0.png", "1.png"} or len(run["artifacts"]) != 2:
                raise ValueError("Unexpected capture set")
            for index, (role, step) in enumerate((("before", trace[1]), ("after", trace[4]))):
                name = step["artifact"]
                if name != f"{index}.png":
                    raise ValueError("Unexpected capture order")
                data = runner.artifact(owner, run["run_id"], name)
                digest = hashlib.sha256(data).hexdigest()
                if digest != hashes[name]:
                    raise ValueError("Capture digest mismatch")
                evidence = f"{role}.png"
                report["evidence"][role] = {"file": evidence, "sha256": digest}
                images.append(data)
        except (KeyError, TypeError, ValueError, OSError):
            report.update(reason="capture_binding_failed", evidence={})
            images.clear()

    if images:
        for role, data in zip(("before", "after"), images, strict=True):
            (output / f"{role}.png").write_bytes(data)
        if cancelled and cancelled.is_set():
            report["reason"] = "cancelled"
        elif images[0] == images[1]:
            report.update(state="rejected", reason="no_visible_effect")
        elif any(len(data) > 3_750_000 for data in images):
            report["reason"] = "visual_image_limit"
        else:
            try:
                verdict = judge(
                    {
                        "purpose": action["purpose"],
                        "type": action["type"],
                        "source": source_format,
                        "target_text": action["params"].get("target_text"),
                    },
                    *images,
                )
                if not Draft202012Validator(VERDICT_SCHEMA).is_valid(verdict):
                    raise ValueError("Invalid visual verdict")
                assert isinstance(verdict, dict)
                state, confidence, issue = (
                    verdict["verdict"],
                    verdict["confidence"],
                    verdict["issue"],
                )
                if not 0 <= confidence <= 1 or not verdict["observation"].strip():
                    raise ValueError("Invalid visual observation")
                report.update(confidence=confidence, model_verdict=verdict)
                if cancelled and cancelled.is_set():
                    report["reason"] = "cancelled"
                elif (state == "validated") != (issue is None):
                    report.update(reason="conflicting_verdict", confidence=confidence)
                else:
                    report.update(
                        state=state,
                        confidence=confidence,
                        reason=issue,
                        semantic_verified=state == "validated",
                    )
            except Exception:
                # Provider responses and errors may contain private slide data.
                report["reason"] = "visual_verdict_unavailable"
    temporary = output / "report.tmp"
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(output / "report.json")
    return report
