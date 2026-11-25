"""Jev decides by default; Cohere stays selectable (#243)."""

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
    CohereDecisionProvider,
    ConfidentFirstProvider,
    JevDecisionProvider,
    Selection,
    jev_request,
)
from buddy_api.app import decision_provider

PASSAGE = {
    "segments": ["Earlier context.", "Revenue came in at 48 million."],
    "latest_segment": 1,
    "current_view": {"slide_number": 4, "summary": "Q4 results"},
    "options": [
        {"id": "c0", "tool": "no_action", "content": "Keep the current view"},
        {
            "id": "c1",
            "tool": "highlight_region",
            "slide_number": 4,
            "content": "Slide context: Q4\nAction purpose: Emphasize revenue\nShows: Revenue $48M",
        },
        {"id": "c2", "tool": "next_slide", "slide_number": 5, "content": "Customers"},
    ],
}


def jev(handler, **bounds):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JevDecisionProvider("test-key", client, **bounds)


def answer(choice, probability, presenting=0.9):
    return httpx.Response(
        200,
        json={
            "answers": {
                "action": {
                    "type": "choice",
                    "choice": choice,
                    "probabilities": {choice: probability},
                },
                "presenting_now": {"type": "noul", "noul": presenting},
            }
        },
    )


def test_request_uses_plain_language_criteria_and_two_questions():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return answer("c1", 0.9)

    selection = asyncio.run(jev(handler).select(PASSAGE))
    assert (selection.choice, selection.reason, selection.evidence) == ("c1", "topic", (1,))
    assert seen["model"] == JEV_MODEL
    assert seen["state"]["latest_sentence"] == "Revenue came in at 48 million."
    assert seen["state"]["earlier_sentences"] == ["Earlier context."]
    criteria = seen["questions"]["action"]["criteria"]
    assert criteria["c0"].startswith("Do nothing")
    assert criteria["c1"] == (
        "Highlight this item on the current slide: Emphasize revenue. It shows: Revenue $48M."
    )
    assert criteria["c2"] == "Go to the next slide: Customers"
    assert seen["questions"]["presenting_now"]["type"] == "noul"


@pytest.mark.parametrize(
    ("choice", "probability", "presenting", "expected"),
    [
        ("c1", 0.6, 0.9, ("c1", "topic")),  # At the 60% bar: act.
        ("c1", 0.59, 0.9, ("c0", "uncertain")),  # Below it: do nothing.
        ("c1", 0.95, 0.1, ("c0", "stay")),  # Recalled/hypothetical/preview: never act.
        ("c0", 0.8, 0.9, ("c0", "stay")),
    ],
)
def test_jev_acts_only_when_sure_and_presenting_now(choice, probability, presenting, expected):
    selection = asyncio.run(
        jev(lambda request: answer(choice, probability, presenting)).select(PASSAGE)
    )
    assert (selection.choice, selection.reason) == expected


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429), "rate_limited"),
        (httpx.Response(500), "provider_unavailable"),
        (httpx.Response(200, json={"answers": {}}), "invalid_output"),
        (answer("c7", 1.0), "invalid_output"),  # Not an offered option.
        (answer("c1", 1.5), "invalid_output"),  # Not a probability.
    ],
)
def test_jev_failures_are_finite_reasons(response, code):
    with pytest.raises(AgentFailure) as raised:
        asyncio.run(jev(lambda request: response).select(PASSAGE))
    assert raised.value.code == code


def test_jev_request_rejects_malformed_passages():
    with pytest.raises(ValueError):
        jev_request({**PASSAGE, "segments": []})


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
        (pick("c1", 0.5), 1),  # Unsure: Cohere decides.
        (AgentFailure("deadline"), 1),  # Jev failed: Cohere decides.
    ],
)
def test_confident_first_falls_back_when_unsure_or_failed(fast_result, careful_calls):
    fast, careful = Fixed(fast_result), Fixed(pick("c0", 0.95))
    provider = ConfidentFirstProvider(fast, careful, 0.6)
    result = asyncio.run(provider.select(PASSAGE))
    assert careful.calls == careful_calls
    assert result == (careful.result if careful_calls else fast_result)


@pytest.mark.parametrize(
    ("choice", "jev_key", "kind"),
    [
        ("jev", "k", JevDecisionProvider),
        ("jev", None, CohereDecisionProvider),  # No Jev key: Cohere decides.
        ("cohere", "k", CohereDecisionProvider),
        ("jev+cohere", "k", ConfidentFirstProvider),
    ],
)
def test_decision_provider_defaults_to_jev_and_keeps_cohere(choice, jev_key, kind):
    settings = AgentSettings("cohere", jev_api_key=jev_key, decision_provider=choice)
    provider = decision_provider(settings, None)
    assert isinstance(provider, kind)
    asyncio.run(provider.aclose())


def test_jev_settings_are_optional_and_validated():
    settings = AgentSettings("cohere")
    assert settings.jev_api_key is None
    assert (settings.decision_provider, settings.jev_confidence) == ("jev", 0.6)
    with pytest.raises(ValueError):
        AgentSettings("cohere", jev_api_key="bad key")
    with pytest.raises(ValueError):
        AgentSettings("cohere", jev_confidence=0.2)
    with pytest.raises(ValueError):
        AgentSettings("cohere", decision_provider="groq")
    with pytest.raises(ValueError):
        JevDecisionProvider("k", threshold=0.4)
    assert DECIDED_BY.get() == MODEL


@pytest.mark.parametrize("unfinished", [False, True])
def test_unfinished_sentences_are_marked_for_jev(unfinished):
    # #248: the desktop sends the stable start of a sentence still being spoken.
    from buddy_api.agent_provider import JEV_INSTRUCTIONS, JEV_UNFINISHED

    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return answer("c0", 0.9)

    passage = (
        {**PASSAGE, "latest_segment_status": "provisional stable prefix; speech is ongoing"}
        if unfinished
        else PASSAGE
    )
    asyncio.run(jev(handler).select(passage))
    instructions = seen["questions"]["action"]["instructions"]
    assert ("latest_sentence_status" in seen["state"]) is unfinished
    assert instructions == (
        JEV_INSTRUCTIONS + " " + JEV_UNFINISHED if unfinished else JEV_INSTRUCTIONS
    )


def test_presenter_choice_selects_the_decider():
    # #245: the desktop sends decision_model; older clients send nothing.
    from buddy_api.app import decision_providers
    from buddy_api.presentation_agent import AgentRuntime

    settings = AgentSettings("cohere", jev_api_key="k")
    default, choices = decision_providers(settings, None)
    runtime = AgentRuntime(settings, default, choices)
    assert isinstance(runtime.provider_for(None), JevDecisionProvider)
    assert isinstance(runtime.provider_for("jev"), JevDecisionProvider)
    assert isinstance(runtime.provider_for("cohere"), CohereDecisionProvider)
    asyncio.run(runtime.aclose())
    # Without a Jev key, every choice decides with Cohere.
    default, choices = decision_providers(AgentSettings("cohere"), None)
    runtime = AgentRuntime(settings, default, choices)
    assert isinstance(runtime.provider_for("jev"), CohereDecisionProvider)
    asyncio.run(runtime.aclose())
