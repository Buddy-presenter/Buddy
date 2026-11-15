"""Owner-bound constrained proposals. This runtime cannot execute or update session state."""

import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from buddy_api.agent_provider import (
    MODEL,
    AgentFailure,
    AgentSettings,
    DecisionProvider,
    FailureReason,
    Selection,
)
from buddy_api.analysis.contracts import AnalysisResult, Model, Region
from buddy_api.auth import Principal, authenticated
from buddy_api.embeddings import EmbeddingFailure
from buddy_api.persistence.database import Database
from buddy_api.persistence.errors import PersistenceError
from buddy_api.persistence.models import AgentQuota, Deck
from buddy_api.presentation_candidates import Candidate, CandidateRequest, navigation_step, resolve
from buddy_api.presentation_sessions import current_ready_analysis, owned_session, snapshot


class SpeechSegment(Model):
    sequence: int = Field(ge=1, strict=True)
    text: str = Field(min_length=1, max_length=4096, repr=False)


class AgentRequest(Model):
    protocol: Literal["presentation-agent.v1"] = "presentation-agent.v1"
    expected_state_version: int = Field(ge=0, strict=True)
    context_epoch: int = Field(ge=0, strict=True)
    stream_id: UUID
    sequence: int = Field(ge=1, strict=True)
    sent_at: datetime
    text: str = Field(min_length=1, max_length=4096, repr=False)
    latest_utterance: str = Field(min_length=1, max_length=4096, repr=False)
    segments: tuple[SpeechSegment, ...] = Field(min_length=1, max_length=64, repr=False)

    @model_validator(mode="after")
    def coherent(self) -> "AgentRequest":
        CandidateRequest.model_validate(
            {**self.model_dump(exclude={"segments"}), "protocol": "presentation-candidates.v1"}
        )
        if (
            len(self.text) > 4096
            or self.text != " ".join(s.text for s in self.segments)
            or self.latest_utterance != self.segments[-1].text
            or self.sequence != max(s.sequence for s in self.segments)
            or any(not s.text.strip() for s in self.segments)
            or len({s.sequence for s in self.segments}) != len(self.segments)
        ):
            raise ValueError("Inconsistent completed speech")
        return self


class Target(Model):
    tool: Literal[
        "no_action",
        "next_slide",
        "previous_slide",
        "go_to_slide",
        "zoom_region",
        "highlight_region",
        "reset_view",
    ]
    slide_id: str = Field(min_length=1, max_length=128)
    action_id: str | None = None
    region: Region | None = None


type AgentReason = (
    Literal["explicit", "topic", "correction", "stay", "uncertain", "disabled"] | FailureReason
)


class AgentProposal(Model):
    protocol: Literal["presentation-agent.v1"] = "presentation-agent.v1"
    decision_id: UUID
    session_id: UUID
    analysis_id: UUID
    state_version: int
    context_epoch: int
    stream_id: UUID
    sequence: int
    input_sha256: str
    target: Target
    decision_source: Literal["model", "navigation", "fallback"]
    reason: AgentReason
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: tuple[int, ...] = ()
    elapsed_ms: int = Field(ge=0)


@dataclass(frozen=True)
class AgentInput:
    analysis: AnalysisResult
    current: str
    focus: str
    identity: str


async def read_input(
    request: Request, principal: Principal, identifier: UUID, values: AgentRequest
) -> AgentInput:
    """A short authoritative transaction; no provider call while it is open."""
    async with request.app.state.database.sessions() as db, db.begin():
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        session = await owned_session(db, principal, identifier)
        if session.status != "active" or session.state_version != values.expected_state_version:
            raise HTTPException(409, "Session changed; wait for fresh speech.")
        deck = await db.get(Deck, session.deck_id)
        if deck is None:
            raise HTTPException(404)
        record, _ = await current_ready_analysis(db, deck, principal.id)
        if record.id != session.analysis_version_id:
            raise HTTPException(409, "Analysis changed; reconnect.")
        analysis = await request.app.state.candidate_analyses.read(db, record, principal.id)
        # Include all validation eligibility/geometry, even if the analysis id is unchanged.
        identity = hashlib.sha256(
            (
                snapshot(session).model_dump_json()
                + analysis.model_dump_json()
                + str(deck.source_revision)
                + str(deck.source_sha256)
                + str(deck.source_version_id)
            ).encode()
        ).hexdigest()
        return AgentInput(analysis, session.active_slide_id or "", session.focus_view, identity)


def options(
    source: AgentInput, candidates: tuple[Candidate, ...]
) -> tuple[tuple[Target, ...], list[dict[str, object]]]:
    slides = list(source.analysis.slides)
    positions = {s.slide_id: i for i, s in enumerate(slides)}
    if source.current not in positions:
        raise AgentFailure("candidates_unavailable")
    targets = [Target(tool="no_action", slide_id=source.current)]
    descriptions: list[dict[str, object]] = [
        {"id": "c0", "tool": "no_action", "content": "Keep the current view"}
    ]
    for candidate in candidates:
        slide = next((s for s in slides if s.slide_id == candidate.slide_id), None)
        if slide is None or candidate.kind == "no_change":
            continue
        content = (slide.summary.text or "")[:1200]
        if candidate.kind == "slide":
            offset = positions[slide.slide_id] - positions[source.current]
            if offset == 0:
                if source.focus == "slide":
                    continue
                target = Target(tool="reset_view", slide_id=slide.slide_id)
            else:
                target = Target(
                    tool="next_slide"
                    if offset == 1
                    else "previous_slide"
                    if offset == -1
                    else "go_to_slide",
                    slide_id=slide.slide_id,
                )
        else:
            action = next(
                (
                    a
                    for a in slide.actions
                    if a.action.instance_id == candidate.action_id
                    and a.usable
                    and a.state == "validated"
                    and a.semantic_verified
                    and a.action.type == candidate.kind
                ),
                None,
            )
            if action is None:
                continue
            target = Target(
                tool=action.action.type,
                slide_id=slide.slide_id,
                action_id=action.action.instance_id,
                region=action.action.params.region,
            )
            content = action.action.purpose[:512]
        if target in targets:
            continue
        descriptions.append(
            {
                "id": f"c{len(targets)}",
                "tool": target.tool,
                "slide_number": positions[slide.slide_id] + 1,
                "content": content,
            }
        )
        targets.append(target)
    return tuple(targets[:8]), descriptions[:8]


def checked_selection(
    selection: Selection, targets: tuple[Target, ...], segment_count: int
) -> Target:
    index = int(selection.choice[1:])
    if (
        index >= len(targets)
        or any(type(i) is not int or not 0 <= i < segment_count for i in selection.evidence)
        or len(set(selection.evidence)) != len(selection.evidence)
        or (index != 0 and (not selection.evidence or selection.reason in {"stay", "uncertain"}))
    ):
        raise AgentFailure("invalid_output")
    return targets[index]


class AgentRuntime:
    def __init__(self, settings: AgentSettings, provider: DecisionProvider):
        self.settings, self.provider = settings, provider
        self.active: set[UUID] = set()

    async def reserve(self, database: Database) -> None:
        # DB clock + row lock share pacing and conservative attempts across replicas/restarts.
        key = hashlib.sha256(self.settings.api_key.encode()).hexdigest()
        async with database.sessions() as db, db.begin():
            await db.execute(
                insert(AgentQuota)
                .values(key_hash=key, month="", calls=0)
                .on_conflict_do_nothing(index_elements=[AgentQuota.key_hash])
            )
            row = await db.scalar(
                select(AgentQuota).where(AgentQuota.key_hash == key).with_for_update()
            )
            assert row is not None
            now = await db.scalar(select(func.clock_timestamp()))
            assert now is not None
            month = now.strftime("%Y-%m")
            if row.last_called_at is not None and (now - row.last_called_at).total_seconds() < 3.2:
                raise AgentFailure("paced")
            if row.month != month:
                row.month, row.calls = month, 0
            if row.calls >= self.settings.monthly_limit:
                raise AgentFailure("budget")
            row.calls += 1
            row.last_called_at = now


def agent_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/sessions", tags=["presentation agent"])
    input_schema = AgentRequest.model_json_schema()
    # Manual bounded body receipt must still publish resolvable OpenAPI schemas.
    input_schema.pop("$defs", None)
    input_schema["properties"]["segments"]["items"] = SpeechSegment.model_json_schema()

    @router.post(
        "/{identifier}/agent",
        response_model=AgentProposal,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": input_schema}},
            }
        },
    )
    async def propose(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> AgentProposal:
        response.headers["Cache-Control"] = "no-store"
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise HTTPException(413, "Agent input is too large.")
        except TimeoutError:
            raise HTTPException(408, "Agent input timed out.") from None
        try:
            values = AgentRequest.model_validate_json(bytes(body))
        except (ValidationError, ValueError):
            raise HTTPException(422, "Invalid completed speech context.") from None
        if not -2 <= (datetime.now(UTC) - values.sent_at).total_seconds() <= 5:
            raise HTTPException(409, "Speech context expired.")
        started = time.monotonic()
        runtime: AgentRuntime | None = request.app.state.presentation_agent
        admitted = False
        try:
            async with asyncio.timeout(4.5):
                source = await read_input(request, principal, identifier, values)
                target = Target(tool="no_action", slide_id=source.current)
                reason: AgentReason = "disabled"
                decision_source: Literal["model", "navigation", "fallback"] = "fallback"
                selection = None
                if runtime is not None:
                    if principal.id in runtime.active or len(runtime.active) >= 16:
                        raise AgentFailure("busy")
                    runtime.active.add(principal.id)
                    admitted = True
                    step = navigation_step(values.latest_utterance)
                    if step is not None:
                        decision_source = "navigation"
                        candidates = resolve(
                            source.analysis, source.current, values.text, values.latest_utterance
                        )
                        targets, _ = options(source, candidates)
                        neighbor = next(
                            (c.slide_id for c in candidates if c.reason == "explicit_navigation"),
                            None,
                        )
                        choice = next(
                            (
                                i
                                for i, t in enumerate(targets)
                                if t.slide_id == neighbor
                                and t.tool in {"next_slide", "previous_slide"}
                            ),
                            0,
                        )
                        selection = Selection.model_validate(
                            {
                                "choice": f"c{choice}",
                                "reason": "explicit" if choice else "stay",
                                "confidence": 1.0,
                                "evidence": [len(values.segments) - 1],
                            }
                        )
                    else:
                        # Reserve first: exhausted/fast clients cannot spend query quota.
                        await runtime.reserve(request.app.state.database)
                        semantic = request.app.state.semantic_candidates
                        candidates = (
                            await semantic.retrieve(
                                source.analysis,
                                principal.id,
                                source.current,
                                values.latest_utterance or values.text,
                                values.text,
                            )
                            if semantic is not None
                            else resolve(
                                source.analysis,
                                source.current,
                                values.text,
                                values.latest_utterance,
                            )
                        )
                        targets, descriptions = options(source, candidates)
                        current_slide = next(
                            s for s in source.analysis.slides if s.slide_id == source.current
                        )
                        selection = await runtime.provider.select(
                            {
                                "segments": [s.text for s in values.segments],
                                "latest_segment": len(values.segments) - 1,
                                "current_view": {
                                    "slide": source.current,
                                    "focus": source.focus,
                                    "summary": (current_slide.summary.text or "")[:1200],
                                },
                                "options": descriptions,
                            }
                        )
                        decision_source = "model"
                    target = checked_selection(selection, targets, len(values.segments))
                    reason = (
                        "stay"
                        if target.tool == "no_action" and selection.reason != "uncertain"
                        else selection.reason
                    )
                    if (datetime.now(UTC) - values.sent_at).total_seconds() > 5:
                        raise AgentFailure("stale")
                    try:
                        fresh = await read_input(request, principal, identifier, values)
                    except HTTPException as exc:
                        if exc.status_code == 409:
                            raise AgentFailure("stale") from None
                        raise
                    if fresh.identity != source.identity:
                        raise AgentFailure("stale")
        except HTTPException:
            # Ownership/auth failures must remain denied; stale session failures never propose.
            raise
        except (AgentFailure, EmbeddingFailure) as exc:
            reason = exc.code if isinstance(exc, AgentFailure) else "candidates_unavailable"
            target = Target(tool="no_action", slide_id=source.current)
            selection = None
            decision_source = "fallback"
        except TimeoutError:
            if "source" not in locals():
                raise HTTPException(503, "Session unavailable.") from None
            reason, target, selection = (
                "deadline",
                Target(tool="no_action", slide_id=source.current),
                None,
            )
            decision_source = "fallback"
        except (PersistenceError, SQLAlchemyError, OSError):
            raise HTTPException(503, "Agent temporarily unavailable.") from None
        finally:
            if admitted and runtime is not None:
                runtime.active.discard(principal.id)
        proposal = AgentProposal(
            decision_id=uuid4(),
            session_id=identifier,
            analysis_id=source.analysis.id,
            state_version=values.expected_state_version,
            context_epoch=values.context_epoch,
            stream_id=values.stream_id,
            sequence=values.sequence,
            input_sha256=hashlib.sha256(values.model_dump_json().encode()).hexdigest(),
            target=target,
            decision_source=decision_source,
            reason=reason,
            confidence=selection.confidence if selection else None,
            evidence=selection.evidence if selection else (),
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        # Raw structured event, like existing EMF events: independent of Uvicorn's
        # warning-only logger. Only locally typed finite fields reach this boundary.
        trace = {
            "event": "presentation_agent_decision",
            "decision_id": str(proposal.decision_id),
            "request_id": request.state.request_id,
            "tool": proposal.target.tool,
            "reason": proposal.reason,
            "elapsed_ms": proposal.elapsed_ms,
            "model": MODEL if runtime else "disabled",
            "decision_source": proposal.decision_source,
        }
        try:
            sys.stdout.write(json.dumps(trace, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except OSError:
            pass  # Telemetry cannot fail a presentation request.
        return proposal

    return router
