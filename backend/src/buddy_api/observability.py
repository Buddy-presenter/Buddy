"""Privacy-safe CloudWatch Embedded Metric Format events for Buddy operations."""

from __future__ import annotations

import json
import math
import re
import sys
import time
from collections.abc import Mapping
from typing import Literal

_ENVIRONMENTS = {"development", "test", "staging", "production"}
_SERVICES = {"api", "worker"}
_STAGES = {
    "upload",
    "pdf_metadata.v1",
    "pdf_analysis.v1",
    "pdf_render",
    "validation",
    "provider",
}
_OUTCOMES = {"success", "rejected", "failed", "retry", "dead_letter"}
_METRICS = {
    "UploadRequest": "Count",
    "JobAttempt": "Count",
    "JobOutcome": "Count",
    "StageOutcome": "Count",
    "DurationMs": "Milliseconds",
    "InputTokens": "Count",
    "OutputTokens": "Count",
}
_OPAQUE_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


def emit_metric(
    *,
    environment: str,
    service: Literal["api", "worker"],
    stage: str,
    outcome: str,
    metric: str,
    value: int | float = 1,
    correlation_ids: Mapping[str, str] | None = None,
    analysis_version: int | None = None,
) -> None:
    """Emit one EMF datapoint with bounded dimensions and optional opaque correlation."""
    if (
        environment not in _ENVIRONMENTS
        or service not in _SERVICES
        or stage not in _STAGES
        or outcome not in _OUTCOMES
        or metric not in _METRICS
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (
            correlation_ids is not None
            and any(
                key not in {"request_id", "job_id", "deck_id", "source_revision", "analysis_id"}
                or not isinstance(identifier, str)
                or not _OPAQUE_ID.fullmatch(identifier)
                for key, identifier in correlation_ids.items()
            )
        )
        or (
            analysis_version is not None
            and (
                isinstance(analysis_version, bool)
                or not isinstance(analysis_version, int)
                or analysis_version < 1
            )
        )
    ):
        return

    document: dict[str, object] = {
        "_aws": {
            "Timestamp": time.time_ns() // 1_000_000,
            "CloudWatchMetrics": [
                {
                    "Namespace": "Buddy/Operational",
                    "Dimensions": [
                        ["Environment", "Service", "Stage", "Outcome"],
                        ["Environment", "Service", "Outcome"],
                        ["Environment", "Service"],
                    ],
                    "Metrics": [
                        {"Name": metric, "Unit": _METRICS[metric]},
                    ],
                }
            ],
        },
        "Environment": environment,
        "Service": service,
        "Stage": stage,
        "Outcome": outcome,
        metric: value,
    }
    if correlation_ids:
        document.update(correlation_ids)
    if analysis_version is not None:
        document["analysis_version"] = analysis_version
    # EMF must be a raw JSON log event; Python's default logging formatter prefixes it.
    try:
        sys.stdout.write(json.dumps(document, separators=(",", ":"), allow_nan=False) + "\n")
        sys.stdout.flush()
    except OSError:
        # Telemetry must never fail an upload or consume a worker attempt.
        return
