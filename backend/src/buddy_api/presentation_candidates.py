"""Bounded deterministic retrieval over saved summaries and usable actions."""

import asyncio
import hashlib
import re
import time
import unicodedata
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
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
from buddy_api.persistence.models import ActionValidation, AnalysisVersion, Deck, DeckExtraction
from buddy_api.presentation_sessions import current_ready_analysis, owned_session, runtime_matches
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


def shown_text(
    analysis: AnalysisResult | PowerPointAnalysisResult,
    action: ActionResult | PowerPointActionResult,
) -> str | None:
    """The slide text a region action shows, so speech naming it matches the action.

    PowerPoint highlights quote their target; PowerPoint zooms frame a whole shape whose
    text `CandidateAnalyses` restores from the saved extraction. PDF actions keep their
    quoted text in the analysis grounding record, not in the action parameters.
    """
    params = getattr(action.action, "params", None)
    for value in (getattr(params, "target_text", None), getattr(params, "text", None)):
        if isinstance(value, str) and value.strip():
            return value.strip()
    for item in getattr(getattr(analysis, "inputs", None), "grounding", ()) or ():
        if item.instance_id == action.action.instance_id and item.trace.target_text:
            return str(item.trace.target_text).strip()
    return None


async def with_powerpoint_shape_text(
    db: AsyncSession, record: AnalysisVersion, owner: UUID, analysis: PowerPointAnalysisResult
) -> PowerPointAnalysisResult:
    """Copy each zoomed shape's text from the saved extraction into this server-side read.

    PowerPoint zooms select a complete shape and so carry no quoted target. The copy is
    used only to describe candidates to retrieval and the agent; it is never persisted.
    """
    if record.extraction_id is None or not any(
        a.action.type == "zoom_region" for slide in analysis.slides for a in slide.actions
    ):
        return analysis
    document = await db.scalar(
        select(DeckExtraction.document).where(
            DeckExtraction.id == record.extraction_id, DeckExtraction.owner_id == owner
        )
    )
    if not isinstance(document, dict):
        return analysis
    texts: dict[tuple[str, int], str] = {}
    for extracted in document.get("slides") or ():
        for obj in extracted.get("objects") or ():
            text, source = obj.get("text"), obj.get("source_id")
            if isinstance(text, str) and text.strip() and type(source) is int:
                texts[(str(extracted.get("id")), source)] = " ".join(text.split())[:300]

    def described(slide_id: str, item: PowerPointActionResult) -> PowerPointActionResult:
        params = item.action.params
        shape = params.source_shape_id
        text = texts.get((slide_id, shape)) if shape is not None else None
        if item.action.type != "zoom_region" or params.text.strip() or not text:
            return item
        action = item.action.model_copy(update={"params": params.model_copy(update={"text": text})})
        return item.model_copy(update={"action": action})

    return analysis.model_copy(
        update={
            "slides": tuple(
                slide.model_copy(
                    update={"actions": tuple(described(slide.slide_id, a) for a in slide.actions)}
                )
                for slide in analysis.slides
            )
        }
    )


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
    # Optional for compatibility with existing clients. New clients provide the
    # locally observed slide so a position write is not a prerequisite for retrieval.
    active_slide_id: str | None = Field(default=None, min_length=1, max_length=128)
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
        "cohere-embed5-v5",
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
        self,
        db: AsyncSession,
        record: AnalysisVersion,
        owner: UUID,
        runtime_context: dict[str, Any] | None = None,
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
        if isinstance(analysis, PowerPointAnalysisResult):
            analysis = await with_powerpoint_shape_text(db, record, owner, analysis)
        # A cached read can propose slides only. Any positively usable region always
        # takes the canonical path, including its current evidence and hash checks.
        if record.content_sha256 and not any(a.usable for s in analysis.slides for a in s.actions):
            self._entries[key] = (now, analysis)
            while len(self._entries) > 8:
                self._entries.popitem(last=False)
        return analysis.model_copy(
            update={
                "slides": tuple(
                    slide.model_copy(
                        update={
                            "actions": tuple(
                                action
                                if action.validation
                                and runtime_matches(action.validation, runtime_context)
                                else action.model_copy(update={"usable": False})
                                for action in slide.actions
                            )
                        }
                    )
                    for slide in analysis.slides
                )
            }
        )


def tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())) - STOP


def navigation_step(text: str) -> int | None:
    """Recognize a leading relative command while the presenter keeps talking."""
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    # Keep questions/quoted examples out of the fast path. Apostrophes are allowed
    # only in the contraction below; stripping punctuation first loses this intent.
    text = re.sub(r"\blet['’]s\b", "let us", text)
    if any(c in text for c in "?？\"'“”‘’«»"):
        return None
    normalized = " ".join(re.findall(r"[^\W_]+", text))
    direct = re.match(
        r"(?:please )?(?:let us )?(?:(?:go|move|switch|advance) to (?:the )?)?"
        r"(next|previous) (?:slide|page)(?: please)?(?=$|[.,;:!?]|\s)",
        normalized,
    )
    if direct and _safe_navigation_continuation(normalized[direct.end() :]):
        return 1 if direct[1] == "next" else -1
    relative = re.match(
        r"(?:please )?(?:let us )?(?:go|move) (forward|back|backward) "
        r"(?:by )?one (?:slide|page)(?: please)?(?=$|[.,;:!?]|\s)",
        normalized,
    )
    if relative and _safe_navigation_continuation(normalized[relative.end() :]):
        return 1 if relative[1] == "forward" else -1
    return None


def _safe_navigation_continuation(suffix: str) -> bool:
    """Allow an imperative followed by explanation, but not a reversal or condition."""
    remainder = " ".join(re.findall(r"[^\W_]+", suffix.casefold()))
    if not remainder:
        return True
    words = set(remainder.split())
    # A continuation can change an imperative into a conditional or explicitly
    # tell Buddy not to move. Check the entire suffix, not just its first token.
    if words & {
        "if",
        "unless",
        "until",
        "only",
        "provided",
        "assuming",
        "depending",
        "not",
        "no",
        "never",
        "cannot",
        "without",
    } or re.search(
        r"\b(?:don|doesn|didn|can|could|couldn|should|shouldn|would|wouldn|won|mustn|mightn) t\b",
        remainder,
    ):
        return False
    first = remainder.split()[0]
    if any(
        re.search(r"(?<!\w)" + marker + r"(?!\w)", remainder)
        for marker in (
            "actually",
            "instead",
            "rather",
            "sorry",
            "wait",
            "never",
            "cancel",
            "forget",
            "scratch",
        )
    ):
        return False
    return first in {
        "and",
        "as",
        "because",
        "since",
        "so",
        "then",
        "where",
        "which",
        "while",
        "we",
        "i",
        "you",
        "it",
        "this",
        "that",
        "the",
        "now",
        "um",
        "uh",
        "please",
    }


def _spoken_slide_number(tokens_: list[str]) -> int | None:
    """Parse English cardinals from 0 through 999 without guessing malformed speech."""
    if len(tokens_) == 1 and re.fullmatch(r"[+-]?\d+", tokens_[0]):
        digits = tokens_[0].lstrip("+-")
        return int(tokens_[0]) if len(digits) <= 9 else None

    small = {
        "zero": 0,
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
    }
    tens = {
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "fifty": 50,
        "sixty": 60,
        "seventy": 70,
        "eighty": 80,
        "ninety": 90,
    }

    def under_hundred(parts: list[str]) -> int | None:
        if len(parts) == 1:
            return small.get(parts[0], tens.get(parts[0]))
        if len(parts) == 2 and parts[0] in tens and parts[1] in small:
            unit = small[parts[1]]
            return tens[parts[0]] + unit if 1 <= unit <= 9 else None
        return None

    if "hundred" not in tokens_:
        return under_hundred(tokens_)
    if tokens_.count("hundred") != 1 or not tokens_ or tokens_[0] not in small:
        return None
    hundreds = small[tokens_[0]]
    if not 1 <= hundreds <= 9 or tokens_[1] != "hundred":
        return None
    remainder = tokens_[2:]
    if remainder[:1] == ["and"]:
        remainder = remainder[1:]
    if not remainder:
        return hundreds * 100
    value = under_hundred(remainder)
    return hundreds * 100 + value if value is not None and value > 0 else None


def _spoken_slide_ordinal(tokens_: list[str]) -> int | None:
    """Parse spoken English ordinals up to 999 and numeric ordinal forms."""
    if len(tokens_) == 1:
        numeric = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", tokens_[0])
        if numeric is not None:
            value = int(numeric.group(1))
            return value if 1 <= value <= 999 else None

    units = {
        "first": 1,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "fifth": 5,
        "sixth": 6,
        "seventh": 7,
        "eighth": 8,
        "ninth": 9,
        "tenth": 10,
        "eleventh": 11,
        "twelfth": 12,
        "thirteenth": 13,
        "fourteenth": 14,
        "fifteenth": 15,
        "sixteenth": 16,
        "seventeenth": 17,
        "eighteenth": 18,
        "nineteenth": 19,
    }
    tens = {
        "twentieth": 20,
        "thirtieth": 30,
        "fortieth": 40,
        "fiftieth": 50,
        "sixtieth": 60,
        "seventieth": 70,
        "eightieth": 80,
        "ninetieth": 90,
    }
    if len(tokens_) == 1:
        return units.get(tokens_[0], tens.get(tokens_[0]))
    cardinal_tens = {
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "fifty": 50,
        "sixty": 60,
        "seventy": 70,
        "eighty": 80,
        "ninety": 90,
    }
    if len(tokens_) == 2 and tokens_[0] in cardinal_tens and tokens_[1] in units:
        unit = units[tokens_[1]]
        return cardinal_tens[tokens_[0]] + unit if 1 <= unit <= 9 else None
    return None


def navigation_intent(text: str) -> tuple[Literal["relative", "absolute"], int] | None:
    """Recognize an explicit leading slide command without waiting for sentence end."""
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    text = re.sub(r"\blet['’]s\b", "let us", text)
    if any(c in text for c in "?？\"'“”‘’«»"):
        return None
    step = navigation_step(text)
    if step is not None:
        return ("relative", step)

    # Spoken ordinals are common in natural presentation commands. Recognize the
    # ordinal before the noun as well as the existing "slide one" form, so an
    # explicit command does not spend a retrieval and model-selection round trip.
    normalized = " ".join(re.findall(r"[^\W_]+", text))
    ordinal_prefix = re.match(
        r"(?:please )?(?:let us )?(?:go|move|jump|navigate|switch|return) "
        r"(?:back )?to (?:the )?(.+?) (?:slide|page)(?=$|[.,;:!?]|\s)",
        normalized,
    )
    if ordinal_prefix is not None and _safe_navigation_continuation(
        normalized[ordinal_prefix.end() :]
    ):
        ordinal = _spoken_slide_ordinal(ordinal_prefix.group(1).split())
        if ordinal is not None:
            return ("absolute", ordinal)

    # Keep a numeric sign attached until it has been validated. Stripping `-`
    # first would turn an invalid negative destination into a positive slide jump.
    words = re.findall(r"[+-]?\d+|[^\W\d_]+", text)
    normalized = " ".join(words)
    prefixes = (
        r"(?:please )?(?:let us )?(?:go|move|jump|navigate|switch|turn) "
        r"(?:to|over to) (?:the )?(?:slide|page) (?:number )?",
        r"(?:please )?(?:let us )?(?:go|move|return) back to "
        r"(?:the )?(?:slide|page) (?:number )?",
        r"(?:please )?show (?:me )?(?:the )?(?:slide|page) (?:number )?",
        r"(?:please )?take me to (?:the )?(?:slide|page) (?:number )?",
        r"(?:please )?i want to (?:go|move|jump) to (?:the )?(?:slide|page) (?:number )?",
    )
    for prefix in prefixes:
        match = re.match(prefix + r"(.+)", normalized)
        if match is not None:
            number_words = match[1].split()
            # The directive can be followed by the speaker's explanation. Try the
            # longest valid cardinal first, then accept only a safe continuation.
            for end in range(len(number_words), 0, -1):
                number = _spoken_slide_number(number_words[:end])
                if number is not None and _safe_navigation_continuation(
                    " ".join(number_words[end:])
                ):
                    return ("absolute", number)
    return None


def confident_topic_destination(candidates: tuple[Candidate, ...], *, semantic: bool) -> str | None:
    """Return only a clearly dominant topic destination for an explicit command."""
    destinations: dict[str, int] = {}
    current_score = 0
    for candidate in candidates:
        if candidate.kind != "slide":
            continue
        if candidate.reason == "current":
            current_score = candidate.score
        elif candidate.reason == "topic":
            destinations[candidate.slide_id] = max(
                destinations.get(candidate.slide_id, 0), candidate.score
            )
    if not destinations:
        return None
    ranked = sorted(destinations.items(), key=lambda item: (-item[1], item[0]))
    best_id, best_score = ranked[0]
    runner_up = max(current_score, ranked[1][1] if len(ranked) > 1 else 0)
    minimum, margin = (1000, 500) if semantic else (2, 1)
    if best_score < minimum or best_score - runner_up < margin:
        return None
    return best_id


def topic_navigation_intent(text: str) -> bool:
    """Recognize an explicit topic-navigation phrase, not a passing mention."""
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    text = re.sub(r"\blet['’]s\b", "let us", text)
    if any(character in text for character in "?？\"'“”‘’«»"):
        return False
    normalized = " ".join(re.findall(r"[^\W_]+", text))
    command = re.match(
        r"(?:please )?(?:let us )?(?:now )?(?:have a look at|take a look at|look at|"
        r"review|show me|open|go to|move to|switch to|jump to|return to|go back to) "
        r"(?:the )?(.+)",
        normalized,
    )
    if command is None:
        return False
    # Do not fast-path a command whose continuation negates, conditions, or
    # corrects it. Such speech stays with the constrained selector.
    continuation = command.group(1).split()
    unsafe = {
        "if",
        "unless",
        "until",
        "only",
        "provided",
        "assuming",
        "not",
        "never",
        "cannot",
        "without",
        "actually",
        "instead",
        "rather",
        "sorry",
        "wait",
        "cancel",
        "forget",
        "scratch",
        "stop",
    }
    return bool(continuation) and not (set(continuation) & unsafe)


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
    intent = navigation_intent(latest_utterance if latest_utterance is not None else text)
    if intent is not None and intent[0] == "absolute":
        requested = intent[1] - 1
        if 0 <= requested < len(slides) and requested != position:
            result.append(
                Candidate(
                    kind="slide",
                    slide_id=identifiers[requested],
                    reason="explicit_navigation",
                    score=0,
                )
            )
        # A recognized numbered command must never fall through to topic matching.
        return tuple(result)
    step = intent[1] if intent is not None else None
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
                            " ".join(sorted(action_words(a, analysis)))
                            for a in slide.actions
                            if candidate_action_eligible(a)
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
            # Match the spoken words against both the action's purpose and the slide
            # text it targets ("the revenue chart"), not the purpose alone (#202).
            score = len(query & action_words(action, analysis))
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
    result.extend(
        sorted(actions, key=lambda c: (-c.score, c.slide_id, c.action_id or ""))[:ACTION_SLOTS]
    )
    return tuple(result)


# Saved region actions offered per decision; the agent still chooses or abstains.
ACTION_SLOTS = 3


def action_words(
    action: Any, analysis: AnalysisResult | PowerPointAnalysisResult | None = None
) -> frozenset[str]:
    target = shown_text(analysis, action) if analysis is not None else None
    return tokens(action.action.purpose) | (tokens(target) if target else frozenset())


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
                if session.status != "active" or (
                    values.active_slide_id is None
                    and session.state_version != values.expected_state_version
                ):
                    raise HTTPException(409, "The session changed; refresh context.")
                deck = await db.get(Deck, session.deck_id)
                if deck is None:
                    raise HTTPException(404)
                record, _ = await current_ready_analysis(db, deck, principal.id)
                if record.id != session.analysis_version_id:
                    raise HTTPException(409, "The analysis changed; reconnect the session.")
                analysis = await request.app.state.candidate_analyses.read(
                    db, record, principal.id, session.runtime_context
                )
                semantic: SemanticCandidates | None = request.app.state.semantic_candidates
                active_slide_id = values.active_slide_id or session.active_slide_id or ""
                if active_slide_id not in {slide.slide_id for slide in analysis.slides}:
                    raise HTTPException(422, "The active slide is not part of this analysis.")
                if semantic is not None:
                    targets = await semantic.retrieve(
                        analysis,
                        principal.id,
                        active_slide_id,
                        values.latest_utterance or values.text,
                        values.text,
                    )
                else:
                    targets = await asyncio.wait_for(
                        asyncio.to_thread(
                            resolve,
                            analysis,
                            active_slide_id,
                            values.text,
                            values.latest_utterance,
                        ),
                        timeout=2,
                    )
                return CandidateSet(
                    resolver_version="cohere-embed5-v5"
                    if semantic is not None
                    else "summary-overlap-2",
                    session_id=identifier,
                    analysis_id=record.id,
                    state_version=(
                        values.expected_state_version
                        if values.active_slide_id is not None
                        else session.state_version
                    ),
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
                analysis = await request.app.state.candidate_analyses.read(
                    db, record, principal.id, session.runtime_context
                )
            created = await semantic.prepare(analysis, principal.id)
            return {"ready": True, "created": created, "resolver_version": "cohere-embed5-v5"}
        except EmbeddingFailure:
            raise
        except (PersistenceError, SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503, "Semantic preparation unavailable.") from None

    return router
