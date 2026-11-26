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
                search_images=lambda query, limit: [],
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
                search_images=lambda query, limit: [],
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
                AgentSettings("server-only-test-key"),
                client,
                search=fail_search,
                search_images=lambda query, limit: [],
            )
            with pytest.raises(InformationSearchFailure) as exc:
                await service.answer("a normal question")
            assert exc.value.code == "search_unavailable"
            assert "private provider detail" not in str(exc.value)

    asyncio.run(scenario())


IMAGE = {
    "title": "A red panda",
    "image": "https://example.org/full.jpg",
    "thumbnail": "https://tse.example.net/th?id=1",
    "url": "https://example.org/red-panda",
    "source": "example.org",
}


def test_image_search_returns_only_bounded_https_images_without_a_model_call():
    # #250: "show me images of ..." fills the panel's grid; no Cohere call is made.
    def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an image search must not call the model")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            service = InformationSearchService(
                AgentSettings("server-only-test-key"),
                client,
                search=lambda query: [],
                search_images=lambda query, limit: [
                    *[
                        {**IMAGE, "thumbnail": f"https://tse.example.net/th?id={i}"}
                        for i in range(20)
                    ],
                    {**IMAGE, "thumbnail": "http://insecure.example.net/x"},
                ],
            )
            result = await service.images("red pandas")
        assert result.answer is None and result.sources == ()
        assert len(result.images) == 9
        assert all(image.thumbnail.startswith("https://") for image in result.images)
        assert result.images[0].source_url == "https://example.org/red-panda"

    asyncio.run(scenario())


def test_image_search_without_usable_images_reports_no_results():
    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            service = InformationSearchService(
                AgentSettings("server-only-test-key"),
                client,
                search=lambda query: [],
                search_images=lambda query, limit: [{**IMAGE, "thumbnail": "http://x.example/y"}],
            )
            with pytest.raises(InformationSearchFailure) as failure:
                await service.images("red pandas")
            assert failure.value.code == "no_results"

    asyncio.run(scenario())


def test_web_answer_adds_a_few_images_and_survives_image_failure():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "finish_reason": "COMPLETE",
                "message": {"content": [{"type": "text", "text": "Short."}]},
            },
        )

    def failing_images(query, limit):
        raise RuntimeError("image provider down")

    result_holder = {}

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            source = [{"title": "T", "href": "https://example.org/a", "body": "B"}]
            with_images = InformationSearchService(
                AgentSettings("k"),
                client,
                search=lambda query: source,
                search_images=lambda query, limit: (
                    [IMAGE] * 1
                    + [{**IMAGE, "thumbnail": f"https://tse.example.net/{i}"} for i in range(8)]
                ),
            )
            result_holder["images"] = await with_images.answer("red pandas")
            without = InformationSearchService(
                AgentSettings("k"),
                client,
                search=lambda query: source,
                search_images=failing_images,
            )
            result_holder["failed"] = await without.answer("red pandas")

    asyncio.run(scenario())
    assert result_holder["images"].answer == "Short."
    assert len(result_holder["images"].images) == 3
    assert result_holder["failed"].answer == "Short." and result_holder["failed"].images == ()


def test_request_mode_is_closed():
    assert InformationSearchRequest(query="cats", mode="images").mode == "images"
    assert InformationSearchRequest(query="cats").mode == "web"
    with pytest.raises(ValueError):
        InformationSearchRequest(query="cats", mode="video")
