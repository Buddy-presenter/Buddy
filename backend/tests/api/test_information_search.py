import asyncio
import json

import httpx
import pytest

from buddy_api.agent_provider import AgentSettings
from buddy_api.information_search import (
    InformationSearchFailure,
    InformationSearchRequest,
    InformationSearchService,
)


def test_information_search_uses_bounded_results_and_returns_grounded_sources():
    calls = []
    reserved = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request, body))
        return httpx.Response(
            200,
            json={
                "finish_reason": "COMPLETE",
                "message": {"content": [{"type": "text", "text": "A short answer."}]},
            },
        )

    async def reserve() -> None:
        reserved.append(True)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            service = InformationSearchService(
                AgentSettings("server-only-test-key"),
                client,
                search=lambda query: [
                    {
                        "title": "Trusted source",
                        "href": "https://example.org/facts",
                        "body": "A bounded supporting snippet.",
                    }
                ],
                reserve_model_call=reserve,
            )
            result = await service.answer("What does opportunity cost mean?")

        assert result.answer == "A short answer."
        assert result.sources[0].url == "https://example.org/facts"
        assert reserved == [True]
        request, body = calls[0]
        assert request.headers["Authorization"] == "Bearer server-only-test-key"
        assert body["model"] == "command-a-03-2025"
        assert body["max_tokens"] == 180
        assert "A bounded supporting snippet." in body["messages"][-1]["content"]

    asyncio.run(scenario())


def test_information_search_rejects_invalid_queries_and_unsafe_sources():
    with pytest.raises(ValueError):
        InformationSearchRequest(query=" ")

    def respond(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Cohere must not be called without a safe source")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            service = InformationSearchService(
                AgentSettings("server-only-test-key"),
                client,
                search=lambda _query: [
                    {"title": "Unsafe", "href": "http://example.org/", "body": "text"}
                ],
            )
            with pytest.raises(InformationSearchFailure) as exc:
                await service.answer("a normal question")
            assert exc.value.code == "no_results"

    asyncio.run(scenario())


def test_information_search_hides_provider_error_details():
    def fail_search(_query: str) -> list[dict[str, object]]:
        raise RuntimeError("private provider detail and query")

    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            service = InformationSearchService(
                AgentSettings("server-only-test-key"), client, search=fail_search
            )
            with pytest.raises(InformationSearchFailure) as exc:
                await service.answer("a normal question")
            assert exc.value.code == "search_unavailable"
            assert "private provider detail" not in str(exc.value)

    asyncio.run(scenario())
