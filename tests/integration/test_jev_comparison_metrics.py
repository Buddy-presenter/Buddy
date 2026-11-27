"""Benchmark accounting must not hide wrong choices behind abstention or execution."""

import importlib.util
from pathlib import Path

# Backend/desktop test packages share the name `tests` in the all-components run.
spec = importlib.util.spec_from_file_location(
    "jev_comparison_metrics", Path(__file__).with_name("jev_comparison_metrics.py")
)
assert spec is not None and spec.loader is not None
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
correct, percentile, summarize = metrics.correct, metrics.percentile, metrics.summarize


def test_target_scoring_accepts_navigation_alias_but_requires_exact_action():
    assert correct(
        {"tool": "go_to_slide", "slide": 1}, {"tool": "next_slide", "slide_id": "b"}, ["a", "b"]
    )
    assert not correct(
        {"tool": "go_to_slide", "slide": 1}, {"tool": "next_slide", "slide_id": "a"}, ["a", "b"]
    )
    assert not correct(
        {"tool": "zoom_region", "action_id": "a"}, {"tool": "zoom_region", "action_id": "b"}, []
    )


def test_summary_separates_errors_misses_wrong_actions_and_native_completion():
    base = dict(
        source_format="pdf",
        mode="semantic",
        wall_ms=100,
        expected={"tool": "zoom_region"},
        correct=False,
    )
    rows = [
        {**base, "target": {"tool": "no_action"}},
        {**base, "target": {"tool": "highlight_region"}},
        {**base, "target": {"tool": "no_action"}, "error": "deadline"},
        {
            **base,
            "target": {"tool": "zoom_region"},
            "correct": True,
            "execution": {"completed": True},
        },
        {**base, "expected": {"tool": "no_action"}, "target": {"tool": "zoom_region"}},
        {
            **base,
            "expected": {"tool": "no_action"},
            "target": {"tool": "no_action"},
            "correct": True,
        },
    ]
    result = summarize(rows)["pdf:semantic"]
    assert result["samples"] == 6 and result["correct"] == 2
    assert result["false_triggers"] == 1 and result["correct_abstentions"] == 1
    assert result["abstained_on_required_action"] == 1
    assert result["wrong_nonempty_actions"] == 1
    assert result["provider_or_pipeline_errors"] == 1
    assert result["native_checks"] == result["native_completed"] == 1
    assert percentile([], 0.5) is None and percentile([20, 10], 0.95) == 20
