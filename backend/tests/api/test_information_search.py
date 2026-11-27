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


def spoken_service(query_text, calls):
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body["max_tokens"] == 40:
            return httpx.Response(
                200, json={"message": {"content": [{"type": "text", "text": query_text}]}}
            )
        return httpx.Response(
            200,
            json={
                "finish_reason": "COMPLETE",
                "message": {"content": [{"type": "text", "text": "A short answer."}]},
            },
        )

    return respond


def test_spoken_request_is_turned_into_search_words():
    # Jev cannot return text, so the search words are written from the sentence.
    calls: list[dict] = []
    searched: list[str] = []

    async def scenario() -> str:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(spoken_service('"population of Lebanon"', calls))
        ) as client:
            service = InformationSearchService(
                AgentSettings("server-only-test-key"),
                client,
                search_images=lambda query, limit: [],
                search=lambda query: (
                    searched.append(query)
                    or [{"title": "T", "href": "https://example.org/x", "body": "B"}]
                ),
            )
            return await service.query_from_speech(
                "Okay, can we quickly check how many people live in Lebanon?",
                "Our Beirut office grew.",
                images=False,
            )

    assert asyncio.run(scenario()) == "population of Lebanon"
    prompt = json.loads(calls[0]["messages"][-1]["content"])
    assert prompt == {
        "wants": "information",
        "earlier_sentence": "Our Beirut office grew.",
        "latest_sentence": "Okay, can we quickly check how many people live in Lebanon?",
    }


@pytest.mark.parametrize("reply", ["NONE", "none.", "", "x" * 200])
def test_spoken_request_without_a_usable_query_finds_nothing(reply):
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(spoken_service(reply, []))
        ) as client:
            service = InformationSearchService(AgentSettings("k"), client)
            await service.query_from_speech("Thanks everyone.", None, images=True)

    with pytest.raises(InformationSearchFailure) as caught:
        asyncio.run(scenario())
    assert caught.value.code == "no_results"


def test_spoken_flag_and_context_are_bounded():
    values = InformationSearchRequest.model_validate(
        {"query": "let me look up EBITDA", "spoken": True, "context": "Costs rose."}
    )
    assert values.spoken and values.context == "Costs rose."
    assert not InformationSearchRequest.model_validate({"query": "EBITDA"}).spoken
    with pytest.raises(ValueError):
        InformationSearchRequest.model_validate({"query": "EBITDA", "context": "x" * 601})


@pytest.mark.parametrize("first", ["error", "empty"])
def test_web_search_retries_once_when_the_search_provider_fails(first):
    attempts: list[str] = []

    def search(query):
        attempts.append(query)
        if len(attempts) == 1:
            if first == "error":
                raise RuntimeError("No results found.")
            return []
        return [{"title": "T", "href": "https://example.org/x", "body": "B"}]

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(spoken_service("unused", []))
        ) as client:
            service = InformationSearchService(
                AgentSettings("k"), client, search=search, search_images=lambda q, n: []
            )
            return await service.answer("EBITDA meaning")

    assert asyncio.run(scenario()).answer == "A short answer."
    assert attempts == ["EBITDA meaning", "EBITDA meaning"]


def test_web_search_gives_up_after_the_retry():
    def search(query):
        raise RuntimeError("down")

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(spoken_service("unused", []))
        ) as client:
            service = InformationSearchService(
                AgentSettings("k"), client, search=search, search_images=lambda q, n: []
            )
            await service.answer("EBITDA meaning")

    with pytest.raises(InformationSearchFailure) as caught:
        asyncio.run(scenario())
    assert caught.value.code == "search_unavailable"


@pytest.mark.parametrize("failure", ["timeout", "500", "garbled"])
def test_spoken_search_falls_back_to_the_sentence_when_the_rewrite_fails(failure):
    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if failure == "500":
            return httpx.Response(500)
        return httpx.Response(200, json={"message": {}})

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            service = InformationSearchService(AgentSettings("k"), client)
            return await service.query_from_speech("Who invented the internet?", None, images=False)

    assert asyncio.run(scenario()) == "Who invented the internet?"


def test_spoken_search_rate_limit_is_reported():
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(429))
        ) as client:
            service = InformationSearchService(AgentSettings("k"), client)
            await service.query_from_speech("Who invented the internet?", None, images=False)

    with pytest.raises(InformationSearchFailure) as caught:
        asyncio.run(scenario())
    assert caught.value.code == "rate_limited"
