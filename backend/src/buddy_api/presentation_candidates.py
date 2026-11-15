"""Bounded deterministic retrieval over saved summaries and usable actions."""

import asyncio
import hashlib
import re
import time
import unicodedata
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from buddy_api.analysis.contracts import (
    ActionResult,
    AnalysisResult,
    Model,
    PowerPointActionResult,
    PowerPointAnalysisResult,
    PowerPointSlideResult,
    SlideResult,
)
from buddy_api.analysis.store import Analyses
from buddy_api.auth import Principal, authenticated
from buddy_api.embeddings import EmbeddingFailure
from buddy_api.persistence.errors import PersistenceError
from buddy_api.persistence.models import ActionValidation, AnalysisVersion, Deck
from buddy_api.presentation_sessions import current_ready_analysis, owned_session
from buddy_api.semantic_candidates import SemanticCandidates

STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "this",
        "that",
        "it",
        "is",
        "are",
        "was",
        "were",
        "to",
        "of",
        "and",
        "or",
        "in",
        "on",
        "for",
        "with",
        "please",
        "let",
        "lets",
        "go",
        "back",
        "show",
        "me",
        "slide",
        "about",
        "our",
        "we",
        "you",
        "i",
    ]
)


def candidate_slides(
    analysis: AnalysisResult | PowerPointAnalysisResult,
) -> tuple[SlideResult | PowerPointSlideResult, ...]:
    return cast(tuple[SlideResult | PowerPointSlideResult, ...], analysis.slides)


def candidate_action_eligible(action: ActionResult | PowerPointActionResult) -> bool:
    """Require independent semantic evidence, not only successful Office execution."""
    if not action.usable or action.state != "validated":
        return False
    if action.action.type not in {"zoom_region", "highlight_region"}:
        return False
    if hasattr(action, "semantic_verified"):
        return bool(action.semantic_verified)
    evidence = getattr(action, "validation", None)
    return bool(evidence is not None and evidence.semantic_verified)


class CandidateRequest(Model):
    protocol: Literal["presentation-candidates.v1"] = "presentation-candidates.v1"
    expected_state_version: int = Field(ge=0, strict=True)
    context_epoch: int = Field(ge=0, strict=True)
    stream_id: UUID
    sequence: int = Field(ge=1, strict=True)
    sent_at: datetime
    text: str = Field(min_length=1, max_length=8192, repr=False)
    latest_utterance: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)

    @model_validator(mode="after")
    def aware(self) -> "CandidateRequest":
        if self.sent_at.tzinfo is None or not self.text.strip():
            raise ValueError("Fresh timestamp and nonempty context required")
        if self.latest_utterance is not None and (
            not self.latest_utterance.strip()
            or not (
                self.text == self.latest_utterance
                or self.text.endswith(" " + self.latest_utterance)
            )
        ):
            raise ValueError("Latest utterance must be the final segment of the rolling context")
        return self


class Candidate(Model):
    kind: Literal["no_change", "slide", "zoom_region", "highlight_region"]
    slide_id: str
    action_id: str | None = None
    reason: Literal["stay", "current", "explicit_navigation", "topic", "purpose"]
    score: int = Field(ge=0)


class CandidateSet(Model):
    protocol: Literal["presentation-candidates.v1"] = "presentation-candidates.v1"
    resolver_version: Literal[
        "summary-overlap-2",
        "cohere-embed5-v1",
        "cohere-embed5-v2",
        "cohere-embed5-v3",
        "cohere-embed5-v4",
    ] = "summary-overlap-2"
    session_id: UUID
    analysis_id: UUID
    state_version: int
    context_epoch: int
    stream_id: UUID
    sequence: int
    input_sha256: str
    candidates: tuple[Candidate, ...] = Field(max_length=8)


class CandidateAnalyses:
    """Briefly reuse region-free typed reads; never cache authorization or usable regions."""

    def __init__(self) -> None:
        self._entries: OrderedDict[
            tuple[UUID, UUID, str, int], tuple[float, AnalysisResult | PowerPointAnalysisResult]
        ] = OrderedDict()

    async def read(
        self, db: AsyncSession, record: AnalysisVersion, owner: UUID
    ) -> AnalysisResult | PowerPointAnalysisResult:
        # Both callers use REPEATABLE READ. Validation history is append-only:
        # its count identifies the same eligibility snapshot as the canonical read.
        revision = int(
            await db.scalar(
                select(func.count())
                .select_from(ActionValidation)
                .where(
                    ActionValidation.analysis_id == record.id,
                    ActionValidation.owner_id == owner,
                )
            )
            or 0
        )
        key = (owner, record.id, record.content_sha256 or "", revision)
        now = time.monotonic()
        self._entries = OrderedDict(
            (k, value) for k, value in self._entries.items() if now - value[0] < 30
        )
        cached = self._entries.get(key)
        if cached is not None:
            # Active presentations need not reconstruct hundreds of unusable
            # actions every 30 seconds. The key and authorization were rechecked;
            # expire inactive entries 30 seconds after their last verified use.
            self._entries[key] = (now, cached[1])
            self._entries.move_to_end(key)
            return cached[1]
        analysis = await Analyses(db).read(record.id, owner_id=owner)
        if not isinstance(analysis, (AnalysisResult, PowerPointAnalysisResult)):
            raise HTTPException(409, "Saved presentation analysis is required.")
        # A cached read can propose slides only. Any positively usable region always
        # takes the canonical path, including its current evidence and hash checks.
        if record.content_sha256 and not any(a.usable for s in analysis.slides for a in s.actions):
            self._entries[key] = (now, analysis)
            while len(self._entries) > 8:
                self._entries.popitem(last=False)
        return analysis


def tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())) - STOP


def navigation_step(text: str) -> int | None:
    """Whole, unambiguous relative commands; other speech stays on the semantic path."""
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    # Keep questions/quoted examples out of the fast path. Apostrophes are allowed
    # only in the contraction below; stripping punctuation first loses this intent.
    text = re.sub(r"\blet['’]s\b", "let us", text)
    if any(c in text for c in "?？\"'“”‘’«»"):
        return None
    normalized = " ".join(re.findall(r"[^\W_]+", text))
    direct = re.fullmatch(
        r"(?:please )?(?:let us )?(?:(?:go|move|switch|advance) to (?:the )?)?"
        r"(next|previous) (?:slide|page)(?: please)?",
        normalized,
    )
    if direct:
        return 1 if direct[1] == "next" else -1
    relative = re.fullmatch(
        r"(?:please )?(?:let us )?(?:go|move) (forward|back|backward) "
        r"(?:by )?one (?:slide|page)(?: please)?",
        normalized,
    )
    if relative:
        return 1 if relative[1] == "forward" else -1
    return None


def resolve(
    analysis: AnalysisResult | PowerPointAnalysisResult,
    current: str,
    text: str,
    latest_utterance: str | None = None,
) -> tuple[Candidate, ...]:
    slides = candidate_slides(analysis)
    identifiers = [slide.slide_id for slide in slides]
    if current not in identifiers or not 1 <= len(slides) <= 500:
        raise HTTPException(409, "The session slide map is unavailable.")
    position = identifiers.index(current)
    result = [
        Candidate(kind="no_change", slide_id=current, reason="stay", score=0),
        Candidate(kind="slide", slide_id=current, reason="current", score=0),
    ]
    query = tokens(text)
    # Commands are whole utterances, not substrings in slide discussion or negation.
    step = navigation_step(latest_utterance if latest_utterance is not None else text)
    target = position + (step or 0)
    if 0 <= target < len(slides) and target != position:
        result.append(
            Candidate(
                kind="slide", slide_id=identifiers[target], reason="explicit_navigation", score=0
            )
        )
    ranked = sorted(
        (
            (
                len(
                    query
                    & tokens(
                        (slide.summary.text or "")
                        + " "
                        + " ".join(
                            a.action.purpose for a in slide.actions if candidate_action_eligible(a)
                        )
                    )
                ),
                index,
            )
            for index, slide in enumerate(slides)
            if slide.summary.status == "available"
        ),
        key=lambda row: (-row[0], row[1]),
    )
    # Ties abstain from non-linear navigation; a unique lexical match is evidence,
    # never permission to execute. Whole-deck action space is never returned.
    if ranked and ranked[0][0] > 0 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        score, index = ranked[0]
        slide_id = identifiers[index]
        if slide_id != current and all(c.slide_id != slide_id for c in result):
            result.append(Candidate(kind="slide", slide_id=slide_id, reason="topic", score=score))
    selected = {candidate.slide_id for candidate in result}
    actions = []
    for slide in slides:
        if slide.slide_id not in selected:
            continue
        for action in slide.actions:
            if not candidate_action_eligible(action):
                continue
            if action.action.type not in {"zoom_region", "highlight_region"}:
                continue
            score = len(query & tokens(action.action.purpose))
            if score:
                actions.append(
                    Candidate(
                        kind=action.action.type,
                        slide_id=slide.slide_id,
                        action_id=action.action.instance_id,
                        reason="purpose",
                        score=score,
                    )
                )
    result.extend(sorted(actions, key=lambda c: (-c.score, c.slide_id, c.action_id or ""))[:2])
    return tuple(result)


def candidate_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/sessions", tags=["presentation candidates"])

    @router.post(
        "/{identifier}/candidates",
        response_model=CandidateSet,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": CandidateRequest.model_json_schema()}},
            }
        },
    )
    async def candidates(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> CandidateSet:
        response.headers["Cache-Control"] = "no-store"
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise HTTPException(413, "Candidate request is too large.")
        except TimeoutError:
            raise HTTPException(408, "Candidate request timed out.") from None
        try:
            values = CandidateRequest.model_validate_json(bytes(body))
        except (ValidationError, ValueError):
            raise HTTPException(422, "Invalid candidate context.") from None
        age = (datetime.now(UTC) - values.sent_at).total_seconds()
        if not -2 <= age <= 5:
            raise HTTPException(409, "The candidate context is stale; refresh it.")
        try:
            async with (
                asyncio.timeout(5),
                request.app.state.database.sessions() as db,
                db.begin(),
            ):
                await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                session = await owned_session(db, principal, identifier)
                if (
                    session.status != "active"
                    or session.state_version != values.expected_state_version
                ):
                    raise HTTPException(409, "The session changed; refresh context.")
                deck = await db.get(Deck, session.deck_id)
                if deck is None:
                    raise HTTPException(404)
                record, _ = await current_ready_analysis(db, deck, principal.id)
                if record.id != session.analysis_version_id:
                    raise HTTPException(409, "The analysis changed; reconnect the session.")
                analysis = await request.app.state.candidate_analyses.read(db, record, principal.id)
                semantic: SemanticCandidates | None = request.app.state.semantic_candidates
                if semantic is not None:
                    targets = await semantic.retrieve(
                        analysis,
                        principal.id,
                        session.active_slide_id or "",
                        values.latest_utterance or values.text,
                        values.text,
                    )
                else:
                    targets = await asyncio.wait_for(
                        asyncio.to_thread(
                            resolve,
                            analysis,
                            session.active_slide_id or "",
                            values.text,
                            values.latest_utterance,
                        ),
                        timeout=2,
                    )
                return CandidateSet(
                    resolver_version="cohere-embed5-v4"
                    if semantic is not None
                    else "summary-overlap-2",
                    session_id=identifier,
                    analysis_id=record.id,
                    state_version=session.state_version,
                    context_epoch=values.context_epoch,
                    stream_id=values.stream_id,
                    sequence=values.sequence,
                    input_sha256=hashlib.sha256(values.model_dump_json().encode()).hexdigest(),
                    candidates=targets,
                )
        except EmbeddingFailure:
            raise
        except (PersistenceError, SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(
                503, "Candidates are unavailable; retry with fresh context."
            ) from None

    @router.post("/{identifier}/candidates/prepare")
    async def prepare_candidates(
        identifier: UUID,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authenticated)],
    ) -> dict[str, str | bool]:
        response.headers["Cache-Control"] = "no-store"
        semantic: SemanticCandidates | None = request.app.state.semantic_candidates
        if semantic is None:
            raise HTTPException(503, "Semantic retrieval is not configured.")
        try:
            async with request.app.state.database.sessions() as db, db.begin():
                await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                session = await owned_session(db, principal, identifier)
                if session.status != "active":
                    raise HTTPException(409, "The session ended.")
                deck = await db.get(Deck, session.deck_id)
                if deck is None:
                    raise HTTPException(404)
                record, _ = await current_ready_analysis(db, deck, principal.id)
                if record.id != session.analysis_version_id:
                    raise HTTPException(409, "The analysis changed.")
                analysis = await request.app.state.candidate_analyses.read(db, record, principal.id)
            created = await semantic.prepare(analysis, principal.id)
            return {"ready": True, "created": created, "resolver_version": "cohere-embed5-v4"}
        except EmbeddingFailure:
            raise
        except (PersistenceError, SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503, "Semantic preparation unavailable.") from None

    return router
