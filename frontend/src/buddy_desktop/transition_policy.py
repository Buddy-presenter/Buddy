"""Deterministic presenter-owned transition admission, independent of Qt and providers.

This module never executes a target or treats model confidence as a probability.
The caller supplies current authoritative eligibility and observes actual completion.
"""

import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from buddy_desktop.transcript_context import ContextSnapshot

VERSION = "transition-policy-1"
MODEL = "command-a-03-2025"


@dataclass(frozen=True)
class Target:
    tool: str
    slide_id: str
    action_id: str | None = None
    region: tuple[float, float, float, float] | None = None

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "Target":
        region = value.get("region")
        return cls(
            value["tool"],
            value["slide_id"],
            value.get("action_id"),
            (
                float(region["x"]),
                float(region["y"]),
                float(region["width"]),
                float(region["height"]),
            )
            if region is not None
            else None,
        )


@dataclass(frozen=True)
class Calibration:
    source_format: Literal["pdf", "pptx"]
    threshold: float
    samples: int
    successes: int
    reference: str
    expires: date
    # Synthetic calibration is useful for deterministic tests, never live admission.
    consented: bool

    @property
    def lower_precision(self) -> float:
        """95% Wilson lower bound, not the provider's self-reported confidence."""
        n, z = self.samples, 1.96
        if n <= 0:
            return 0
        p = self.successes / n
        return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (
            1 + z * z / n
        )

    def usable(self, source_format: str, today: date) -> bool:
        return (
            self.consented
            and self.source_format == source_format
            and today <= self.expires
            and self.samples >= 100
            and self.lower_precision >= 0.95
        )


def load_calibration(path: Path) -> Calibration:
    """Read only aggregate, format/model-bound calibration, with bounded strict input."""
    with path.open("rb") as handle:
        data = handle.read(65537)
    if len(data) > 65536:
        raise ValueError("Calibration exceeds its size limit.")
    raw = json.loads(data)
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "protocol",
            "model",
            "source_format",
            "threshold",
            "samples",
            "successes",
            "development_sha256",
            "permission",
        }
        or raw["protocol"] != VERSION
        or raw["model"] != MODEL
    ):
        raise ValueError("Unsupported policy calibration.")
    n, correct, threshold, permission = (
        raw["samples"],
        raw["successes"],
        raw["threshold"],
        raw["permission"],
    )
    reference = raw["development_sha256"]
    if (
        raw["source_format"] not in {"pdf", "pptx"}
        or type(n) is not int
        or not 1 <= n <= 1_000_000
        or type(correct) is not int
        or not 0 <= correct <= n
        or type(threshold) not in {int, float}
        or not math.isfinite(threshold)
        or not 0.5 <= threshold <= 1
        or not isinstance(reference, str)
        or len(reference) != 64
        or any(c not in "0123456789abcdef" for c in reference)
        or not isinstance(permission, dict)
        or set(permission) != {"kind", "reference", "expires"}
        or permission["kind"] not in {"synthetic", "consented"}
        or not isinstance(permission["reference"], str)
        or not 1 <= len(permission["reference"]) <= 256
    ):
        raise ValueError("Invalid calibration evidence.")
    return Calibration(
        raw["source_format"],
        float(threshold),
        n,
        correct,
        reference,
        date.fromisoformat(permission["expires"]),
        permission["kind"] == "consented",
    )


@dataclass(frozen=True)
class Settings:
    cooldown_ms: int = 750
    manual_hold_ms: int = 5000

    def __post_init__(self) -> None:
        if (
            type(self.cooldown_ms) is not int
            or not 0 <= self.cooldown_ms <= 60000
            or type(self.manual_hold_ms) is not int
            or not 0 <= self.manual_hold_ms <= 60000
        ):
            raise ValueError("Invalid transition policy settings.")


@dataclass(frozen=True)
class Prediction:
    decision_id: str
    target: Target
    source: str
    reason: str
    confidence: float | None
    evidence_sequences: tuple[int, ...]
    session_id: str
    analysis_id: str
    state_version: int
    epoch: int
    stream_id: str
    sequence: int

    @classmethod
    def from_validated(cls, value: dict[str, Any], context: ContextSnapshot) -> "Prediction":
        segments = context.candidate_segments
        return cls(
            value["decision_id"],
            Target.parse(value["target"]),
            value["decision_source"],
            value["reason"],
            value["confidence"],
            tuple(segments[i].sequence for i in value["evidence"]),
            value["session_id"],
            value["analysis_id"],
            value["state_version"],
            value["context_epoch"],
            value["stream_id"],
            value["sequence"],
        )


@dataclass(frozen=True)
class View:
    session_id: str
    analysis_id: str
    source_identity: str
    source_format: str
    state_version: int
    current: Target
    # Recomputed from saved source/native contracts, never provider-supplied tools.
    eligible: tuple[Target, ...]
    busy: bool = False
    connected: bool = True


class TransitionPolicy:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.enabled = False
        self._binding: tuple[object, ...] | None = None
        self._hold_until = 0
        self._inflight: str | None = None
        self._seen: OrderedDict[tuple[object, ...], None] = OrderedDict()

    def clear_pending(self) -> None:
        # Retained for callers that invalidate speech context. A policy decision is
        # admitted atomically now; there is no multi-utterance confirmation buffer.
        return

    def reset(self, *, manual: bool = False, now_ns: int = 0) -> None:
        self._binding = None
        if manual:
            self._hold_until = max(
                self._hold_until, now_ns + self.settings.manual_hold_ms * 1_000_000
            )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self._inflight = None
        self.reset()

    def completed(self, decision_id: str, *, success: bool, now_ns: int) -> None:
        if decision_id != self._inflight:
            return
        self._inflight = None
        self.reset()
        self._hold_until = now_ns + self.settings.cooldown_ms * 1_000_000

    def consider(
        self,
        prediction: Prediction,
        context: ContextSnapshot,
        view: View,
        *,
        now_ns: int,
        today: date,
    ) -> str:
        """Return a finite reason. Only 'accepted' authorizes one immediate dispatch."""

        def reject(reason: str) -> str:
            self.clear_pending()
            return reason

        if not self.enabled:
            return reject("disabled")
        if view.source_format not in {"pdf", "pptx"}:
            return reject("unsupported_format")
        if view.busy or self._inflight is not None:
            return reject("busy")
        segments = context.candidate_segments
        if (
            not view.connected
            or not context.fresh
            or not segments
            or context.segments != segments  # An unfinished partial blocks the whole window.
            or not 0 <= now_ns - segments[-1].capture_end_ns <= 5_000_000_000
            or (prediction.session_id, prediction.analysis_id, prediction.state_version)
            != (view.session_id, view.analysis_id, view.state_version)
            or (context.session_id, context.state_version, context.epoch, context.stream_id)
            != (view.session_id, view.state_version, prediction.epoch, prediction.stream_id)
            or prediction.sequence != max(segment.sequence for segment in segments)
        ):
            return reject("stale")
        binding = (
            view.session_id,
            view.analysis_id,
            view.source_identity,
            view.source_format,
            view.state_version,
            context.epoch,
            context.stream_id,
            context.language,
        )
        if binding != self._binding:
            self.reset()
            self._binding = binding
        target = prediction.target
        if target.tool == "no_action":
            return reject("no_action")
        if target not in view.eligible:
            return reject("ineligible")
        if (
            (target.slide_id, target.tool, target.region)
            == (view.current.slide_id, view.current.tool, view.current.region)
            or target.tool == "reset_view"
            and view.current.tool == "slide"
        ):
            return reject("current_focus")
        evidence_key = (
            prediction.session_id,
            prediction.epoch,
            prediction.stream_id,
            segments[-1].sequence,
        )
        if evidence_key in self._seen:
            return reject("duplicate")
        if now_ns < self._hold_until:
            return reject("cooldown")
        if (
            prediction.source not in {"model", "navigation"}
            or prediction.reason not in {"explicit", "topic", "correction"}
            or segments[-1].sequence not in prediction.evidence_sequences
        ):
            return reject("weak_evidence")
        if prediction.source == "navigation":
            if prediction.reason != "explicit" or target.tool not in {
                "next_slide",
                "previous_slide",
                "go_to_slide",
            }:
                return reject("weak_evidence")
        else:
            confidence = prediction.confidence
            if confidence is None or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                return reject("weak_evidence")
        self._seen[evidence_key] = None
        # Bound deduplication to this presenter lifetime without turning automation off.
        if len(self._seen) > 2048:
            self._seen.popitem(last=False)
        self._inflight = prediction.decision_id
        self.clear_pending()
        return "accepted"

    @property
    def calibration_sha256(self) -> str | None:
        # Calibration artifacts remain a development/evaluation tool, not an end-user
        # prerequisite. Keep this nullable telemetry field for trace compatibility.
        return None

    @property
    def settings_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self.settings), sort_keys=True).encode()
        ).hexdigest()
