"""Permissioned, deterministic agent replay and scoring of captured policy executions.

No microphone, credentials, network, database writes or policy implementation are implicit.
Provider responses are replayed at the real HTTP adapter boundary, not as reference answers.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Self

import httpx
from pydantic import Field, model_validator

from buddy_api.agent_provider import (
    MODEL,
    AgentFailure,
    AgentSettings,
    CohereDecisionProvider,
    Selection,
)
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
    provider_processing: bool = False

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
    usage_complete: bool = True
    applied: tuple[AppliedAction, ...] = Field(default=(), max_length=100)


class Trace(Model):
    protocol: Literal["presentation-replay-trace.v1"] = "presentation-replay-trace.v1"
    corpus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: Literal["synthetic_contract", "captured_provider", "captured_service"]
    policy_version: str | None = Field(default=None, min_length=1, max_length=128)
    provider_model: str = Field(default=MODEL, min_length=1, max_length=128)
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


class UsageTransport(httpx.AsyncBaseTransport):
    """Bounded billing capture at the production HTTP adapter boundary, never prompt logging."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.inputs = self.outputs = 0
        self.complete = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 65536:
                    raise AgentFailure("invalid_output")
        finally:
            await response.aclose()
        try:
            result = json.loads(body)
            units = result["usage"]["billed_units"]
            inputs, outputs = units["input_tokens"], units["output_tokens"]
            if all(type(n) is int and 0 <= n <= 1_000_000_000 for n in (inputs, outputs)):
                self.inputs, self.outputs, self.complete = inputs, outputs, True
        except (ValueError, KeyError, TypeError, RecursionError):
            pass  # Missing billing is unknown, never a zero-cost success.
        return httpx.Response(response.status_code, content=bytes(body))

    async def aclose(self) -> None:
        await self.inner.aclose()


class LiveProvider:
    def __init__(self, key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = UsageTransport(
            transport if transport is not None else httpx.AsyncHTTPTransport(retries=0)
        )
        self.provider = CohereDecisionProvider(AgentSettings(key), transport=self.transport)
        self.elapsed_ms = 0.0

    async def select(self, passage: dict[str, object]) -> Selection:
        self.transport.inputs = self.transport.outputs = 0
        self.transport.complete = False
        started = time.monotonic()
        try:
            return await self.provider.select(passage)
        finally:
            self.elapsed_ms = (time.monotonic() - started) * 1000

    async def aclose(self) -> None:
        await self.provider.aclose()


async def replay(corpus: Corpus, digest: str, live: LiveProvider | None = None) -> Trace:
    observations = []
    for session in corpus.sessions:
        for frame in session.frames:
            source, candidates = frame_options(session, frame)
            targets, descriptions = options(source, candidates)
            target, reason = targets[0], "silence"
            elapsed, inputs, outputs = 0.0, 0, 0
            usage_complete = True
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
                    if live is not None:
                        try:
                            selection = await live.select(
                                decision_passage(source, values, descriptions)
                            )
                            target = checked_selection(selection, targets, len(values.segments))
                            reason = selection.reason
                        except AgentFailure as exc:
                            reason = exc.code
                        elapsed = live.elapsed_ms
                        inputs, outputs = live.transport.inputs, live.transport.outputs
                        usage_complete = live.transport.complete
                    else:
                        target, reason, elapsed, inputs, outputs = await frozen_selection(
                            source, values, targets, descriptions, frame.provider
                        )
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
                    usage_complete=usage_complete,
                )
            )
    return Trace(
        corpus_sha256=digest,
        provenance="captured_provider" if live is not None else "synthetic_contract",
        observations=tuple(observations),
    )


async def frozen_selection(
    source: AgentInput,
    values: AgentRequest,
    targets: tuple[Target, ...],
    descriptions: list[dict[str, object]],
    recorded: ProviderReply | None,
) -> tuple[Target, str, float, int, int]:
    target, reason = targets[0], "invalid_output"
    reply = recorded
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
        selection = await provider.select(decision_passage(source, values, descriptions))
        target = checked_selection(selection, targets, len(values.segments))
        reason = selection.reason
    except AgentFailure as exc:
        reason = exc.code
    finally:
        await provider.aclose()
    if request_digests != [reply.request_sha256]:
        raise ValueError("Provider prompt changed; frozen replay needs review")
    return target, reason, elapsed, inputs, outputs


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
                if action.at_ms < frame.at_ms + observation.elapsed_ms:
                    raise ValueError("Applied action cannot precede its completed decision")
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
        "usage_incomplete_observations": sum(not o.usage_complete for o in trace.observations),
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
    if metrics["usage_incomplete_observations"]:
        failures.append("billing_usage_unknown")
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
        "gates_sha256": hashlib.sha256(gates.model_dump_json().encode()).hexdigest(),
        "provider_model": trace.provider_model,
        "cost_scope": "decision_provider_billed_tokens",
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


async def run_live(corpus: Corpus, digest: str, max_calls: int) -> Trace:
    if not 1 <= max_calls <= 1000:
        raise ValueError("Live request limit must be between 1 and 1000")
    calls = sum(
        f.request is not None and navigation_step(f.request.latest_utterance) is None
        for s in corpus.sessions
        for f in s.frames
    )
    if calls > max_calls:
        raise ValueError("Corpus exceeds the explicit live request limit")
    if corpus.permission.kind == "consented" and not corpus.permission.provider_processing:
        raise ValueError("Recording permission does not allow cloud provider processing")
    key = os.environ.get("BUDDY_COHERE_API_KEY", "")
    if not key:
        raise ValueError("Missing backend-only provider credential")
    provider = LiveProvider(key)
    try:
        return await replay(corpus, digest, provider)
    finally:
        await provider.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--gates", required=True, type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--trace", type=Path, help="Score a captured service/policy trace instead")
    source.add_argument(
        "--live-provider", action="store_true", help="Opt in to paid provider calls"
    )
    parser.add_argument("--max-provider-calls", type=int, default=20)
    parser.add_argument("--output", required=True, type=Path, help="New private report directory")
    parser.add_argument(
        "--release", action="store_true", help="Require real service/policy evidence"
    )
    args = parser.parse_args(argv)
    try:
        if args.live_provider and args.release:
            raise ValueError("Provider-only evaluation cannot supply policy release evidence")
        corpus, digest = load_corpus(args.corpus)
        gates = Gates.model_validate_json(read_document(args.gates))
        if gates.corpus_sha256 != digest or args.output.exists():
            raise ValueError("Validate the frozen gates and new output path before paid work")
        args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        trace = (
            Trace.model_validate_json(read_document(args.trace))
            if args.trace
            else asyncio.run(run_live(corpus, digest, args.max_provider_calls))
            if args.live_provider
            else asyncio.run(replay(corpus, digest))
        )
        report = score(corpus, digest, trace, gates, release=args.release)
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
