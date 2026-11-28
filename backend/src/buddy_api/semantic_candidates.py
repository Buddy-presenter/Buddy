"""Persistent presentation indexes; ephemeral queries; bounded Cohere usage."""

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

if TYPE_CHECKING:
    from buddy_api.presentation_candidates import Candidate

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from buddy_api.analysis.contracts import AnalysisResult, PowerPointAnalysisResult
from buddy_api.embeddings import (
    DIMENSIONS,
    DOCUMENT_MODEL,
    QUERY_MODEL,
    SPACE,
    CohereEmbeddings,
    EmbeddingFailure,
    normalize,
)
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import CandidateIndex, EmbeddingQuota


@dataclass(frozen=True)
class Document:
    slide_id: str
    action_id: str | None
    kind: str
    text: str = field(repr=False)


def documents(analysis: AnalysisResult | PowerPointAnalysisResult) -> tuple[Document, ...]:
    from buddy_api.presentation_candidates import (
        candidate_action_eligible,
        candidate_slides,
        shown_text,
    )

    result = []
    for position, slide in enumerate(candidate_slides(analysis), 1):
        if slide.summary.status == "available" and slide.summary.text:
            result.append(
                Document(slide.slide_id, None, "slide", f"Slide {position}. " + slide.summary.text)
            )
        for action in slide.actions:
            if candidate_action_eligible(action):
                # Include the slide text the action targets, so speech that names the
                # content ("the revenue chart") retrieves it, not only its purpose (#202).
                target = shown_text(analysis, action)
                result.append(
                    Document(
                        slide.slide_id,
                        action.action.instance_id,
                        action.action.type,
                        f"Slide {position}. "
                        + f"Slide context: {(slide.summary.text or '')[:400]}\n"
                        + f"Action type: {action.action.type}\n"
                        + action.action.purpose
                        + (f" Target: {target}" if target else ""),
                    )
                )
    if not result or len(result) > 1500 or sum(len(d.text) for d in result) > 2_000_000:
        raise EmbeddingFailure("embedding_document_limit")
    return tuple(result)


def fingerprint(items: tuple[Document, ...]) -> str:
    return hashlib.sha256(
        json.dumps(
            [(d.slide_id, d.action_id, d.kind, d.text) for d in items], ensure_ascii=False
        ).encode()
    ).hexdigest()


def combined_context(recent: str) -> str:
    """Embed a coherent bounded passage, retaining preceding references in one input."""
    text = recent.strip()
    if len(text) <= 4096:
        return text
    tail = text[-4096:]
    if not text[-4097].isspace() and not tail[0].isspace():
        boundary = re.search(r"\s", tail)
        if boundary is not None:
            tail = tail[boundary.end() :]
    return tail.strip()


def balanced_shortlist(
    items: tuple[Document, ...], similarities: tuple[tuple[float, ...], ...], current: str
) -> tuple[list[tuple[float, Document]], set[str]]:
    """Share available slide slots between views within the eight-candidate budget."""
    # Current slide and no change occupy two slots. Reserve three action slots
    # only when the current slide has usable region actions; actions on other
    # slides cannot execute yet and should not reduce the navigation shortlist.
    current_has_action_evidence = any(
        doc.action_id is not None
        and doc.slide_id == current
        and any(scores[index] > 0 for scores in similarities)
        for index, doc in enumerate(items)
    )
    other_slides = 3 if current_has_action_evidence else 6
    per_view = other_slides // len(similarities)
    rankings = [
        sorted(
            range(len(items)),
            key=lambda i: (-scores[i], items[i].slide_id, items[i].action_id or ""),
        )
        for scores in similarities
    ]
    # Reciprocal ranks combine ordering evidence, not confidence probabilities.
    fused = [0.0] * len(items)
    destinations = {current}
    for scores, ranking in zip(similarities, rankings, strict=True):
        view_slides: set[str] = set()
        for rank, index in enumerate(ranking):
            if scores[index] <= 0:
                continue
            fused[index] += 1 / (rank + 1)
            slide = items[index].slide_id
            if slide != current and len(view_slides) < per_view:
                view_slides.add(slide)
                destinations.add(slide)
    scored = sorted(
        zip(fused, items, strict=True),
        key=lambda item: (-item[0], item[1].slide_id, item[1].action_id or ""),
    )
    for score, doc in scored:
        if score > 0 and len(destinations) < other_slides + 1:
            destinations.add(doc.slide_id)
    return scored, destinations


def current_slide_action_documents(
    scored: list[tuple[float, Document]], current: str, limit: int
) -> tuple[tuple[float, Document], ...]:
    """Return ranked positive action matches that can be executed on this request.

    Region actions on other slides are not executable until navigation has happened,
    so presentation_agent.options drops them. Do not let those documents occupy the
    bounded candidate slots intended for Jev.
    """
    if limit <= 0:
        return ()
    return tuple(
        (score, doc)
        for score, doc in scored
        if score > 0 and doc.slide_id == current and doc.action_id is not None
    )[:limit]


class SemanticCandidates:
    def __init__(
        self, database: Database, provider: CohereEmbeddings, call_limit: int | None = None
    ):
        self.database, self.provider, self.call_limit = database, provider, call_limit
        self._preparing: set[UUID] = set()
        self._queries: set[UUID] = set()
        self._cache: dict[tuple[UUID, UUID, str], tuple[float, tuple[tuple[float, ...], ...]]] = {}

    async def _embed(self, texts: list[str], kind: str) -> tuple[tuple[float, ...], ...]:
        key_hash = hashlib.sha256(self.provider.settings.api_key.encode()).hexdigest()
        month = datetime.now(UTC).strftime("%Y-%m")
        async with self.database.sessions() as db, db.begin():
            statement = insert(EmbeddingQuota).values(key_hash=key_hash, month=month, calls=1)
            reservation = statement.on_conflict_do_update(
                index_elements=[EmbeddingQuota.key_hash, EmbeddingQuota.month],
                set_={"calls": EmbeddingQuota.calls + 1},
                where=EmbeddingQuota.calls < self.call_limit
                if self.call_limit is not None
                else None,
            ).returning(EmbeddingQuota.calls)
            if await db.scalar(reservation) is None:
                raise EmbeddingFailure("embedding_budget_exhausted")
        return await self.provider.embed(texts, "document" if kind == "document" else "query")

    async def prepare(
        self, analysis: AnalysisResult | PowerPointAnalysisResult, owner: UUID
    ) -> bool:
        items = documents(analysis)
        identity = fingerprint(items)
        async with self.database.sessions() as db:
            existing = await db.get(CandidateIndex, (analysis.id, SPACE))
            if (
                existing is not None
                and existing.owner_id == owner
                and existing.fingerprint == identity
            ):
                return False
        if owner in self._preparing or len(self._preparing) >= 2:
            raise EmbeddingFailure("embedding_preparation_busy")
        self._preparing.add(owner)
        try:
            async with asyncio.timeout(120):
                vectors: list[tuple[float, ...]] = []
                for start in range(0, len(items), 64):
                    vectors.extend(
                        await self._embed([d.text for d in items[start : start + 64]], "document")
                    )
                async with self.database.sessions() as db, db.begin():
                    statement = insert(CandidateIndex).values(
                        analysis_id=analysis.id,
                        space=SPACE,
                        deck_id=analysis.deck_id,
                        owner_id=owner,
                        fingerprint=identity,
                        vectors=[list(v) for v in vectors],
                    )
                    await db.execute(
                        statement.on_conflict_do_update(
                            index_elements=[CandidateIndex.analysis_id, CandidateIndex.space],
                            set_={"fingerprint": identity, "vectors": statement.excluded.vectors},
                        )
                    )
            return True
        finally:
            self._preparing.discard(owner)

    async def retrieve(
        self,
        analysis: AnalysisResult | PowerPointAnalysisResult,
        owner: UUID,
        current: str,
        latest: str,
        recent: str,
        *,
        diagnostics: dict[str, object] | None = None,
    ) -> tuple["Candidate", ...]:
        from buddy_api.presentation_candidates import (
            Candidate,
            candidate_slides,
            navigation_intent,
            resolve,
        )

        base = resolve(analysis, current, "", latest)
        if navigation_intent(latest) is not None:
            return base
        items = documents(analysis)
        async with self.database.sessions() as db:
            row = await db.scalar(
                select(CandidateIndex).where(
                    CandidateIndex.analysis_id == analysis.id,
                    CandidateIndex.space == SPACE,
                    CandidateIndex.owner_id == owner,
                    CandidateIndex.fingerprint == fingerprint(items),
                )
            )
            if row is None:
                raise EmbeddingFailure("embedding_index_not_ready")
            raw = row.vectors
        if not isinstance(raw, list) or len(raw) != len(items):
            raise EmbeddingFailure("embedding_invalid_index")
        vectors = tuple(normalize(v) for v in raw)
        # Keep the request views available on both cache misses and hits so the
        # diagnostic trace can describe the exact query evidence either way.
        texts = [combined_context(recent)]
        if latest.strip() and latest.strip() != texts[0]:
            texts.append(combined_context(latest))
        cache_key = (
            owner,
            analysis.id,
            hashlib.sha256(json.dumps([combined_context(recent), latest]).encode()).hexdigest(),
        )
        now = time.monotonic()
        self._cache = {k: v for k, v in self._cache.items() if now - v[0] <= 5}
        cached = self._cache.get(cache_key)
        cache_hit = cached is not None
        if cached:
            queries = cached[1]
        else:
            if owner in self._queries or len(self._queries) >= 16:
                raise EmbeddingFailure("embedding_query_busy")
            self._queries.add(owner)
            try:
                # Keep contextual references and the newest topic as independent
                # retrieval evidence; batch both inputs in one provider request.
                queries = await self._embed(texts, "query")
            finally:
                self._queries.discard(owner)
            if len(self._cache) >= 128:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = (time.monotonic(), queries)
        similarities = tuple(
            tuple(sum(a * b for a, b in zip(vector, query, strict=True)) for vector in vectors)
            for query in queries
        )
        scored, destinations = balanced_shortlist(items, similarities, current)
        if diagnostics is not None:
            slides = candidate_slides(analysis)
            slide_numbers = {slide.slide_id: index + 1 for index, slide in enumerate(slides)}
            query_views = []
            for query_index, scores in enumerate(similarities):
                ranking = sorted(
                    range(len(items)),
                    key=lambda index: (
                        -scores[index],
                        items[index].slide_id,
                        items[index].action_id or "",
                    ),
                )
                best_by_slide: dict[str, tuple[int, float, Document]] = {}
                for rank, index in enumerate(ranking, 1):
                    document = items[index]
                    previous = best_by_slide.get(document.slide_id)
                    if previous is None or scores[index] > previous[1]:
                        best_by_slide[document.slide_id] = (rank, scores[index], document)
                query_views.append(
                    {
                        "query_index": query_index,
                        "query_text": texts[query_index],
                        "top_documents": [
                            {
                                "rank": rank,
                                "cosine_similarity": round(scores[index], 8),
                                "slide_number": slide_numbers[items[index].slide_id],
                                "slide_id": items[index].slide_id,
                                "action_id": items[index].action_id,
                                "kind": items[index].kind,
                                "indexed_text": items[index].text[:400],
                            }
                            for rank, index in enumerate(ranking[:12], 1)
                        ],
                        "best_document_per_slide": [
                            {
                                "best_document_rank": rank,
                                "cosine_similarity": round(score, 8),
                                "slide_number": slide_numbers[slide_id],
                                "slide_id": slide_id,
                                "action_id": document.action_id,
                                "kind": document.kind,
                            }
                            for slide_id, (rank, score, document) in sorted(
                                best_by_slide.items(), key=lambda item: slide_numbers[item[0]]
                            )
                        ],
                    }
                )
            diagnostics["embedding_retrieval"] = {
                "query_model": QUERY_MODEL,
                "document_model": DOCUMENT_MODEL,
                "query_input_type": "search_query",
                "document_input_type": "search_document",
                "dimensions": DIMENSIONS,
                "index_fingerprint": fingerprint(items),
                "indexed_document_count": len(items),
                "cache_hit": cache_hit,
                "scoring": (
                    "positive-cosine document ranks fused by reciprocal rank; "
                    "fused score scaled by 1000"
                ),
                "query_views": query_views,
                "fused_top_documents": [
                    {
                        "reciprocal_rank_score": round(score, 8),
                        "slide_number": slide_numbers[document.slide_id],
                        "slide_id": document.slide_id,
                        "action_id": document.action_id,
                        "kind": document.kind,
                        "indexed_text": document.text[:400],
                    }
                    for score, document in scored[:24]
                ],
                "shortlisted_slide_ids": sorted(
                    destinations, key=lambda slide_id: slide_numbers[slide_id]
                ),
            }
        result = [
            Candidate(kind="no_change", slide_id=current, reason="stay", score=0),
            Candidate(kind="slide", slide_id=current, reason="current", score=0),
        ]
        current_score = max((score for score, doc in scored if doc.slide_id == current), default=0)
        result[1] = Candidate(
            kind="slide",
            slide_id=current,
            reason="current",
            score=round(max(0, current_score) * 1000),
        )
        selected = {current}
        for score, doc in scored:
            if score <= 0:
                continue
            if doc.slide_id not in selected and doc.slide_id in destinations:
                selected.add(doc.slide_id)
                result.append(
                    Candidate(
                        kind="slide",
                        slide_id=doc.slide_id,
                        reason="topic",
                        score=round(score * 1000),
                    )
                )
        for score, doc in current_slide_action_documents(scored, current, 8 - len(result)):
            kind = cast(Literal["zoom_region", "highlight_region"], doc.kind)
            result.append(
                Candidate(
                    kind=kind,
                    slide_id=doc.slide_id,
                    action_id=doc.action_id,
                    reason="purpose",
                    score=round(score * 1000),
                )
            )
        return tuple(result)
