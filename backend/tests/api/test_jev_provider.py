"""Jev first, Cohere when unsure (#243)."""

import asyncio
import json

import httpx
import pytest

from buddy_api.agent_provider import (
    DECIDED_BY,
    JEV_MODEL,
    MODEL,
    AgentFailure,
    AgentSettings,
    ConfidentFirstProvider,
    JevDecisionProvider,
    Selection,
)

PASSAGE = {
    "segments": ["Revenue came in at 48 million."],
    "options": [{"id": "c0", "tool": "no_action"}, {"id": "c1", "tool": "highlight_region"}],
}


def jev(handler):
    return JevDecisionProvider(
        "test-key", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def answer(choice, probabilities):
    return httpx.Response(
        200,
        json={
            "answers": {
                "action": {"type": "choice", "choice": choice, "probabilities": probabilities}
            }
        },
    )


def test_jev_sends_one_choice_question_and_maps_the_answer():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return answer("c1", {"c0": 0.1, "c1": 0.9})

    selection = asyncio.run(jev(handler).select(PASSAGE))
    assert selection.choice == "c1" and selection.reason == "topic"
    assert selection.confidence == 0.9 and selection.evidence == (0,)
    assert seen["model"] == JEV_MODEL and "options" not in seen["state"]
    assert set(seen["questions"]["action"]["criteria"]) == {"c0", "c1"}


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429), "rate_limited"),
        (httpx.Response(500), "provider_unavailable"),
        (httpx.Response(200, json={"answers": {}}), "invalid_output"),
        (answer("c7", {"c7": 1.0}), "invalid_output"),  # Not an offered option.
    ],
)
def test_jev_failures_are_finite_reasons(response, code):
    with pytest.raises(AgentFailure) as raised:
        asyncio.run(jev(lambda request: response).select(PASSAGE))
    assert raised.value.code == code


class Fixed:
    def __init__(self, result):
        self.result, self.calls = result, 0

    async def select(self, passage):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        DECIDED_BY.set("fixed")
        return self.result

    async def aclose(self):
        pass


def pick(choice, confidence):
    return Selection(choice=choice, reason="topic", confidence=confidence, evidence=(0,))


@pytest.mark.parametrize(
    ("fast_result", "careful_calls"),
    [
        (pick("c1", 0.9), 0),  # Sure enough: Jev decides.
        (pick("c1", 0.6), 1),  # Unsure: Cohere decides.
        (AgentFailure("deadline"), 1),  # Jev failed: Cohere decides.
    ],
)
def test_confident_first_falls_back_when_unsure_or_failed(fast_result, careful_calls):
    fast, careful = Fixed(fast_result), Fixed(pick("c0", 0.95))
    provider = ConfidentFirstProvider(fast, careful, 0.7)
    result = asyncio.run(provider.select(PASSAGE))
    assert careful.calls == careful_calls
    assert result == (careful.result if careful_calls else fast_result)


def test_jev_settings_are_optional_and_validated():
    assert AgentSettings("cohere").jev_api_key is None
    assert AgentSettings("cohere", jev_api_key="k", jev_confidence=0.8).jev_confidence == 0.8
    with pytest.raises(ValueError):
        AgentSettings("cohere", jev_api_key="bad key")
    with pytest.raises(ValueError):
        AgentSettings("cohere", jev_confidence=0.2)
    assert DECIDED_BY.get() == MODEL
