"""Persistent presentation indexes; ephemeral queries; bounded Cohere usage."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

if TYPE_CHECKING:
    from buddy_api.presentation_candidates import Candidate

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from buddy_api.analysis.contracts import AnalysisResult
from buddy_api.embeddings import SPACE, CohereEmbeddings, EmbeddingFailure, normalize
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import CandidateIndex, EmbeddingQuota


@dataclass(frozen=True)
class Document:
    slide_id: str
    action_id: str | None
    kind: str
    text: str = field(repr=False)


def documents(analysis: AnalysisResult) -> tuple[Document, ...]:
    result = []
    for position, slide in enumerate(analysis.slides, 1):
        if slide.summary.status == "available" and slide.summary.text:
            result.append(
                Document(slide.slide_id, None, "slide", f"Slide {position}. " + slide.summary.text)
            )
        for action in slide.actions:
            if action.usable and action.state == "validated" and action.semantic_verified:
                result.append(
                    Document(
                        slide.slide_id,
                        action.action.instance_id,
                        action.action.type,
                        f"Slide {position}. " + action.action.purpose,
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


class SemanticCandidates:
    def __init__(self, database: Database, provider: CohereEmbeddings, call_limit: int = 750):
        self.database, self.provider, self.call_limit = database, provider, call_limit
        self._preparing: set[UUID] = set()
        self._queries: set[UUID] = set()
        self._last_call: dict[UUID, float] = {}
        self._cache: dict[tuple[UUID, UUID, str], tuple[float, tuple[tuple[float, ...], ...]]] = {}

    async def _embed(self, texts: list[str], kind: str) -> tuple[tuple[float, ...], ...]:
        key_hash = hashlib.sha256(self.provider.settings.api_key.encode()).hexdigest()
        month = datetime.now(UTC).strftime("%Y-%m")
        async with self.database.sessions() as db, db.begin():
            statement = insert(EmbeddingQuota).values(key_hash=key_hash, month=month, calls=1)
            reservation = statement.on_conflict_do_update(
                index_elements=[EmbeddingQuota.key_hash, EmbeddingQuota.month],
                set_={"calls": EmbeddingQuota.calls + 1},
                where=EmbeddingQuota.calls < self.call_limit,
            ).returning(EmbeddingQuota.calls)
            if await db.scalar(reservation) is None:
                raise EmbeddingFailure("embedding_budget_exhausted")
        return await self.provider.embed(texts, "document" if kind == "document" else "query")

    async def prepare(self, analysis: AnalysisResult, owner: UUID) -> bool:
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
        self, analysis: AnalysisResult, owner: UUID, current: str, latest: str, recent: str
    ) -> tuple["Candidate", ...]:
        from buddy_api.presentation_candidates import Candidate, resolve

        base = resolve(analysis, current, "", latest)
        if any(candidate.reason == "explicit_navigation" for candidate in base):
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
        cache_key = (
            owner,
            analysis.id,
            hashlib.sha256((latest + "\0" + recent).encode()).hexdigest(),
        )
        now = time.monotonic()
        self._cache = {k: v for k, v in self._cache.items() if now - v[0] <= 5}
        self._last_call = {k: v for k, v in self._last_call.items() if now - v <= 2}
        cached = self._cache.get(cache_key)
        if cached:
            queries = cached[1]
        else:
            if owner in self._queries or owner in self._last_call or len(self._queries) >= 16:
                raise EmbeddingFailure("embedding_query_busy")
            self._queries.add(owner)
            self._last_call[owner] = now
            try:
                # Both query perspectives use one provider call. No extra LLM pass.
                queries = await self._embed([latest, recent[-2000:]], "query")
            finally:
                self._queries.discard(owner)
            if len(self._cache) >= 128:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = (time.monotonic(), queries)
        scored = [
            (
                0.8 * sum(a * b for a, b in zip(vector, queries[0], strict=True))
                + 0.2 * sum(a * b for a, b in zip(vector, queries[1], strict=True)),
                doc,
            )
            for doc, vector in zip(items, vectors, strict=True)
        ]
        scored.sort(key=lambda item: (-item[0], item[1].slide_id, item[1].action_id or ""))
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
            if doc.slide_id not in selected and len(selected) < 5:
                selected.add(doc.slide_id)
                result.append(
                    Candidate(
                        kind="slide",
                        slide_id=doc.slide_id,
                        reason="topic",
                        score=round(score * 1000),
                    )
                )
        for score, doc in scored:
            if len(result) >= 8:
                break
            if score > 0 and doc.slide_id in selected and doc.action_id:
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
