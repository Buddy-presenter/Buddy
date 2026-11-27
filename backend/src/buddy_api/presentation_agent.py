"""Owner-bound constrained proposals. This runtime cannot execute or update session state."""

import asyncio
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from buddy_api.agent_provider import (
    DECIDED_BY,
    DECISION_TRACE,
    AgentFailure,
    AgentSettings,
    DecisionProvider,
    FailureReason,
    Selection,
    record_decision_trace,
)
from buddy_api.analysis.contracts import AnalysisResult, Model, PowerPointAnalysisResult, Region
from buddy_api.auth import Principal, authenticated
from buddy_api.embeddings import EmbeddingFailure
from buddy_api.persistence.database import Database
from buddy_api.persistence.errors import PersistenceError
from buddy_api.persistence.models import AgentQuota, Deck
from buddy_api.presentation_candidates import (
    Candidate,
    CandidateRequest,
    candidate_action_eligible,
    candidate_slides,
    confident_topic_destination,
    navigation_intent,
    resolve,
    shown_text,
    topic_navigation_intent,
)
from buddy_api.presentation_sessions import FocusRegion, current_ready_analysis, owned_session


class SpeechSegment(Model):
    sequence: int = Field(ge=1, strict=True)
    text: str = Field(min_length=1, max_length=4096, repr=False)
    final: bool = True


class AgentRequest(Model):
    protocol: Literal["presentation-agent.v1"] = "presentation-agent.v1"
    expected_state_version: int = Field(ge=0, strict=True)
    # New desktop clients send their local slide; older clients keep the persisted
    # session snapshot contract until they are upgraded.
    active_slide_id: str | None = Field(default=None, min_length=1, max_length=128)
    focus_view: Literal["slide", "zoom_region", "highlight_region"] | None = None
    focus_region: FocusRegion | None = None
    context_epoch: int = Field(ge=0, strict=True)
    stream_id: UUID
    sequence: int = Field(ge=1, strict=True)
    sent_at: datetime
    text: str = Field(min_length=1, max_length=4096, repr=False)
    latest_utterance: str = Field(min_length=1, max_length=4096, repr=False)
    segments: tuple[SpeechSegment, ...] = Field(min_length=1, max_length=64, repr=False)
    provisional: bool = False
    # The presenter's choice in the desktop app (#245); None keeps the server default.
    decision_model: Literal["jev", "cohere"] | None = None
    # Desktop clients with the search panel let Jev also decide "search the web".
    web_search: bool = False
    # Buddy's search panel is open: Jev may also close or scroll it.
    search_panel_open: bool = False

    @model_validator(mode="after")
    def coherent(self) -> "AgentRequest":
        CandidateRequest.model_validate(
            {
                **self.model_dump(
                    exclude={
                        "segments",
                        "provisional",
                        "focus_view",
                        "focus_region",
                        "decision_model",
                        "web_search",
                        "search_panel_open",
                    }
                ),
                "protocol": "presentation-candidates.v1",
            }
        )
        if self.focus_view is None and self.focus_region is not None:
            raise ValueError("A local focus region requires a focus view")
        if self.focus_view is not None and (
            (self.focus_view == "slide") != (self.focus_region is None)
        ):
            raise ValueError("Local focus view and region are inconsistent")
        if (
            len(self.text) > 4096
            or self.text != " ".join(s.text for s in self.segments)
            or self.latest_utterance != self.segments[-1].text
            or self.sequence != max(s.sequence for s in self.segments)
            or any(not s.text.strip() for s in self.segments)
            or len({s.sequence for s in self.segments}) != len(self.segments)
            or any(not s.final for s in self.segments[:-1])
            or self.provisional == self.segments[-1].final
        ):
            raise ValueError("Inconsistent speech evidence")
        return self


type AgentConflictCode = Literal[
    "speech_context_expired",
    "session_inactive",
    "session_state_changed",
    "analysis_changed",
    "analysis_unavailable",
]


class AgentConflict(HTTPException):
    """A safe, diagnosable conflict while preparing a live suggestion."""

    def __init__(self, code: AgentConflictCode, message: str):
        super().__init__(status_code=409, detail=message)
        self.code = code
        self.public_message = message


def agent_conflict(
    request: Request,
    code: AgentConflictCode,
    message: str,
    **safe_context: str | int,
) -> AgentConflict:
    """Emit a content-free correlation event and return a typed conflict."""
    event = {
        "event": "presentation_agent_rejected",
        "request_id": request.state.request_id,
        "reason": code,
        **safe_context,
    }
    try:
        sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except OSError:
        pass  # Diagnostics must never change the request outcome.
    return AgentConflict(code, message)


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


class TransitionCheck(Model):
    expected_state_version: int = Field(ge=0, strict=True)
    analysis_id: UUID
    active_slide_id: str | None = Field(default=None, min_length=1, max_length=128)
    focus_view: Literal["slide", "zoom_region", "highlight_region"] | None = None
    focus_region: FocusRegion | None = None
    target: Target

    @model_validator(mode="after")
    def coherent_focus(self) -> "TransitionCheck":
        if self.focus_view is None and self.focus_region is not None:
            raise ValueError("A local focus region requires a focus view")
        if self.focus_view is not None and (
            (self.focus_view == "slide") != (self.focus_region is None)
        ):
            raise ValueError("Local focus view and region are inconsistent")
        return self


class TransitionApproval(Model):
    session_id: UUID
    analysis_id: UUID
    state_version: int
    target: Target


async def transition_input(request: Request) -> TransitionCheck:
    body = bytearray()
    try:
        async with asyncio.timeout(3):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    raise HTTPException(413, "Transition input is too large.")
    except TimeoutError:
        raise HTTPException(408, "Transition input timed out.") from None
    try:
        return TransitionCheck.model_validate_json(bytes(body))
    except (ValueError, ValidationError):
        raise HTTPException(422, "Invalid transition input.") from None


type AgentReason = (
    Literal["explicit", "topic", "correction", "stay", "uncertain", "disabled"] | FailureReason
)


class AgentProposal(Model):
    protocol: Literal["presentation-agent.v1"] = "presentation-agent.v1"
    decision_id: UUID
    session_id: UUID
    analysis_id: UUID
    state_version: int
    active_slide_id: str | None = Field(default=None, min_length=1, max_length=128)
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
    retry_after_ms: int | None = Field(default=None, ge=1, le=5000)
    # Jev decided the presenter asked Buddy to look something up. The target is
    # then "no_action"; the desktop writes the search from the spoken sentence.
    search: Literal["web", "images"] | None = None
    # Jev decided the presenter wants the open search panel closed or scrolled.
    panel: Literal["close", "scroll_down", "scroll_up"] | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> "AgentProposal":
        if self.decision_source == "fallback" and (
            self.target.tool != "no_action" or self.confidence is not None or self.evidence
        ):
            raise ValueError("Fallback cannot propose an action or provider evidence")
        if (self.reason == "paced") != (self.retry_after_ms is not None):
            raise ValueError("Only unspent paced admission carries a retry wait")
        if self.decision_source == "navigation" and self.target.tool not in {
            "no_action",
            "next_slide",
            "previous_slide",
            "go_to_slide",
        }:
            raise ValueError("Deterministic navigation cannot select another tool")
        if (self.search is not None or self.panel is not None) and (
            self.decision_source != "model" or self.target.tool != "no_action"
        ):
            raise ValueError("Only a model decision without a slide action can search")
        if self.search is not None and self.panel is not None:
            raise ValueError("A decision either searches or acts on the panel")
        return self


@dataclass(frozen=True)
class AgentInput:
    analysis: AnalysisResult | PowerPointAnalysisResult
    current: str
    focus: str
    identity: str
    focus_region: Region | None = None


def request_focus(
    *,
    persisted_slide_id: str | None,
    persisted_focus_view: str,
    persisted_focus_region: object,
    local_slide_id: str,
    local_focus_view: str | None,
    local_focus_region: FocusRegion | None,
) -> tuple[str, Region | None]:
    """Bind focus to the request's actual local slide, never a prior slide's focus."""
    if local_focus_view is not None:
        return (
            local_focus_view,
            Region.model_validate(local_focus_region.model_dump())
            if local_focus_region is not None
            else None,
        )
    if local_slide_id != persisted_slide_id:
        return "slide", None
    return (
        persisted_focus_view,
        Region.model_validate(persisted_focus_region)
        if persisted_focus_region is not None
        else None,
    )


async def read_input(
    request: Request, principal: Principal, identifier: UUID, values: AgentRequest
) -> AgentInput:
    """A short authoritative transaction; no provider call while it is open."""
    async with request.app.state.database.sessions() as db, db.begin():
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        session = await owned_session(db, principal, identifier)
        if session.status != "active":
            raise agent_conflict(
                request,
                "session_inactive",
                "Presentation session is no longer active.",
            )
        if (
            values.active_slide_id is None
            and session.state_version != values.expected_state_version
        ):
            raise agent_conflict(
                request,
                "session_state_changed",
                "Presentation changed; wait for session sync, then speak again.",
                expected_state_version=values.expected_state_version,
                current_state_version=session.state_version,
            )
        deck = await db.get(Deck, session.deck_id)
        if deck is None:
            raise HTTPException(404)
        try:
            record, _ = await current_ready_analysis(db, deck, principal.id)
        except HTTPException as exc:
            if exc.status_code == 409:
                raise agent_conflict(
                    request,
                    "analysis_unavailable",
                    "The saved analysis is unavailable for live suggestions.",
                ) from None
            raise
        if record.id != session.analysis_version_id:
            raise agent_conflict(
                request,
                "analysis_changed",
                "The saved analysis changed; reopen the presentation.",
            )
        try:
            analysis = await request.app.state.candidate_analyses.read(
                db, record, principal.id, session.runtime_context
            )
        except HTTPException as exc:
            if exc.status_code == 409:
                raise agent_conflict(
                    request,
                    "analysis_unavailable",
                    "The saved analysis is unavailable for live suggestions.",
                ) from None
            raise
        # Include all validation eligibility/geometry, even if the analysis id is unchanged.
        identity = hashlib.sha256(
            (
                f"{session.id}:{session.status}:{session.deck_id}:{session.analysis_version_id}:"
                + analysis.model_dump_json()
                + str(deck.source_revision)
                + str(deck.source_sha256)
                + str(deck.source_version_id)
            ).encode()
        ).hexdigest()
        active_slide_id = values.active_slide_id or session.active_slide_id or ""
        if active_slide_id not in {slide.slide_id for slide in analysis.slides}:
            raise HTTPException(422, "The active slide is not part of this analysis.")
        focus_view, focus_region = request_focus(
            persisted_slide_id=session.active_slide_id,
            persisted_focus_view=session.focus_view,
            persisted_focus_region=session.focus_region,
            local_slide_id=active_slide_id,
            local_focus_view=values.focus_view,
            local_focus_region=values.focus_region,
        )
        return AgentInput(
            analysis,
            active_slide_id,
            focus_view,
            identity,
            focus_region,
        )


def options(
    source: AgentInput, candidates: tuple[Candidate, ...], *, max_options: int = 10
) -> tuple[tuple[Target, ...], list[dict[str, object]]]:
    slides = list(candidate_slides(source.analysis))
    positions = {s.slide_id: i for i, s in enumerate(slides)}
    if source.current not in positions:
        raise AgentFailure("candidates_unavailable")
    targets = [Target(tool="no_action", slide_id=source.current)]
    descriptions: list[dict[str, object]] = [
        {
            "id": "c0",
            "tool": "no_action",
            "content": "Keep the current view",
            "is_current_view": True,
        }
    ]
    for candidate in candidates:
        slide = next((s for s in slides if s.slide_id == candidate.slide_id), None)
        if slide is None or candidate.kind == "no_change":
            continue
        # Keep the choice request focused: the selector needs a discriminating
        # slide excerpt, not another full summary for every retrieved candidate.
        content = (slide.summary.text or "")[:600]
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
            if slide.slide_id != source.current:
                # The desktop zooms or highlights only the slide on screen, so an action on
                # another slide could never run (#187). That slide stays reachable through
                # its navigation option; its actions are offered once it is shown.
                continue
            action = next(
                (
                    a
                    for a in slide.actions
                    if a.action.instance_id == candidate.action_id
                    and candidate_action_eligible(a)
                    and a.action.type == candidate.kind
                ),
                None,
            )
            if action is None:
                continue
            target = Target(
                tool=candidate.kind,
                slide_id=slide.slide_id,
                action_id=action.action.instance_id,
                region=action.action.params.region,
            )
            # Show the selector the slide text the action targets, so speech that
            # names that content ("$48 million") visibly matches this option.
            shown = shown_text(source.analysis, action)
            content = (
                f"Slide context: {(slide.summary.text or '')[:800]}\n"
                f"Action purpose: {action.action.purpose[:512]}"
            )
            if shown:
                content += f"\nShows: {shown[:256]}"
        if target in targets:
            continue
        descriptions.append(
            {
                "id": f"c{len(targets)}",
                "tool": target.tool,
                "slide_number": positions[slide.slide_id] + 1,
                "content": content,
                "is_current_view": target.slide_id == source.current
                and target.tool == source.focus
                and target.region == source.focus_region,
            }
        )
        targets.append(target)
    # Natural or multilingual navigation may not resemble any slide summary.
    # Preserve all retrieved alternatives and add the two authoritative neighbors,
    # rather than requiring English command spellings or evicting semantic hits.
    for offset, tool in ((-1, "previous_slide"), (1, "next_slide")):
        position = positions[source.current] + offset
        if not 0 <= position < len(slides):
            continue
        neighbor = slides[position]
        target = Target.model_validate({"tool": tool, "slide_id": neighbor.slide_id})
        if target in targets:
            continue
        descriptions.append(
            {
                "id": f"c{len(targets)}",
                "tool": tool,
                "slide_number": position + 1,
                "content": (neighbor.summary.text or "")[:1200],
                "is_current_view": False,
            }
        )
        targets.append(target)
    if len(targets) > max_options or len(targets) > 255:
        raise AgentFailure("candidates_unavailable")
    return tuple(targets), descriptions


# Web search (2) and search-panel (3) options Jev may also receive with the slide menu.
JEV_EXTRA_OPTIONS = 5


def direct_current_options(
    source: AgentInput,
) -> tuple[tuple[Candidate, ...], tuple[Target, ...], list[dict[str, object]]]:
    """Prototype: every eligible current-slide action and every slide, without retrieval.

    Reuse the same descriptions, exact saved geometry, and server-owned target mapping.
    Fail closed when the complete menu exceeds Jev's limit; never silently truncate it.
    """
    slides = tuple(candidate_slides(source.analysis))
    actions = tuple(
        action
        for slide in slides
        if slide.slide_id == source.current
        for action in slide.actions
        if candidate_action_eligible(action)
    )
    # Bound the complete menu before the existing option builder performs lookups.
    # One no-action + every other slide + eligible actions + an optional reset, plus up to
    # five web-search and search-panel options added for Jev: never above its 255 choices.
    if len(slides) + len(actions) + (source.focus != "slide") + JEV_EXTRA_OPTIONS > 255:
        raise AgentFailure("candidates_unavailable")
    candidates = tuple(
        Candidate(kind="slide", slide_id=slide.slide_id, reason="topic", score=0)
        for slide in slides
    ) + tuple(
        Candidate(
            kind=cast(Literal["zoom_region", "highlight_region"], action.action.type),
            slide_id=source.current,
            action_id=action.action.instance_id,
            reason="purpose",
            score=0,
        )
        for action in actions
    )
    targets, descriptions = options(source, candidates, max_options=255)
    return candidates, targets, descriptions


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


def decision_passage(
    source: AgentInput, values: AgentRequest, descriptions: list[dict[str, object]]
) -> dict[str, object]:
    """The production provider input, also used by permissioned offline replay."""
    current_slide = next(
        s for s in candidate_slides(source.analysis) if s.slide_id == source.current
    )
    passage: dict[str, object] = {
        "segments": [s.text for s in values.segments],
        "latest_segment": len(values.segments) - 1,
        "current_view": {
            "slide": source.current,
            "slide_number": next(
                i + 1
                for i, s in enumerate(candidate_slides(source.analysis))
                if s.slide_id == source.current
            ),
            "slide_count": len(source.analysis.slides),
            "focus": source.focus,
            "summary": (current_slide.summary.text or "")[:600],
            "focused_purpose": next(
                (
                    a.action.purpose
                    for a in current_slide.actions
                    if candidate_action_eligible(a)
                    and a.action.type == source.focus
                    and a.action.params.region == source.focus_region
                ),
                None,
            ),
        },
        "options": descriptions,
    }
    if getattr(values, "web_search", False):
        passage["web_search"] = True
        if getattr(values, "search_panel_open", False):
            passage["search_panel_open"] = True
    if values.provisional:
        passage["latest_segment_status"] = "provisional stable prefix; speech is ongoing"
    return passage


def navigation_selection(
    targets: tuple[Target, ...], candidates: tuple[Candidate, ...], segment_count: int
) -> Selection:
    destination = next((c.slide_id for c in candidates if c.reason == "explicit_navigation"), None)
    choice = next(
        (
            i
            for i, target in enumerate(targets)
            if target.slide_id == destination
            and target.tool in {"next_slide", "previous_slide", "go_to_slide"}
        ),
        0,
    )
    return Selection.model_validate(
        {
            "choice": f"c{choice}",
            "reason": "explicit" if choice else "stay",
            "confidence": 1.0,
            "evidence": [segment_count - 1],
        }
    )


def topic_navigation_selection(
    targets: tuple[Target, ...],
    candidates: tuple[Candidate, ...],
    segment_count: int,
    *,
    semantic: bool,
) -> Selection | None:
    destination = confident_topic_destination(candidates, semantic=semantic)
    if destination is None:
        return None
    choice = next(
        (
            index
            for index, target in enumerate(targets)
            if target.slide_id == destination
            and target.tool in {"next_slide", "previous_slide", "go_to_slide"}
        ),
        None,
    )
    if choice is None:
        return None
    return Selection.model_validate(
        {
            "choice": f"c{choice}",
            "reason": "topic",
            "confidence": 1.0,
            "evidence": [segment_count - 1],
        }
    )


class AgentRuntime:
    def __init__(
        self,
        settings: AgentSettings,
        provider: DecisionProvider,
        choices: dict[str, DecisionProvider] | None = None,
    ):
        self.settings, self.provider = settings, provider
        # Deciders a presenter may pick in the desktop app (#245); others use the default.
        self.choices = dict(choices or {})
        self.active: set[UUID] = set()

    def provider_for(self, choice: str | None) -> DecisionProvider:
        # Jev-only mode has no other decider: an older app that still asks for Cohere
        # simply gets Jev.
        return self.choices.get(choice, self.provider) if choice else self.provider

    async def aclose(self) -> None:
        unique = {id(p): p for p in (*self.choices.values(), self.provider)}
        for provider in unique.values():
            await provider.aclose()

    async def reserve(self, database: Database) -> None:
        # DB clock + row lock share a rolling provider window across replicas/restarts.
        key = self.quota_key()
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
            if row.blocked_until is not None and row.blocked_until > now:
                remaining = (row.blocked_until - now).total_seconds()
                raise AgentFailure("paced", min(5000, math.ceil(remaining * 1000)))
            month = now.strftime("%Y-%m")
            cutoff = now.timestamp() - 60
            times = sorted(t for t in row.request_times if t > cutoff)
            if len(times) >= self.settings.requests_per_minute:
                # If an operator lowered the rate, enough older attempts must
                # expire before the next request, rather than just the first.
                remaining = times[-self.settings.requests_per_minute] + 60 - now.timestamp()
                raise AgentFailure("paced", min(5000, max(1, math.ceil(remaining * 1000) + 50)))
            if row.month != month:
                row.month, row.calls = month, 0
            if self.settings.monthly_limit is not None and row.calls >= self.settings.monthly_limit:
                raise AgentFailure("budget")
            row.calls += 1
            row.last_called_at = now
            row.request_times = [*times, now.timestamp()]

    async def provider_limited(self, database: Database) -> None:
        # Other users of the credential and provider-specific windows are outside
        # our counter. A real 429 wins; do not hammer the service on new finals.
        key = self.quota_key()
        async with database.sessions() as db, db.begin():
            row = await db.scalar(
                select(AgentQuota).where(AgentQuota.key_hash == key).with_for_update()
            )
            assert row is not None  # Every model request first reserves an attempt.
            now = await db.scalar(select(func.clock_timestamp()))
            assert now is not None
            row.blocked_until = now + timedelta(seconds=60)

    def quota_key(self) -> str:
        credential = (
            self.settings.jev_api_key
            if self.settings.candidate_mode == "direct_current"
            else self.settings.api_key
        )
        assert credential
        return hashlib.sha256(credential.encode()).hexdigest()


def agent_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/sessions", tags=["presentation agent"])

    transition_schema = TransitionCheck.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    # Target and Region components are registered by the typed approval response.
    transition_schema.pop("$defs", None)

    @router.post(
        "/{identifier}/transition-check",
        response_model=TransitionApproval,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": transition_schema}},
            }
        },
    )
    async def transition_check(
        identifier: UUID,
        values: Annotated[TransitionCheck, Depends(transition_input)],
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> TransitionApproval:
        """Recheck eligibility immediately before local policy execution; no model call.

        This is not a policy grant or evidence of player completion. The presenter
        still owns manual control and must fence its capture/source at dispatch.
        """
        response.headers["Cache-Control"] = "no-store"
        try:
            async with asyncio.timeout(3), request.app.state.database.sessions() as db, db.begin():
                await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                session = await owned_session(db, principal, identifier)
                if (
                    session.status != "active"
                    or session.autonomy_mode != "autonomous"
                    or (
                        values.active_slide_id is None
                        and session.state_version != values.expected_state_version
                    )
                    or session.analysis_version_id != values.analysis_id
                ):
                    raise HTTPException(409, "Automatic transition state changed.")
                deck = await db.get(Deck, session.deck_id)
                if deck is None:
                    raise HTTPException(404)
                record, _ = await current_ready_analysis(db, deck, principal.id)
                if record.id != values.analysis_id or record.source_format not in {"pdf", "pptx"}:
                    raise HTTPException(409, "This source has no compatible live agent runtime.")
                analysis = await request.app.state.candidate_analyses.read(
                    db, record, principal.id, session.runtime_context
                )
                active_slide_id = values.active_slide_id or session.active_slide_id or ""
                focus_view, focus_region = request_focus(
                    persisted_slide_id=session.active_slide_id,
                    persisted_focus_view=session.focus_view,
                    persisted_focus_region=session.focus_region,
                    local_slide_id=active_slide_id,
                    local_focus_view=values.focus_view,
                    local_focus_region=values.focus_region,
                )
                source = AgentInput(
                    analysis,
                    active_slide_id,
                    focus_view,
                    "transition",
                    focus_region,
                )
                if source.current not in {slide.slide_id for slide in analysis.slides}:
                    raise HTTPException(422, "The active slide is not part of this analysis.")
                target = values.target
                if target.tool == "no_action":
                    raise HTTPException(422, "No action cannot execute a transition.")
                candidate = Candidate(
                    kind=target.tool
                    if target.tool in {"zoom_region", "highlight_region"}
                    else "slide",
                    slide_id=target.slide_id,
                    action_id=target.action_id,
                    reason="topic",
                    score=0,
                )
                allowed, _ = options(source, (candidate,))
                if target not in allowed:
                    raise HTTPException(409, "The action is no longer eligible.")
                return TransitionApproval(
                    session_id=identifier,
                    analysis_id=record.id,
                    state_version=(
                        values.expected_state_version
                        if values.active_slide_id is not None
                        else session.state_version
                    ),
                    target=target,
                )
        except (TimeoutError, SQLAlchemyError, PersistenceError, OSError):
            raise HTTPException(503, "Transition check unavailable; continue manually.") from None

    input_schema = AgentRequest.model_json_schema()
    # Rebase FocusRegion to the shared OpenAPI component before dropping request-local defs.
    focus_schema = input_schema["properties"]["focus_region"]
    focus_schema["anyOf"][0]["$ref"] = "#/components/schemas/FocusRegion"
    input_schema.pop("$defs", None)
    input_schema["properties"]["segments"]["items"] = SpeechSegment.model_json_schema()

    @router.post(
        "/{identifier}/agent",
        response_model=AgentProposal,
        response_model_exclude_unset=True,
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
    ) -> AgentProposal | JSONResponse:
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
        speech_age_ms = round((datetime.now(UTC) - values.sent_at).total_seconds() * 1000)
        if not -2000 <= speech_age_ms <= 5000:
            raise agent_conflict(
                request,
                "speech_context_expired",
                "Speech context expired; wait for fresh speech.",
                speech_age_ms=speech_age_ms,
            )
        started = time.monotonic()
        runtime: AgentRuntime | None = request.app.state.presentation_agent
        decision_diagnostics: dict[str, object] | None = (
            {
                "input": values.model_dump(mode="json"),
                "analysis_id": None,
                "candidate_retrieval": None,
                "candidate_options": None,
                "option_targets": None,
            }
            if (
                runtime is not None
                and runtime.settings.diagnostics_enabled
                and runtime.settings.diagnostics_session_id == identifier
            )
            else None
        )
        trace_token = DECISION_TRACE.set(decision_diagnostics)
        admitted = False
        retry_after_ms = None
        phase = "session"
        phase_started = started
        phase_timings_ms: dict[str, int] = {}
        failure_detail = None

        def finish_phase() -> None:
            nonlocal phase_started
            phase_timings_ms[phase] = max(0, round((time.monotonic() - phase_started) * 1000))
            phase_started = time.monotonic()

        def begin_phase(name: str) -> None:
            nonlocal phase, phase_started
            phase = name
            phase_started = time.monotonic()

        try:
            async with asyncio.timeout(5):
                source = await read_input(request, principal, identifier, values)
                if decision_diagnostics is not None:
                    decision_diagnostics["analysis_id"] = str(source.analysis.id)
                    decision_diagnostics["current_slide_id"] = source.current
                finish_phase()
                target = Target(tool="no_action", slide_id=source.current)
                reason: AgentReason = "disabled"
                decision_source: Literal["model", "navigation", "fallback"] = "fallback"
                selection = None
                if runtime is not None:
                    begin_phase("admission")
                    if principal.id in runtime.active or len(runtime.active) >= 16:
                        raise AgentFailure("busy")
                    runtime.active.add(principal.id)
                    admitted = True
                    finish_phase()
                    intent = navigation_intent(values.latest_utterance)
                    if intent is not None:
                        decision_source = "navigation"
                        begin_phase("navigation")
                        candidates = resolve(
                            source.analysis, source.current, values.text, values.latest_utterance
                        )
                        targets, descriptions = options(source, candidates)
                        if decision_diagnostics is not None:
                            decision_diagnostics.update(
                                route="explicit_navigation_fastpath",
                                candidate_retrieval=[c.model_dump(mode="json") for c in candidates],
                                candidate_options=descriptions,
                                option_targets=[t.model_dump(mode="json") for t in targets],
                            )
                        selection = navigation_selection(targets, candidates, len(values.segments))
                        finish_phase()
                    else:
                        begin_phase("quota_reservation")
                        # Reserve first: exhausted/fast clients cannot spend query quota.
                        await runtime.reserve(request.app.state.database)
                        finish_phase()
                        begin_phase("retrieval")
                        semantic = request.app.state.semantic_candidates
                        direct = runtime.settings.candidate_mode == "direct_current"
                        if direct:
                            candidates, targets, descriptions = direct_current_options(source)
                        elif semantic is not None and decision_diagnostics is not None:
                            candidates = await semantic.retrieve(
                                source.analysis,
                                principal.id,
                                source.current,
                                values.latest_utterance or values.text,
                                values.text,
                                diagnostics=decision_diagnostics,
                            )
                        elif semantic is not None:
                            candidates = await semantic.retrieve(
                                source.analysis,
                                principal.id,
                                source.current,
                                values.latest_utterance or values.text,
                                values.text,
                            )
                        else:
                            candidates = resolve(
                                source.analysis,
                                source.current,
                                values.text,
                                values.latest_utterance,
                            )
                        if not direct:
                            targets, descriptions = options(source, candidates)
                        if decision_diagnostics is not None:
                            decision_diagnostics.update(
                                route="direct_current"
                                if direct
                                else "semantic_retrieval"
                                if semantic is not None
                                else "lexical_retrieval",
                                candidate_retrieval=[c.model_dump(mode="json") for c in candidates],
                                candidate_options=descriptions,
                                option_targets=[t.model_dump(mode="json") for t in targets],
                                resolver_version=(
                                    "direct-current-v1"
                                    if direct
                                    else "cohere-embed5-v4"
                                    if semantic is not None
                                    else "summary-overlap-2"
                                ),
                            )
                        finish_phase()
                        fast_topic = (
                            topic_navigation_selection(
                                targets,
                                candidates,
                                len(values.segments),
                                semantic=semantic is not None,
                            )
                            if not direct and topic_navigation_intent(values.latest_utterance)
                            else None
                        )
                        if fast_topic is not None:
                            begin_phase("topic_navigation")
                            selection = fast_topic
                            decision_source = "navigation"
                            record_decision_trace(
                                bypass="confident_topic_fastpath",
                                selected_choice=selection.choice,
                                selected_reason=selection.reason,
                                selected_confidence=selection.confidence,
                                selected_evidence=list(selection.evidence),
                            )
                            finish_phase()
                        else:
                            begin_phase("selection")
                            passage = decision_passage(source, values, descriptions)
                            provider = runtime.provider_for(values.decision_model)
                            if decision_diagnostics is not None:
                                decision_diagnostics["provider_passage"] = passage
                                decision_diagnostics["provider_class"] = type(provider).__name__
                            selection = await provider.select(passage)
                            finish_phase()
                            decision_source = "model"
                    target = checked_selection(selection, targets, len(values.segments))
                    reason = (
                        "stay"
                        if target.tool == "no_action" and selection.reason != "uncertain"
                        else selection.reason
                    )
                    if (datetime.now(UTC) - values.sent_at).total_seconds() > 5:
                        raise AgentFailure("stale")
                    begin_phase("validation")
                    try:
                        fresh = await read_input(request, principal, identifier, values)
                    except HTTPException as exc:
                        if exc.status_code == 409:
                            raise AgentFailure("stale") from None
                        raise
                    if fresh.identity != source.identity:
                        raise AgentFailure("stale")
                    finish_phase()
                    if decision_diagnostics is not None and selection is not None:
                        decision_diagnostics["selection"] = selection.model_dump(mode="json")
                        decision_diagnostics["selected_target"] = target.model_dump(mode="json")
        except HTTPException:
            # Ownership/auth failures must remain denied; stale session failures never propose.
            raise
        except (AgentFailure, EmbeddingFailure) as exc:
            reason = exc.code if isinstance(exc, AgentFailure) else "candidates_unavailable"
            if decision_diagnostics is not None:
                decision_diagnostics["failure"] = {
                    "kind": type(exc).__name__,
                    "reason": reason,
                }
            target = Target(tool="no_action", slide_id=source.current)
            selection = None
            decision_source = "fallback"
            if isinstance(exc, AgentFailure) and exc.code == "rate_limited" and runtime is not None:
                try:
                    await runtime.provider_limited(request.app.state.database)
                except (SQLAlchemyError, OSError, TimeoutError):
                    raise HTTPException(503, "Agent temporarily unavailable.") from None
            if isinstance(exc, AgentFailure) and exc.code == "paced":
                retry_after_ms = exc.retry_after_ms or 3200
            if isinstance(exc, EmbeddingFailure):
                known = {
                    "embedding_document_limit",
                    "embedding_budget_exhausted",
                    "embedding_preparation_busy",
                    "embedding_index_not_ready",
                    "embedding_invalid_index",
                    "embedding_query_busy",
                    "embedding_invalid_response",
                    "embedding_input_limit",
                    "embedding_provider_unavailable",
                    "embedding_rate_limited",
                }
                failure_detail = exc.code if exc.code in known else "other"
        except TimeoutError:
            if "source" not in locals():
                raise HTTPException(503, "Session unavailable.") from None
            reason, target, selection = (
                "deadline",
                Target(tool="no_action", slide_id=source.current),
                None,
            )
            decision_source = "fallback"
            if decision_diagnostics is not None:
                decision_diagnostics["failure"] = {
                    "kind": "TimeoutError",
                    "reason": "deadline",
                }
        except (PersistenceError, SQLAlchemyError, OSError):
            raise HTTPException(503, "Agent temporarily unavailable.") from None
        finally:
            if phase not in phase_timings_ms:
                finish_phase()
            if admitted and runtime is not None:
                runtime.active.discard(principal.id)
            DECISION_TRACE.reset(trace_token)
        proposal_values: dict[str, object] = {
            "protocol": "presentation-agent.v1",
            "decision_id": uuid4(),
            "session_id": identifier,
            "analysis_id": source.analysis.id,
            "state_version": values.expected_state_version,
            "context_epoch": values.context_epoch,
            "stream_id": values.stream_id,
            "sequence": values.sequence,
            "input_sha256": hashlib.sha256(values.model_dump_json().encode()).hexdigest(),
            # Serialize optional target members explicitly so adding an optional
            # proposal field does not silently strip existing response fields.
            "target": target.model_dump(),
            "decision_source": decision_source,
            "reason": reason,
            "confidence": selection.confidence if selection else None,
            "evidence": selection.evidence if selection else (),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "retry_after_ms": retry_after_ms,
        }
        if (
            values.web_search
            and selection is not None
            and selection.search is not None
            and decision_source == "model"
        ):
            proposal_values["search"] = selection.search
        if (
            values.web_search
            and values.search_panel_open
            and selection is not None
            and selection.panel is not None
            and decision_source == "model"
        ):
            proposal_values["panel"] = selection.panel
        if values.active_slide_id is not None:
            proposal_values["active_slide_id"] = source.current
        proposal = AgentProposal.model_validate(proposal_values)
        # Raw structured event, like existing EMF events: independent of Uvicorn's
        # warning-only logger. Only locally typed finite fields reach this boundary.
        trace = {
            "event": "presentation_agent_decision",
            "decision_id": str(proposal.decision_id),
            "request_id": request.state.request_id,
            "tool": proposal.target.tool,
            "search": proposal.search,
            "panel": proposal.panel,
            "reason": proposal.reason,
            "elapsed_ms": proposal.elapsed_ms,
            "model": DECIDED_BY.get() if runtime else "disabled",
            "decision_source": proposal.decision_source,
            "phase": phase,
            "phase_timings_ms": phase_timings_ms,
            "failure_detail": failure_detail,
        }
        try:
            sys.stdout.write(json.dumps(trace, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except OSError:
            pass  # Telemetry cannot fail a presentation request.
        if decision_diagnostics is not None:
            decision_diagnostics["phase"] = phase
            decision_diagnostics["phase_timings_ms"] = phase_timings_ms
            if decision_source == "model":
                decision_diagnostics["resolved_provider"] = DECIDED_BY.get()
            proposal_body = proposal.model_dump(mode="json", exclude_unset=True)
            decision_diagnostics["final_proposal"] = {**proposal_body}
            proposal_body["diagnostics"] = decision_diagnostics
            return JSONResponse(
                content=proposal_body,
                headers={"Cache-Control": "no-store"},
            )
        return proposal

    return router
