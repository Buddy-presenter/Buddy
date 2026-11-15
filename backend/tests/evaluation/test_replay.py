"""Frozen contracts, independent scoring, hostile traces and fail-closed release gates."""

import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from buddy_api.presentation_agent import Target
from buddy_api.replay import (
    AppliedAction,
    Corpus,
    Gates,
    Permission,
    Trace,
    load_corpus,
    main,
    replay,
    score,
)

FIXTURES = Path(__file__).resolve().parents[3] / "tests/fixtures/replay"


@pytest.fixture
def sample():
    corpus, digest = load_corpus(FIXTURES / "development.json")
    gates = Gates.model_validate_json((FIXTURES / "development-gates.json").read_bytes())
    trace = asyncio.run(replay(corpus, digest))
    return corpus, digest, gates, trace


@pytest.mark.parametrize("split", ["development", "holdout"])
def test_frozen_contract_gate(split):
    corpus, digest = load_corpus(FIXTURES / f"{split}.json")
    gates = Gates.model_validate_json((FIXTURES / f"{split}-gates.json").read_bytes())
    first = asyncio.run(replay(corpus, digest))
    assert first == asyncio.run(replay(corpus, digest))
    report = score(corpus, digest, first, gates, release=False)
    assert report["passed"]
    assert report["scope"] == "agent_proposals"
    assert report["metrics"]["unnecessary_animations"] is None
    assert report["metrics"]["outside_candidate_set"] == 0
    assert all(o.proposed in o.allowed for o in first.observations)
    reasons = {o.frame_id: o.reason for o in first.observations}
    for identifier in ("injection", "invented-choice", "bad-evidence"):
        assert reasons[identifier] == "invalid_output"
    assert reasons["throttled"] == "rate_limited"


def test_synthetic_success_cannot_pass_release(sample):
    corpus, digest, gates, trace = sample
    report = score(corpus, digest, trace, gates, release=True)
    assert not report["passed"]
    assert "real_service_policy_and_permission_evidence_required" in report["failures"]


def test_trace_is_complete_unique_and_bound_to_corpus(sample):
    corpus, digest, gates, trace = sample
    for changed in (
        trace.model_copy(update={"observations": trace.observations[:-1]}),
        trace.model_copy(update={"observations": trace.observations + trace.observations[:1]}),
        trace.model_copy(update={"corpus_sha256": "0" * 64}),
    ):
        with pytest.raises(ValueError):
            score(corpus, digest, changed, gates, release=False)
    with pytest.raises(ValueError):
        score(
            corpus,
            digest,
            trace,
            gates.model_copy(update={"corpus_sha256": "0" * 64}),
            release=False,
        )


def test_metrics_detect_wrong_target_abstention_latency_and_cost(sample):
    corpus, digest, gates, trace = sample
    original = trace.observations[0]
    bad = original.model_copy(
        update={
            "proposed": original.allowed[-1],
            "elapsed_ms": 1000,
            "input_tokens": 1_000_000,
        }
    )
    observations = [bad, *trace.observations[1:]]
    # Ensure P95 is sensitive to more than one high-latency sample.
    observations[1] = observations[1].model_copy(update={"elapsed_ms": 1000})
    changed = trace.model_copy(update={"observations": tuple(observations)})
    report = score(corpus, digest, changed, gates, release=False)
    assert not report["passed"]
    assert {
        "target_accuracy",
        "no_action_recall",
        "false_transitions_per_minute",
        "latency_p95_ms",
        "cost_usd",
    } <= set(report["failures"])


def test_invented_target_and_forged_allowlist_never_pass(sample):
    corpus, digest, gates, trace = sample
    observation = trace.observations[0]
    injected = Target(tool="go_to_slide", slide_id="not-in-deck")
    for field in ("proposed", "allowed"):
        bad = observation.model_copy(
            update={field: injected if field == "proposed" else (injected,)}
        )
        changed = trace.model_copy(update={"observations": (bad, *trace.observations[1:])})
        if field == "allowed":
            with pytest.raises(ValueError):
                score(corpus, digest, changed, gates, release=False)
        else:
            assert (
                "outside_candidate_set"
                in score(corpus, digest, changed, gates, release=False)["failures"]
            )


def test_wrong_abstention_fails_precision_gate(sample):
    corpus, digest, gates, trace = sample
    observations = list(trace.observations)
    index = next(i for i, o in enumerate(observations) if o.proposed.tool != "no_action")
    observations[index] = observations[index].model_copy(
        update={"proposed": observations[index].allowed[0]}
    )
    changed = trace.model_copy(update={"observations": tuple(observations)})
    assert "no_action_precision" in score(corpus, digest, changed, gates, release=False)["failures"]


def policy_trace(corpus, trace):
    frames = {(s.id, f.id): f for s in corpus.sessions for f in s.frames}
    observations = []
    for observation in trace.observations:
        frame = frames[observation.session_id, observation.frame_id]
        applied = (
            ()
            if frame.expected.tool == "no_action"
            else (AppliedAction(target=frame.expected, at_ms=frame.at_ms + 100, animated=True),)
        )
        observations.append(observation.model_copy(update={"applied": applied}))
    return trace.model_copy(
        update={"policy_version": "test-policy-1", "observations": tuple(observations)}
    )


def test_applied_metrics_and_repeated_animation_regression(sample):
    corpus, digest, gates, trace = sample
    trace = policy_trace(corpus, trace)
    report = score(corpus, digest, trace, gates, release=False)
    assert report["passed"]
    assert report["scope"] == "applied_policy"
    assert report["metrics"]["unnecessary_animations"] == 0
    index = next(i for i, o in enumerate(trace.observations) if o.applied)
    observations = list(trace.observations)
    observations[index] = observations[index].model_copy(
        update={"applied": observations[index].applied * 2}
    )
    repeated = trace.model_copy(update={"observations": tuple(observations)})
    report = score(corpus, digest, repeated, gates, release=False)
    assert report["metrics"]["unnecessary_animations"] == 1
    assert "unnecessary_animations" in report["failures"]
    assert "false_transitions_per_minute" in report["failures"]


def test_applied_actions_require_policy_identity_and_valid_clock(sample):
    corpus, digest, gates, trace = sample
    trace = policy_trace(corpus, trace)
    with pytest.raises(ValueError):
        score(
            corpus, digest, trace.model_copy(update={"policy_version": None}), gates, release=False
        )
    observations = list(trace.observations)
    index = next(i for i, o in enumerate(observations) if o.applied)
    observations[index] = observations[index].model_copy(
        update={"applied": (observations[index].applied[0].model_copy(update={"at_ms": 0}),)}
    )
    with pytest.raises(ValueError):
        score(
            corpus,
            digest,
            trace.model_copy(update={"observations": tuple(observations)}),
            gates,
            release=False,
        )


def test_permission_expiration_and_schema_boundaries(tmp_path, sample):
    corpus, *_ = sample
    with pytest.raises(ValueError):
        Permission(kind="consented", reference="consent-1")
    expired = corpus.model_copy(
        update={
            "permission": Permission(
                kind="consented", reference="consent-1", expires=date(2020, 1, 1)
            )
        }
    )
    path = tmp_path / "expired.json"
    path.write_text(expired.model_dump_json())
    with pytest.raises(ValueError):
        load_corpus(path)
    raw = corpus.model_dump(mode="json")
    raw["sessions"][0]["frames"][1]["at_ms"] = 0
    with pytest.raises(ValueError):
        Corpus.model_validate(raw)
    raw = corpus.model_dump(mode="json")
    raw["sessions"][0]["frames"][0]["provider"]["elapsed_ms"] = float("nan")
    with pytest.raises(ValueError):
        Corpus.model_validate(raw)


def test_prompt_drift_is_not_hidden_by_recorded_answers(sample):
    corpus, digest, *_ = sample
    raw = corpus.model_dump(mode="json")
    raw["sessions"][0]["frames"][0]["provider"]["request_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="prompt changed"):
        asyncio.run(replay(Corpus.model_validate(raw), digest))


def test_cli_writes_reproducible_private_artifacts_and_refuses_overwrite(tmp_path, capsys):
    output = tmp_path / "report"
    args = [
        str(FIXTURES / "holdout.json"),
        "--gates",
        str(FIXTURES / "holdout-gates.json"),
        "--output",
        str(output),
    ]
    assert main(args) == 0
    report = json.loads((output / "report.json").read_text())
    trace = Trace.model_validate_json((output / "trace.json").read_bytes())
    assert (
        trace.corpus_sha256 == hashlib.sha256((FIXTURES / "holdout.json").read_bytes()).hexdigest()
    )
    assert report["passed"]
    assert "coffee" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
