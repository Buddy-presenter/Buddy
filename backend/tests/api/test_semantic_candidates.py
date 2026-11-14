"""Semantic provider boundaries and actual persisted index/quota/API behavior."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from backend.tests.api.test_authorization import private_api as private_api
from backend.tests.api.test_authorization import ready_session_api as ready_session_api
from backend.tests.api.test_presentation_candidates import payload
from backend.tests.support.database import database_settings as database_settings
from sqlalchemy import select

from buddy_api.embeddings import DIMENSIONS, CohereEmbeddings, EmbeddingFailure, EmbeddingSettings
from buddy_api.persistence.models import EmbeddingQuota
from buddy_api.semantic_candidates import SemanticCandidates, combined_context


def test_combined_query_preserves_references_and_bounds_old_context():
    passage = "The application stores addresses and payment tokens. These need protection."
    assert combined_context(passage) == passage
    latest = "This is the completed latest segment."
    bounded = combined_context("older context " * 600 + latest)
    assert len(bounded) <= 4096 and bounded.endswith(latest)
    assert not bounded.startswith("lder")
    assert combined_context(" \n" + passage + " \n") == passage
    assert len(combined_context("界" * 5000)) == 4096


def vector(axis):
    return [float(i == axis) for i in range(DIMENSIONS)]


def test_provider_selects_pro_fast_and_rejects_private_failure_bodies():
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200, json={"embeddings": {"float": [vector(0) for _ in body["texts"]]}}
        )

    provider = CohereEmbeddings(EmbeddingSettings("synthetic-secret"), httpx.MockTransport(respond))

    async def check():
        assert len(await provider.embed(["Generated slide"], "document")) == 1
        assert len(await provider.embed(["Generated speech"], "query")) == 1

    asyncio.run(check())
    assert [c["model"] for c in calls] == ["embed-v5.0-pro", "embed-v5.0-fast"]
    assert [c["input_type"] for c in calls] == ["search_document", "search_query"]
    assert "synthetic-secret" not in repr(provider.settings)


@pytest.mark.parametrize("bad", [[], [[0.0] * 512], [[float("nan")] * 512], [[1.0] * 511]])
def test_provider_rejects_malformed_vectors(bad):
    provider = CohereEmbeddings(
        EmbeddingSettings("synthetic"),
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=json.dumps({"embeddings": {"float": bad}}, allow_nan=True).encode()
            )
        ),
    )
    with pytest.raises(EmbeddingFailure):
        asyncio.run(provider.embed(["Generated example"], "query"))


class FakeProvider:
    settings = SimpleNamespace(api_key="synthetic-semantic-key")

    def __init__(self):
        self.calls = []
        self.inputs = []

    async def aclose(self):
        pass

    async def embed(self, texts, kind):
        self.calls.append((kind, len(texts)))
        self.inputs.append(list(texts))
        if kind == "document":
            return tuple(tuple(vector(i)) for i, _ in enumerate(texts))
        return tuple(tuple(vector(1)) for _ in texts)


@pytest.mark.postgres
def test_index_persists_queries_cache_and_budget_is_enforced(ready_session_api):
    client, ids, owner, other, _ = ready_session_api
    provider = FakeProvider()
    engine = SemanticCandidates(client.app.state.database, provider, call_limit=2)
    client.app.state.semantic_candidates = engine
    path = f"/api/v1/sessions/{ids['sessions']}"
    headers = {"Authorization": "Bearer owner"}
    # No lazy document embedding on the live query path.
    assert (
        client.post(
            path + "/candidates", headers=headers, json=payload(text="different phrasing")
        ).status_code
        == 503
    )
    first = client.post(path + "/candidates/prepare", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["created"]
    assert not client.post(path + "/candidates/prepare", headers=headers).json()["created"]
    context = "The application stores addresses and payment tokens. These need protection."
    request = payload(text=context, latest_utterance="These need protection.")
    result = client.post(path + "/candidates", headers=headers, json=request)
    assert result.status_code == 200, result.text
    assert result.json()["resolver_version"] == "cohere-embed5-v2"
    assert any(c["reason"] == "topic" for c in result.json()["candidates"])
    # Cached query does not spend a third call; quota is persistent across engines.
    assert client.post(path + "/candidates", headers=headers, json=request).status_code == 200
    assert provider.calls == [("document", 2), ("query", 1)]
    assert provider.inputs[-1] == [context]
    new_engine = SemanticCandidates(client.app.state.database, provider, call_limit=2)
    client.app.state.semantic_candidates = new_engine
    exhausted = client.post(path + "/candidates", headers=headers, json=payload(text="new query"))
    assert exhausted.status_code == 503 and "embedding_budget_exhausted" in exhausted.text
    assert (
        client.post(
            path + "/candidates/prepare", headers={"Authorization": "Bearer other"}
        ).status_code
        == 403
    )
    # Exact navigation needs neither a query call nor a budget exception.
    navigation = client.post(path + "/candidates", headers=headers, json=payload(text="next slide"))
    assert navigation.status_code == 200, navigation.text
    assert any(c["reason"] == "explicit_navigation" for c in navigation.json()["candidates"])
    # At the last slide an explicit next command still avoids the provider and
    # cannot turn the older topic into a spurious non-linear destination.
    from buddy_api.analysis.store import Analyses

    async def at_boundary():
        async with client.app.state.database.sessions() as db:
            analysis = await Analyses(db).read(ids["analyses"], owner_id=owner)
        return await new_engine.retrieve(
            analysis, owner, analysis.slides[-1].slide_id, "next slide", "older topic next slide"
        )

    boundary = client.portal.call(at_boundary)
    assert len(boundary) == 2 and all(c.reason in {"stay", "current"} for c in boundary)
    assert provider.calls == [("document", 2), ("query", 1)]


@pytest.mark.postgres
def test_quota_reservations_are_atomic_across_concurrent_requests(ready_session_api):
    client, *_ = ready_session_api
    provider = FakeProvider()
    engine = SemanticCandidates(client.app.state.database, provider, call_limit=1)

    async def spend():
        results = await asyncio.gather(
            *(engine._embed(["Synthetic query"], "query") for _ in range(3)),
            return_exceptions=True,
        )
        async with client.app.state.database.sessions() as db:
            counts = (await db.scalars(select(EmbeddingQuota.calls))).all()
        return results, counts

    results, counts = client.portal.call(spend)
    assert sum(isinstance(r, EmbeddingFailure) for r in results) == 2
    assert counts == [1]
    assert provider.calls == [("query", 1)]


def test_rate_limit_response_does_not_retry_or_leak_provider_content():
    calls = []

    def respond(request):
        calls.append(1)
        return httpx.Response(429, json={"message": "private provider response"})

    provider = CohereEmbeddings(EmbeddingSettings("synthetic"), httpx.MockTransport(respond))

    async def check():
        try:
            await provider.embed(["Synthetic input"], "query")
        finally:
            await provider.aclose()

    with pytest.raises(EmbeddingFailure, match="^embedding_rate_limited$"):
        asyncio.run(check())
    assert calls == [1]
