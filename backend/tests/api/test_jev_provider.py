"""Jev decides by default; Cohere stays selectable (#243)."""

import asyncio
import contextlib
import json

import httpx
import pytest

from buddy_api.agent_provider import (
    DECIDED_BY,
    DECISION_TRACE,
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


def test_opt_in_trace_captures_exact_jev_request_and_raw_probabilities():
    trace = {}
    token = DECISION_TRACE.set(trace)
    try:
        selection = asyncio.run(jev(lambda request: answer("c1", 0.83, 0.75)).select(PASSAGE))
    finally:
        DECISION_TRACE.reset(token)

    assert trace["provider_request"]["state"]["latest_sentence"] == PASSAGE["segments"][-1]
    assert trace["provider_request"]["questions"]["action"]["criteria"]["c1"].startswith(
        "Highlight this item"
    )
    assert trace["provider_response"] == {
        "action_choice": "c1",
        "action_probabilities": {"c1": 0.83},
        "presenting_now_probability": 0.75,
        "action_probability": 0.83,
        "threshold": 0.5,
        "presenting_floor": 0.4,
    }
    assert selection.choice == "c1"


@pytest.mark.parametrize(
    ("choice", "probability", "presenting", "expected"),
    [
        ("c1", 0.5, 0.9, ("c1", "topic")),  # At the 50% bar: act.
        ("c1", 0.49, 0.9, ("c0", "uncertain")),  # Below it: do nothing.
        ("c1", 0.95, 0.39, ("c0", "stay")),  # Below the presenting-now floor: abstain.
        ("c1", 0.95, 0.4, ("c1", "topic")),  # At the presenting-now floor: act.
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
    assert (settings.decision_provider, settings.jev_confidence) == ("jev", 0.5)
    assert settings.jev_presenting_floor == 0.4
    with pytest.raises(ValueError):
        AgentSettings("cohere", jev_api_key="bad key")
    with pytest.raises(ValueError):
        AgentSettings("cohere", jev_confidence=0.2)
    with pytest.raises(ValueError):
        AgentSettings("cohere", jev_presenting_floor=1.0)
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


def test_jev_search_options_only_when_the_desktop_asks():
    # Jev decides "search the web" alongside the slide options, never for old clients.
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["questions"]["action"]["criteria"])
        return answer("c0", 0.9)

    provider = jev(handler)
    asyncio.run(provider.select(PASSAGE))
    asyncio.run(provider.select({**PASSAGE, "web_search": True}))
    assert "search_web" not in seen[0] and "search_images" not in seen[0]
    assert {"search_web", "search_images"} <= set(seen[1])
    assert set(seen[1]) - {"search_web", "search_images"} == set(seen[0])


@pytest.mark.parametrize(
    ("choice", "probability", "presenting", "search", "reason"),
    [
        ("search_web", 0.9, 0.9, "web", "stay"),
        ("search_images", 0.8, 0.9, "images", "stay"),
        # A search request is not "presenting" a slide; the slide guard must not block it.
        ("search_web", 0.9, 0.05, "web", "stay"),
        ("search_web", 0.5, 0.9, "web", "stay"),
        ("search_web", 0.49, 0.9, None, "uncertain"),
    ],
)
def test_jev_search_choice_returns_no_slide_action(choice, probability, presenting, search, reason):
    provider = jev(lambda request: answer(choice, probability, presenting))
    selection = asyncio.run(provider.select({**PASSAGE, "web_search": True}))
    assert (selection.choice, selection.search, selection.reason) == ("c0", search, reason)
    assert selection.evidence == ()


def test_cohere_never_sees_the_search_flag():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "finish_reason": "COMPLETE",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "choice": "c0",
                                    "reason": "stay",
                                    "confidence": 0.9,
                                    "evidence": [],
                                }
                            ),
                        }
                    ]
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CohereDecisionProvider(AgentSettings("test-key"), client=client)
    with contextlib.suppress(AgentFailure):  # Only the request matters here.
        asyncio.run(provider.select({**PASSAGE, "web_search": True}))
    assert "Revenue came in" in seen["messages"][-1]["content"]
    assert "web_search" not in seen["messages"][-1]["content"]


def test_panel_options_only_while_the_search_panel_is_open():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((body["questions"]["action"], body["state"]))
        return answer("c0", 0.9)

    provider = jev(handler)
    asyncio.run(provider.select({**PASSAGE, "web_search": True}))
    asyncio.run(provider.select({**PASSAGE, "web_search": True, "search_panel_open": True}))
    closed, opened = seen
    assert not {"panel_close", "panel_scroll_down"} & set(closed[0]["criteria"])
    assert "on_screen" not in closed[1]
    assert {"panel_close", "panel_scroll_down", "panel_scroll_up"} <= set(opened[0]["criteria"])
    assert "search results panel" in opened[1]["on_screen"]
    assert "search results panel" in opened[0]["instructions"]


@pytest.mark.parametrize(
    ("choice", "probability", "panel"),
    [
        ("panel_close", 0.9, "close"),
        ("panel_scroll_down", 0.8, "scroll_down"),
        ("panel_scroll_up", 0.7, "scroll_up"),
        ("panel_close", 0.4, None),
    ],
)
def test_panel_choice_returns_no_slide_action(choice, probability, panel):
    provider = jev(lambda request: answer(choice, probability, 0.05))
    selection = asyncio.run(
        provider.select({**PASSAGE, "web_search": True, "search_panel_open": True})
    )
    assert (selection.choice, selection.panel, selection.search) == ("c0", panel, None)


def test_reset_has_its_own_wording_and_jev_knows_what_is_focused():
    # #257: reset read as "go to the slide already shown", so Jev never chose it.
    passage = {
        **PASSAGE,
        "current_view": {
            "slide_number": 4,
            "summary": "Q4 results",
            "focus": "zoom_region",
            "focused_purpose": "Show the revenue chart",
        },
        "options": [
            *PASSAGE["options"],
            {"id": "c3", "tool": "reset_view", "slide_number": 4, "content": "Q4 results"},
        ],
    }
    state, criteria = jev_request(passage)
    assert criteria["c3"].startswith("Zoom back out and remove the highlight")
    assert "Go to" not in criteria["c3"]
    assert state["current_view"] == "zoomed into Show the revenue chart"
    assert jev_request(PASSAGE)[0]["current_view"] == "the whole slide"


def test_future_plans_count_as_presenting_but_later_talk_previews_do_not():
    # #257: "we're launching the new website in January" was vetoed as a preview.
    from buddy_api.agent_provider import JEV_INSTRUCTIONS, JEV_PRESENTING_NOW

    assert "launching the new website in January" in JEV_PRESENTING_NOW
    assert "we'll get to that later" in JEV_PRESENTING_NOW
    assert "previews of later parts of this talk" in JEV_INSTRUCTIONS


def test_slide_image_is_sent_as_an_asset_but_never_traced():
    # #259: the slide on screen goes to Jev; diagnostics never hold the image bytes.
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return answer("c0", 0.9)

    trace = {}
    token = DECISION_TRACE.set(trace)
    try:
        asyncio.run(jev(handler).select({**PASSAGE, "slide_image": ("png", b"\x89PNG-bytes")}))
    finally:
        DECISION_TRACE.reset(token)
    reference = seen["body"]["state"]["current_slide"]["image"]
    asset = seen["body"]["assets"][reference]
    assert asset["format"] == "png" and asset["data_base64"] == "iVBORy1ieXRlcw=="
    assert seen["headers"]["typesafe-danger-zone-images"] == "true"
    assert "assets" not in trace["provider_request"]
    assert "image" not in trace["provider_request"]["state"]["current_slide"]


def test_no_image_means_no_asset_or_opt_in_header():
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return answer("c0", 0.9)

    asyncio.run(jev(handler).select(PASSAGE))
    assert "assets" not in seen["body"]
    assert "typesafe-danger-zone-images" not in seen["headers"]


def test_option_text_says_where_the_item_is():
    from buddy_api.agent_provider import jev_request

    passage = {
        **PASSAGE,
        "options": [
            *PASSAGE["options"][:1],
            {
                "id": "c1",
                "tool": "zoom_region",
                "slide_number": 4,
                "content": "Action purpose: Revenue chart\nShows: $48M\nPosition: top left",
            },
        ],
    }
    assert jev_request(passage)[1]["c1"].endswith("It is at the top left of the slide.")
