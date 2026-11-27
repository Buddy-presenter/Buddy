"""Pure paired-evaluation scoring; no provider calls or native dependencies."""


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    import math

    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 2)


def summarize(records):
    groups = {}
    for source in ("pdf", "pptx"):
        for mode in ("semantic", "direct_current"):
            rows = [r for r in records if r["source_format"] == source and r["mode"] == mode]
            values = [r["wall_ms"] for r in rows]
            positives = [r for r in rows if r["expected"]["tool"] != "no_action"]
            negatives = [r for r in rows if r["expected"]["tool"] == "no_action"]
            groups[source + ":" + mode] = dict(
                samples=len(rows),
                correct=sum(r["correct"] for r in rows),
                accuracy=round(sum(r["correct"] for r in rows) / len(rows), 4) if rows else None,
                false_triggers=sum(
                    r.get("target", {}).get("tool", "no_action") != "no_action" for r in negatives
                ),
                missed_actions=sum(not r["correct"] for r in positives),
                abstained_on_required_action=sum(
                    not r.get("error") and r.get("target", {}).get("tool") == "no_action"
                    for r in positives
                ),
                wrong_nonempty_actions=sum(
                    not r["correct"]
                    and not r.get("error")
                    and r.get("target", {}).get("tool") != "no_action"
                    for r in positives
                ),
                correct_abstentions=sum(r["correct"] for r in negatives),
                native_checks=sum("execution" in r for r in rows),
                native_completed=sum(r.get("execution", {}).get("completed", False) for r in rows),
                provider_or_pipeline_errors=sum(bool(r.get("error")) for r in rows),
                p50_ms=percentile(values, 0.5),
                p95_ms=percentile(values, 0.95),
                retrieval_p50_ms=percentile(
                    [r.get("phases", {}).get("retrieval", 0) for r in rows], 0.5
                ),
                selection_p50_ms=percentile(
                    [r.get("phases", {}).get("selection", 0) for r in rows], 0.5
                ),
                option_counts=sorted({r.get("option_count", 0) for r in rows}),
                embedding_calls=sum(r.get("embedding_calls", 0) for r in rows),
            )
    return groups


def correct(expected, target, slide_ids):
    if expected["tool"] == "go_to_slide":
        return (
            target.get("tool") in {"go_to_slide", "next_slide", "previous_slide"}
            and target.get("slide_id") == slide_ids[expected["slide"]]
        )
    if target.get("tool") != expected["tool"]:
        return False
    return not expected.get("action_id") or target.get("action_id") == expected["action_id"]
