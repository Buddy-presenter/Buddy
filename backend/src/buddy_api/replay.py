"""Permissioned, deterministic agent replay and scoring of captured policy executions.

No microphone, credentials, network, database writes or policy implementation are implicit.
Provider responses are replayed at the real HTTP adapter boundary, not as reference answers.
"""

import argparse
import asyncio
import hashlib
import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Self

import httpx
from pydantic import Field, model_validator

from buddy_api.agent_provider import AgentFailure, AgentSettings, CohereDecisionProvider
from buddy_api.analysis.contracts import AnalysisResult, Model, Region
from buddy_api.presentation_agent import (
    AgentInput,
    AgentRequest,
    Target,
    checked_selection,
    decision_passage,
    navigation_selection,
    options,
)
from buddy_api.presentation_candidates import Candidate, navigation_step, resolve

MAX_FILE_BYTES = 8 * 1024 * 1024
TAGS = {"hesitation", "digression", "return", "silence", "ambiguity", "asr_error"}


class Permission(Model):
    kind: Literal["synthetic", "consented"]
    reference: str = Field(min_length=1, max_length=256)
    expires: date | None = None

    @model_validator(mode="after")
    def consent_expiration(self) -> Self:
        if self.kind == "consented" and self.expires is None:
            raise ValueError("Consented recordings require an explicit retention deadline")
        return self


class ProviderReply(Model):
    status: int = Field(default=200, ge=100, le=599)
    body: dict[str, object]
    elapsed_ms: float = Field(ge=0, le=120000)
    input_tokens: int = Field(ge=0, le=1_000_000_000, strict=True)
    output_tokens: int = Field(ge=0, le=1_000_000_000, strict=True)
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Frame(Model):
    id: str = Field(min_length=1, max_length=128)
    at_ms: int = Field(ge=0, strict=True)
    current: str = Field(min_length=1, max_length=128)
    focus: Literal["slide", "zoom_region", "highlight_region"] = "slide"
    focus_region: Region | None = None
    request: AgentRequest | None
    # None runs the production lexical resolver; a tuple replays a frozen semantic shortlist.
    candidates: tuple[Candidate, ...] | None = Field(default=None, max_length=8)
    provider: ProviderReply | None
    expected: Target
    tags: frozenset[
        Literal[
            "hesitation", "digression", "return", "silence", "ambiguity", "asr_error", "security"
        ]
    ]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.request is None and (
            self.provider is not None or self.expected.tool != "no_action"
        ):
            raise ValueError("Silence has no provider request and cannot require a transition")
        if self.expected.tool == "no_action" and self.expected.slide_id != self.current:
            raise ValueError("No action must preserve the current slide")
        if (self.focus == "slide") != (self.focus_region is None):
            raise ValueError("Focused state requires its actual region")
        return self


class Session(Model):
    id: str = Field(min_length=1, max_length=128)
    duration_ms: int = Field(gt=0, le=86400000, strict=True)
    analysis: AnalysisResult
    frames: tuple[Frame, ...] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        times = [frame.at_ms for frame in self.frames]
        if times != sorted(set(times)) or times[-1] >= self.duration_ms:
            raise ValueError("Frames must be strictly ordered within session duration")
        if len({frame.id for frame in self.frames}) != len(self.frames):
            raise ValueError("Frame ids must be unique within a session")
        slides = {slide.slide_id for slide in self.analysis.slides}
        if not 1 <= len(slides) <= 500 or len(slides) != len(self.analysis.slides):
            raise ValueError("Saved slide map must contain 1-500 unique slides")
        if any(f.current not in slides or f.expected.slide_id not in slides for f in self.frames):
            raise ValueError("Frame state and reference targets must belong to the saved deck")
        return self


class Corpus(Model):
    protocol: Literal["presentation-replay.v1"] = "presentation-replay.v1"
    id: str = Field(min_length=1, max_length=128)
    split: Literal["development", "holdout"]
    permission: Permission
    sessions: tuple[Session, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_sessions(self) -> Self:
        if len({s.id for s in self.sessions}) != len(self.sessions):
            raise ValueError("Session ids must be unique")
        return self


class AppliedAction(Model):
    target: Target
    at_ms: int = Field(ge=0, strict=True)
    animated: bool


class Observation(Model):
    session_id: str
    frame_id: str
    proposed: Target
    allowed: tuple[Target, ...] = Field(min_length=1, max_length=10)
    reason: str = Field(min_length=1, max_length=64)
    elapsed_ms: float = Field(ge=0, le=120000)
    input_tokens: int = Field(ge=0, le=1_000_000_000, strict=True)
    output_tokens: int = Field(ge=0, le=1_000_000_000, strict=True)
    applied: tuple[AppliedAction, ...] = Field(default=(), max_length=100)


class Trace(Model):
    protocol: Literal["presentation-replay-trace.v1"] = "presentation-replay-trace.v1"
    corpus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: Literal["synthetic_contract", "captured_service"]
    policy_version: str | None = Field(default=None, min_length=1, max_length=128)
    observations: tuple[Observation, ...] = Field(max_length=100000)


class Gates(Model):
    protocol: Literal["presentation-replay-gates.v1"] = "presentation-replay-gates.v1"
    corpus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_accuracy_min: float = Field(ge=0, le=1)
    no_action_recall_min: float = Field(ge=0, le=1)
    no_action_precision_min: float = Field(ge=0, le=1)
    false_transitions_per_minute_max: float = Field(ge=0)
    latency_p95_ms_max: float = Field(ge=0)
    unnecessary_animations_max: int = Field(ge=0)
    cost_usd_max: float = Field(ge=0)
    input_usd_per_million: float = Field(ge=0, le=1_000_000)
    output_usd_per_million: float = Field(ge=0, le=1_000_000)
    pricing_reference: str = Field(min_length=1, max_length=256)


def read_document(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Replay file exceeds 8 MiB")
    return data


def load_corpus(path: Path) -> tuple[Corpus, str]:
    data = read_document(path)
    corpus = Corpus.model_validate_json(data)
    if (
        corpus.permission.expires is not None
        and corpus.permission.expires < datetime.now(UTC).date()
    ):
        raise ValueError("Corpus permission/retention has expired")
    return corpus, hashlib.sha256(data).hexdigest()


def frame_options(session: Session, frame: Frame) -> tuple[AgentInput, tuple[Candidate, ...]]:
    source = AgentInput(session.analysis, frame.current, frame.focus, "replay", frame.focus_region)
    candidates = frame.candidates
    if candidates is None:
        candidates = resolve(
            session.analysis,
            frame.current,
            frame.request.text if frame.request else "",
            frame.request.latest_utterance if frame.request else "",
        )
    return source, candidates


async def replay(corpus: Corpus, digest: str) -> Trace:
    observations = []
    for session in corpus.sessions:
        for frame in session.frames:
            source, candidates = frame_options(session, frame)
            targets, descriptions = options(source, candidates)
            target, reason = targets[0], "silence"
            elapsed, inputs, outputs = 0.0, 0, 0
            if frame.request is not None:
                values = frame.request
                if navigation_step(values.latest_utterance) is not None:
                    # Production fast path always uses the authoritative lexical neighbors.
                    candidates = resolve(
                        session.analysis, frame.current, values.text, values.latest_utterance
                    )
                    targets, _ = options(source, candidates)
                    selection = navigation_selection(targets, candidates, len(values.segments))
                    target = checked_selection(selection, targets, len(values.segments))
                    reason = selection.reason
                    if frame.provider is not None:
                        raise ValueError("Navigation fast path must not spend a provider response")
                else:
                    reply = frame.provider
                    if reply is None:
                        raise ValueError("Missing recorded provider response")
                    elapsed, inputs, outputs = (
                        reply.elapsed_ms,
                        reply.input_tokens,
                        reply.output_tokens,
                    )
                    request_digests: list[str] = []

                    def respond(
                        request: httpx.Request,
                        reply: ProviderReply = reply,
                        request_digests: list[str] = request_digests,
                    ) -> httpx.Response:
                        request_digests.append(hashlib.sha256(request.content).hexdigest())
                        return httpx.Response(reply.status, json=reply.body)

                    provider = CohereDecisionProvider(
                        AgentSettings("offline-replay-only"), transport=httpx.MockTransport(respond)
                    )
                    try:
                        selection = await provider.select(
                            decision_passage(source, values, descriptions)
                        )
                        target = checked_selection(selection, targets, len(values.segments))
                        reason = selection.reason
                    except AgentFailure as exc:
                        reason = exc.code
                    finally:
                        await provider.aclose()
                    if request_digests != [reply.request_sha256]:
                        raise ValueError("Provider prompt changed; frozen replay needs review")
            observations.append(
                Observation(
                    session_id=session.id,
                    frame_id=frame.id,
                    proposed=target,
                    allowed=targets,
                    reason=reason,
                    elapsed_ms=elapsed,
                    input_tokens=inputs,
                    output_tokens=outputs,
                )
            )
    return Trace(
        corpus_sha256=digest,
        provenance="synthetic_contract",
        observations=tuple(observations),
    )


def score(
    corpus: Corpus, digest: str, trace: Trace, gates: Gates, *, release: bool
) -> dict[str, object]:
    if trace.corpus_sha256 != digest or gates.corpus_sha256 != digest:
        raise ValueError("Corpus digest differs from frozen trace or gate manifest")
    frames = {(s.id, f.id): (s, f) for s in corpus.sessions for f in s.frames}
    observations = {(o.session_id, o.frame_id): o for o in trace.observations}
    if len(observations) != len(trace.observations) or frames.keys() != observations.keys():
        raise ValueError("Trace must contain each corpus frame exactly once")
    policy = trace.policy_version is not None
    correct = abstain_expected = abstain_actual = abstain_correct = false = animations = outside = 0
    latencies = []
    for key, observation in observations.items():
        session, frame = frames[key]
        source, candidates = frame_options(session, frame)
        if frame.request and navigation_step(frame.request.latest_utterance) is not None:
            candidates = resolve(
                session.analysis, frame.current, frame.request.text, frame.request.latest_utterance
            )
        targets, _ = options(source, candidates)
        if tuple(observation.allowed) != targets:
            raise ValueError("Trace candidate set differs from the frozen production candidates")
        outside += int(observation.proposed not in targets)
        if not policy and observation.applied:
            raise ValueError("Applied actions require an identified transition policy")
        if policy:
            actual = observation.applied[-1].target if observation.applied else targets[0]
            seen = set()
            next_frame = next(
                (f.at_ms for f in session.frames if f.at_ms > frame.at_ms), session.duration_ms
            )
            action_times = [action.at_ms for action in observation.applied]
            if action_times != sorted(action_times):
                raise ValueError("Applied actions must be in capture order")
            for action in observation.applied:
                if not frame.at_ms <= action.at_ms < next_frame:
                    raise ValueError("Applied action is outside its session/frame clock")
                outside += int(action.target not in targets)
                identity = action.target.model_dump_json()
                unnecessary = action.target != frame.expected or identity in seen
                false += int(unnecessary and action.target.tool != "no_action")
                animations += int(
                    action.animated and (unnecessary or action.target.tool == "no_action")
                )
                seen.add(identity)
            if observation.applied:
                latencies.append(float(observation.applied[-1].at_ms - frame.at_ms))
        else:
            actual = observation.proposed
            false += int(actual.tool != "no_action" and actual != frame.expected)
            if frame.request is not None:
                latencies.append(observation.elapsed_ms)
        correct += int(actual == frame.expected)
        abstain_expected += int(frame.expected.tool == "no_action")
        abstain_actual += int(actual.tool == "no_action")
        abstain_correct += int(actual.tool == frame.expected.tool == "no_action")
    total = len(frames)
    latencies.sort()
    cost = (
        sum(
            o.input_tokens * gates.input_usd_per_million
            + o.output_tokens * gates.output_usd_per_million
            for o in trace.observations
        )
        / 1_000_000
    )
    metrics = {
        "target_accuracy": correct / total,
        "no_action_recall": abstain_correct / abstain_expected if abstain_expected else None,
        "no_action_precision": abstain_correct / abstain_actual if abstain_actual else None,
        "false_transitions_per_minute": false
        / (sum(s.duration_ms for s in corpus.sessions) / 60000),
        "latency_p95_ms": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
        "unnecessary_animations": animations if policy else None,
        "cost_usd": cost,
        "outside_candidate_set": outside,
    }
    failures = []
    for name, threshold, minimum in (
        ("target_accuracy", gates.target_accuracy_min, True),
        ("no_action_recall", gates.no_action_recall_min, True),
        ("no_action_precision", gates.no_action_precision_min, True),
        ("false_transitions_per_minute", gates.false_transitions_per_minute_max, False),
        ("latency_p95_ms", gates.latency_p95_ms_max, False),
        ("cost_usd", gates.cost_usd_max, False),
    ):
        value = metrics[name]
        if value is None or (value < threshold if minimum else value > threshold):
            failures.append(name)
    if outside:
        failures.append("outside_candidate_set")
    if policy and animations > gates.unnecessary_animations_max:
        failures.append("unnecessary_animations")
    tags = {tag for s in corpus.sessions for f in s.frames for tag in f.tags}
    if not tags >= TAGS:
        failures.append("required_scenarios")
    if release and (
        not policy
        or trace.provenance != "captured_service"
        or corpus.permission.kind != "consented"
        or corpus.split != "holdout"
    ):
        failures.append("real_service_policy_and_permission_evidence_required")
    return {
        "protocol": "presentation-replay-report.v1",
        "corpus_sha256": digest,
        "split": corpus.split,
        "scope": "applied_policy" if policy else "agent_proposals",
        "policy_version": trace.policy_version,
        "latency_samples": len(latencies),
        "pricing_reference": gates.pricing_reference,
        "provenance": trace.provenance,
        "release_gate": release,
        "sessions": len(corpus.sessions),
        "frames": total,
        "metrics": metrics,
        "failures": failures,
        "passed": not failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--gates", required=True, type=Path)
    parser.add_argument("--trace", type=Path, help="Score a captured service/policy trace instead")
    parser.add_argument("--output", required=True, type=Path, help="New private report directory")
    parser.add_argument(
        "--release", action="store_true", help="Require real service/policy evidence"
    )
    args = parser.parse_args(argv)
    try:
        corpus, digest = load_corpus(args.corpus)
        gates = Gates.model_validate_json(read_document(args.gates))
        trace = (
            Trace.model_validate_json(read_document(args.trace))
            if args.trace
            else asyncio.run(replay(corpus, digest))
        )
        report = score(corpus, digest, trace, gates, release=args.release)
        args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        (args.output / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        (args.output / "trace.json").write_text(
            trace.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    except (ValueError, OSError):
        # Validation exceptions can contain private transcript text. Do not print them.
        parser.exit(2, "Replay input, permission, digest, trace or output directory is invalid.\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
