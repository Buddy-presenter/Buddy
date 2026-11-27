"""Opt-in provider capacity probe; distractors are synthetic, not executable actions."""

import argparse
import asyncio
import copy
import json
import time
from pathlib import Path

import boto3
from tests.integration.compare_jev_candidates import secret

from buddy_api.agent_provider import AgentFailure, JevDecisionProvider


async def evaluate(key, passage, target):
    rows = []
    provider = JevDecisionProvider(key)
    try:
        for count in (16, 32, 64, 128, 255):
            for repeat in range(3):
                query = copy.deepcopy(passage)
                options = [item for item in query["options"] if item != target]
                while len(options) < count - 1:
                    options.append(
                        dict(
                            tool="go_to_slide",
                            slide_number=len(options) + 2,
                            content=f"Appendix {len(options)}: unrelated equipment inventory",
                            is_current_view=False,
                        )
                    )
                options.append(copy.deepcopy(target))
                for index, item in enumerate(options):
                    item["id"] = f"c{index}"
                query["options"] = options
                started = time.perf_counter()
                try:
                    selected = await provider.select(query)
                    row = dict(
                        choice=selected.choice,
                        correct=selected.choice == f"c{count - 1}",
                        confidence=selected.confidence,
                        error=None,
                    )
                except AgentFailure as exc:
                    row = dict(choice=None, correct=False, error=exc.code)
                rows.append(
                    dict(
                        options=count,
                        repeat=repeat,
                        **row,
                        wall_ms=round((time.perf_counter() - started) * 1000, 2),
                    )
                )
    finally:
        await provider.aclose()
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true")
    parser.add_argument("records", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", default="buddy-staging-tools")
    args = parser.parse_args()
    record = next(
        json.loads(line)
        for line in args.records.read_text().splitlines()
        if json.loads(line)["mode"] == "direct_current"
        and json.loads(line)["expected"]["tool"] == "highlight_region"
    )
    passage = record["diagnostics"]["provider_passage"]
    mapping = record["diagnostics"]["option_targets"]
    index = next(
        index
        for index, item in enumerate(mapping)
        if item.get("action_id") == record["expected"]["action_id"]
    )
    key = secret(
        boto3.Session(profile_name=args.profile, region_name="eu-central-1").client(
            "secretsmanager"
        ),
        "buddy-staging/agent/jev",
    )
    result = dict(
        scope="Provider-only synthetic distractor stress; no native execution",
        target_position="last",
        records=asyncio.run(evaluate(key, passage, passage["options"][index])),
    )
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
