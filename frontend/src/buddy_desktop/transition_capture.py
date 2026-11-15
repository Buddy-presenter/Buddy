"""Explicit private evaluation capture of actual player completion signals.

The evaluation driver supplies corpus frame/decision bindings and proposal observations.
This module has no backend imports and never infers execution from model output.
"""

import copy
from dataclasses import asdict, is_dataclass
from typing import Any, Literal


class TransitionCapture:
    def __init__(
        self,
        corpus_sha256: str,
        *,
        provenance: Literal["synthetic_contract", "captured_service"],
        policy_version: str,
        provider_model: str,
    ):
        if len(corpus_sha256) != 64 or any(c not in "0123456789abcdef" for c in corpus_sha256):
            raise ValueError("Invalid corpus digest")
        self.digest = corpus_sha256
        self.provenance = provenance
        self.policy_version = policy_version
        self.provider_model = provider_model
        self._frames: dict[tuple[str, str], dict[str, Any]] = {}
        self._decisions: dict[str, tuple[tuple[str, str], int, int]] = {}
        self._completed: set[str] = set()

    def arm(
        self, decision_id: str, observation: dict[str, Any], *, capture_ns: int, frame_at_ms: int
    ) -> None:
        key = (observation["session_id"], observation["frame_id"])
        if key in self._frames or decision_id in self._decisions or len(self._frames) >= 10000:
            raise ValueError("Duplicate or excessive capture frames")
        if capture_ns < 0 or frame_at_ms < 0 or observation.get("applied"):
            raise ValueError("Capture requires a fresh observation without inferred actions")
        self._frames[key] = copy.deepcopy(observation)
        self._frames[key]["applied"] = []
        self._decisions[decision_id] = key, capture_ns, frame_at_ms

    def completed(self, event: dict[str, Any]) -> None:
        identity = event["decision_id"]
        if identity not in self._decisions or identity in self._completed:
            raise ValueError("Unbound or duplicate player completion")
        key, capture_ns, frame_at_ms = self._decisions[identity]
        stamp = event["completed_ns"]
        if type(stamp) is not int or stamp < capture_ns:
            raise ValueError("Invalid player clock")
        target = event["target"]
        if is_dataclass(target) and not isinstance(target, type):
            target = asdict(target)
        else:
            target = copy.deepcopy(target)
        region = target.get("region")
        if isinstance(region, (tuple, list)):
            target["region"] = dict(zip(("x", "y", "width", "height"), region, strict=True))
        animated = event["animated"]
        if animated is not None and type(animated) is not bool:
            raise ValueError("Animation requires observed boolean evidence")
        self._frames[key]["applied"].append(
            {
                "target": target,
                "at_ms": frame_at_ms + (stamp - capture_ns) // 1_000_000,
                "animated": animated,
            }
        )
        self._completed.add(identity)

    def document(self) -> dict[str, Any]:
        return {
            "protocol": "presentation-replay-trace.v1",
            "corpus_sha256": self.digest,
            "provenance": self.provenance,
            "policy_version": self.policy_version,
            "provider_model": self.provider_model,
            "observations": copy.deepcopy(list(self._frames.values())),
        }
