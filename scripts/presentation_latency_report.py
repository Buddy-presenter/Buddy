"""Summarize privacy-safe Buddy presentation latency logs as percentile JSON."""

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_FIELDS = re.compile(r"([a-z_]+)=([^\s]+)")


def _percentiles(values: list[int]) -> dict[str, int | None]:
    ordered = sorted(values)

    def at(percentile: float) -> int | None:
        if not ordered:
            return None
        return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]

    return {"sample_count": len(ordered), "p50_ms": at(0.50), "p95_ms": at(0.95)}


def build_report(paths: list[Path]) -> dict[str, Any]:
    action_times: dict[str, list[int]] = defaultdict(list)
    action_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    proposal_capture: list[int] = []
    proposal_server: list[int] = []
    backend_elapsed: list[int] = []
    backend_phases: dict[str, list[int]] = defaultdict(list)
    prefetch_outcomes: Counter[str] = Counter()

    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lstrip().startswith("{"):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = None
                if isinstance(event, dict) and event.get("event") == "presentation_agent_decision":
                    if type(event.get("elapsed_ms")) is int and event["elapsed_ms"] >= 0:
                        backend_elapsed.append(event["elapsed_ms"])
                    phases = event.get("phase_timings_ms")
                    if isinstance(phases, dict):
                        for name, duration in phases.items():
                            if isinstance(name, str) and type(duration) is int and duration >= 0:
                                backend_phases[name].append(duration)
                    continue
            if "presentation_latency " not in line:
                continue
            fields = dict(_FIELDS.findall(line.split("presentation_latency ", 1)[1]))
            event_name = fields.get("event")
            if event_name == "proposal_received":
                for name, target in (
                    ("capture_to_proposal_ms", proposal_capture),
                    ("server_elapsed_ms", proposal_server),
                ):
                    try:
                        value = int(fields[name])
                    except (KeyError, ValueError):
                        continue
                    if value >= 0:
                        target.append(value)
            elif event_name == "action_finished":
                source, outcome = (
                    fields.get("source_format", "unknown"),
                    fields.get("outcome", "unknown"),
                )
                action_outcomes[source][outcome] += 1
                if outcome == "completed":
                    try:
                        value = int(fields["capture_to_action_ms"])
                    except (KeyError, ValueError):
                        continue
                    if value >= 0:
                        action_times[source].append(value)
            elif event_name == "pdf_prefetch":
                prefetch_outcomes[fields.get("outcome", "unknown")] += 1

    formats = sorted(set(action_times) | set(action_outcomes))
    return {
        "actions": {
            source: {
                **_percentiles(action_times[source]),
                "outcomes": dict(sorted(action_outcomes[source].items())),
            }
            for source in formats
        },
        "proposals": {
            "capture_to_proposal": _percentiles(proposal_capture),
            "server_elapsed": _percentiles(proposal_server),
        },
        "backend": {
            "elapsed": _percentiles(backend_elapsed),
            "phases": {
                name: _percentiles(values) for name, values in sorted(backend_phases.items())
            },
        },
        "pdf_prefetch_outcomes": dict(sorted(prefetch_outcomes.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path, help="Buddy log files to summarize")
    args = parser.parse_args(argv)
    missing = [path for path in args.logs if not path.is_file()]
    if missing:
        parser.error("log file does not exist: " + ", ".join(str(path) for path in missing))
    print(json.dumps(build_report(args.logs), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
