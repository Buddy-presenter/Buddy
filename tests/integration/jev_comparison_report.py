"""Sanitized comparison report from synthetic, paired evaluation records only."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from tests.integration.jev_comparison_metrics import percentile, summarize


def report(records):
    pairs = defaultdict(dict)
    for row in records:
        pairs[(row["source_format"], row["case"], row["repeat"])][row["mode"]] = row
    contingency = dict(
        both_correct=0,
        semantic_only_correct=0,
        direct_only_correct=0,
        both_wrong=0,
        direct_faster=0,
        pairs=0,
    )
    deltas = []
    for pair in pairs.values():
        if set(pair) != {"semantic", "direct_current"}:
            continue
        baseline, direct = pair["semantic"], pair["direct_current"]
        key = (
            "both_correct"
            if baseline["correct"] and direct["correct"]
            else "semantic_only_correct"
            if baseline["correct"]
            else "direct_only_correct"
            if direct["correct"]
            else "both_wrong"
        )
        contingency[key] += 1
        contingency["pairs"] += 1
        delta = baseline["wall_ms"] - direct["wall_ms"]
        deltas.append(delta)
        contingency["direct_faster"] += delta > 0
    contingency["paired_saving_p50_ms"] = percentile(deltas, 0.5)
    contingency["paired_saving_p95_ms"] = percentile(deltas, 0.95)
    failures = []
    for row in records:
        if row["correct"] and row.get("execution", {}).get("completed", True):
            continue
        target = row.get("target", {})
        actual_slide_number = None
        diagnostics = row.get("diagnostics", {})
        for index, option in enumerate(diagnostics.get("option_targets") or []):
            if option == target:
                actual_slide_number = (diagnostics.get("candidate_options") or [])[index].get(
                    "slide_number"
                )
                break
        failures.append(
            dict(
                case=row["case"],
                source_format=row["source_format"],
                mode=row["mode"],
                repeat=row["repeat"],
                category=row["category"],
                input_mode=row["input_mode"],
                expected=row["expected"],
                actual_tool=target.get("tool"),
                actual_action=target.get("action_id"),
                actual_slide_number=actual_slide_number,
                error=row.get("error"),
                execution_error=row.get("execution", {}).get("error"),
            )
        )
    by_category = {}
    for category in sorted({r["category"] for r in records}):
        by_category[category] = summarize([r for r in records if r["category"] == category])
    coverage = {}
    for source in ("pdf", "pptx"):
        for mode in ("semantic", "direct_current"):
            focus = [
                r
                for r in records
                if r["source_format"] == source
                and r["mode"] == mode
                and r["expected"].get("action_id")
            ]
            missing = [
                dict(case=r["case"], repeat=r["repeat"])
                for r in focus
                if r["expected"]["action_id"]
                not in {
                    o.get("action_id")
                    for o in (r.get("diagnostics", {}).get("option_targets") or [])
                }
            ]
            coverage[source + ":" + mode] = dict(focus_requests=len(focus), missing=missing)
    return dict(
        schema="buddy.jev-comparison.public.v1",
        provenance="Authored synthetic sources and labels; real providers and isolated players",
        groups=summarize(records),
        text_groups=summarize([r for r in records if r["input_mode"] == "text"]),
        speech_groups=summarize([r for r in records if r["input_mode"] == "speech"]),
        model_only=summarize([r for r in records if r.get("decision_source") == "model"]),
        paired=contingency,
        routes=dict(Counter(r.get("route") for r in records)),
        errors=dict(Counter(r["error"] for r in records if r.get("error"))),
        candidate_coverage=coverage,
        by_category=by_category,
        failures=failures,
    )


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.records.read_text().splitlines()]
    args.output.write_text(json.dumps(report(rows), indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
