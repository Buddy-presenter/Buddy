"""Import content-free desktop action timings into matching Langfuse decision traces."""

import argparse
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

from buddy_api.agent_tracing import AgentTracer, finish

FIELDS = re.compile(r"([a-z_]+)=([^\s]+)")
TOOLS = {
    "next_slide",
    "previous_slide",
    "go_to_slide",
    "reset_view",
    "zoom_region",
    "highlight_region",
}
TIMINGS = {"capture_to_action_ms", "proposal_to_action_ms", "preflight_ms", "executor_ms"}


def execution_records(paths: list[Path]) -> Iterator[tuple[UUID, dict[str, Any]]]:
    seen: set[UUID] = set()
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                if "presentation_latency event=action_finished " not in line:
                    continue
                fields = dict(FIELDS.findall(line))
                try:
                    decision_id = UUID(fields["decision_id"])
                    timings = {name: int(fields[name]) for name in TIMINGS}
                except (KeyError, ValueError):
                    continue
                if (
                    decision_id in seen
                    or fields.get("tool") not in TOOLS
                    or fields.get("source_format") not in {"pdf", "pptx"}
                    or any(not 0 <= value <= 3_600_000 for value in timings.values())
                ):
                    continue
                seen.add(decision_id)
                yield (
                    decision_id,
                    {
                        **timings,
                        "tool": fields["tool"],
                        "source_format": fields["source_format"],
                        # Preserve only a success flag; never forward arbitrary log strings.
                        "completed": fields.get("outcome") == "completed",
                        "decision_id": str(decision_id),
                    },
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    if any(not path.is_file() for path in args.logs):
        parser.error("Every log path must be an existing file.")
    tracer = AgentTracer.from_environment()
    if tracer.client is None:
        parser.error("Enable BUDDY_LANGFUSE_ENABLED and configure the backend Langfuse SDK.")
    count = 0
    try:
        for decision_id, metadata in execution_records(args.logs):
            observation = tracer.start(
                "desktop.execution_timing", decision_id, as_type="span", metadata=metadata
            )
            if observation is not None:
                finish(observation)
                count += 1
    finally:
        tracer.shutdown()
    print(f"Queued {count} execution timing observations; verify delivery in Langfuse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
