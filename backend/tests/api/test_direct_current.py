"""The opt-in complete menu skips retrieval, not authorization or execution guards."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from backend.tests.api.test_authorization import private_api as private_api
from backend.tests.api.test_authorization import ready_session_api as ready_session_api
from backend.tests.api.test_presentation_agent import agent_api as agent_api
from backend.tests.api.test_presentation_agent import payload
from backend.tests.api.test_presentation_agent import (
    test_proposal_fallback_and_inflight_state_changes as check_fallback,
)
from backend.tests.api.test_presentation_candidates import analysis

from buddy_api.agent_provider import AgentFailure, AgentSettings, JevDecisionProvider, Selection
from buddy_api.analysis.contracts import Region
from buddy_api.app import decision_providers
from buddy_api.config import Settings
from buddy_api.presentation_agent import AgentInput, checked_selection, direct_current_options


def region(identifier, **changes):
    return SimpleNamespace(
        usable=True,
        state="validated",
        semantic_verified=True,
        action=SimpleNamespace(
            instance_id=identifier,
            type="highlight_region",
            purpose="Revenue " + identifier,
            params=SimpleNamespace(region=Region(x=1, y=2, width=30, height=40)),
        ),
        **changes,
    )


def test_complete_menu_keeps_current_actions_and_all_navigation_only():
    deck = analysis(*(f"Slide summary {i}" for i in range(12)))
    deck.slides[3].actions = tuple(region(str(i)) for i in range(18)) + (
        SimpleNamespace(usable=False, state="rejected"),
        SimpleNamespace(
            usable=True,
            state="validated",
            semantic_verified=False,
            action=SimpleNamespace(type="zoom_region"),
        ),
    )
    deck.slides[5].actions = (region("foreign-slide"),)
    source = AgentInput(deck, "slide-3", "zoom_region", "hash", Region(x=1, y=2, width=3, height=4))
    _, targets, descriptions = direct_current_options(source)
    assert len(targets) == 31 and len(descriptions) == 31
    assert targets[0].tool == "no_action"
    assert {t.action_id for t in targets if t.action_id} == {str(i) for i in range(18)}
    assert {
        t.slide_id for t in targets if t.tool in {"go_to_slide", "next_slide", "previous_slide"}
    } == {f"slide-{i}" for i in range(12) if i != 3}
    assert sum(t.tool == "reset_view" for t in targets) == 1
    assert descriptions[-1]["id"] == "c30"
    selected = checked_selection(
        Selection(choice="c30", reason="topic", confidence=0.8, evidence=(0,)), targets, 1
    )
    assert selected == targets[-1]


def test_oversized_complete_menu_is_not_silently_truncated():
    source = AgentInput(analysis(*(str(i) for i in range(256))), "slide-0", "slide", "hash")
    with pytest.raises(AgentFailure):
        direct_current_options(source)


@pytest.mark.parametrize("choice", ["c255", "c999", "c-1", "c01", "arbitrary"])
def test_choice_identifiers_remain_bounded(choice):
    with pytest.raises(ValueError):
        Selection(choice=choice, reason="topic", confidence=0.9, evidence=(0,))


def test_prototype_requires_jev_and_constructs_no_cohere_client():
    settings = AgentSettings("", jev_api_key="test-key", candidate_mode="direct_current")
    default, choices = decision_providers(settings, None)
    assert isinstance(default, JevDecisionProvider) and set(choices) == {"jev"}
    asyncio.run(default.aclose())
    for changes in (
        {"jev_api_key": None},
        {"decision_provider": "cohere"},
        {"candidate_mode": "unknown"},
    ):
        with pytest.raises(ValueError):
            replace(settings, **changes)


def test_environment_enables_direct_mode_without_cohere_or_embeddings():
    from buddy_api.app import create_app

    config = Settings.from_environment(
        {
            "BUDDY_ENVIRONMENT": "development",
            "BUDDY_AGENT_ENABLED": "true",
            "BUDDY_AGENT_CANDIDATE_MODE": "direct_current",
            "BUDDY_JEV_API_KEY": "synthetic-key",
        }
    )
    assert config.embeddings is None and config.agent.api_key == ""
    app = create_app(config)
    assert app.state.semantic_candidates is None
    assert set(app.state.presentation_agent.choices) == {"jev"}
    with pytest.raises(AgentFailure):
        app.state.presentation_agent.provider_for("cohere")
    asyncio.run(app.state.presentation_agent.aclose())


def test_jev_can_choose_beyond_the_old_ten_option_limit():
    passage = dict(
        segments=["Show the result"],
        current_view={},
        options=[
            dict(
                id=f"c{i}",
                tool="no_action" if i == 0 else "highlight_region",
                content="Action purpose: Result",
            )
            for i in range(20)
        ],
    )

    def response(request):
        assert (
            len(__import__("json").loads(request.content)["questions"]["action"]["criteria"]) == 20
        )
        return httpx.Response(
            200,
            json={
                "answers": {
                    "action": {"choice": "c19", "probabilities": {"c19": 0.85}},
                    "presenting_now": {"noul": 0.95},
                }
            },
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            result = await JevDecisionProvider("test", client).select(passage)
            assert result.choice == "c19" and result.evidence == (0,)

    asyncio.run(check())


@pytest.mark.postgres
def test_real_session_prepare_and_agent_do_not_touch_embeddings(agent_api):
    client, ids, provider = agent_api
    runtime = client.app.state.presentation_agent
    runtime.settings = replace(
        runtime.settings, jev_api_key="prototype", candidate_mode="direct_current"
    )

    class Forbidden:
        async def prepare(self, *args, **kwargs):
            raise AssertionError("Prototype must not prepare embeddings")

        async def retrieve(self, *args, **kwargs):
            raise AssertionError("Prototype must not retrieve embeddings")

    client.app.state.semantic_candidates = Forbidden()
    path = f"/api/v1/sessions/{ids['sessions']}"
    assert (
        client.post(path + "/candidates/prepare", headers={"Authorization": "Bearer owner"}).json()[
            "resolver_version"
        ]
        == "direct-current-v1"
    )
    response = client.post(
        path + "/agent", headers={"Authorization": "Bearer owner"}, json=payload()
    )
    assert response.status_code == 200 and len(provider.calls) == 1
    assert response.json()["target"]["tool"] == "no_action"
    assert (
        client.post(
            path + "/agent", headers={"Authorization": "Bearer other"}, json=payload()
        ).status_code
        == 403
    )
    assert (
        client.post(
            path + "/candidates/prepare", headers={"Authorization": "Bearer other"}
        ).status_code
        == 403
    )


@pytest.mark.postgres
@pytest.mark.parametrize("mode", ["stale", "source_changed", "failure", "malformed", "busy"])
def test_direct_mode_retains_fallback_and_inflight_source_guards(agent_api, mode):
    client, _, _ = agent_api
    runtime = client.app.state.presentation_agent
    runtime.settings = replace(
        runtime.settings, jev_api_key="prototype", candidate_mode="direct_current"
    )
    check_fallback(agent_api, mode)
