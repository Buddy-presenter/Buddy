import json
from pathlib import Path
from runpy import run_path

build_report = run_path(
    str(Path(__file__).resolve().parents[2] / "scripts" / "presentation_latency_report.py")
)["build_report"]


def test_latency_report_groups_actions_and_percentiles_without_emitting_log_text(tmp_path):
    desktop = tmp_path / "desktop.log"
    backend = tmp_path / "backend.log"
    desktop.write_text(
        "\n".join(
            [
                "INFO presentation_latency event=proposal_received decision_id=a "
                "capture_to_proposal_ms=200 submit_to_proposal_ms=180 "
                "server_elapsed_ms=150",
                "INFO presentation_latency event=action_finished decision_id=a "
                "source_format=pdf tool=next_slide outcome=completed "
                "capture_to_action_ms=500 proposal_to_action_ms=300 "
                "preflight_ms=100 executor_ms=20",
                "INFO presentation_latency event=action_finished decision_id=b "
                "source_format=pdf tool=next_slide outcome=completed "
                "capture_to_action_ms=700 proposal_to_action_ms=400 "
                "preflight_ms=120 executor_ms=30",
                "INFO presentation_latency event=action_finished decision_id=c "
                "source_format=pptx tool=next_slide outcome=failed "
                "capture_to_action_ms=900 proposal_to_action_ms=600 "
                "preflight_ms=150 executor_ms=40",
                "INFO presentation_latency event=pdf_prefetch outcome=ready elapsed_ms=60 "
                "cache_bytes=1000",
                "INFO presentation_latency event=pdf_prefetch outcome=hit cache_bytes=1000",
                "",
            ]
        ),
        encoding="utf-8",
    )
    backend.write_text(
        json.dumps(
            {
                "event": "presentation_agent_decision",
                "elapsed_ms": 160,
                "phase_timings_ms": {"retrieval": 20, "selection": 100},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_report([desktop, backend])

    assert report["actions"]["pdf"] == {
        "sample_count": 2,
        "p50_ms": 500,
        "p95_ms": 700,
        "outcomes": {"completed": 2},
    }
    assert report["actions"]["pptx"]["sample_count"] == 0
    assert report["actions"]["pptx"]["outcomes"] == {"failed": 1}
    assert report["proposals"]["capture_to_proposal"]["p50_ms"] == 200
    assert report["backend"]["elapsed"]["p95_ms"] == 160
    assert report["backend"]["phases"]["selection"]["p50_ms"] == 100
    assert report["pdf_prefetch_outcomes"] == {"hit": 1, "ready": 1}
    assert "Our private presentation" not in json.dumps(report)


def test_latency_percentiles_are_empty_when_no_events_are_present(tmp_path):
    empty = tmp_path / "empty.log"
    empty.write_text("unrelated warning\n", encoding="utf-8")

    report = build_report([empty])

    assert report["actions"] == {}
    assert report["proposals"]["capture_to_proposal"] == {
        "sample_count": 0,
        "p50_ms": None,
        "p95_ms": None,
    }
