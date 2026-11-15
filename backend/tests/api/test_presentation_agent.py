"""Constrained provider output, real owner/session gates, budgets and post-call races."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from backend.tests.api.test_authorization import private_api as private_api
from backend.tests.api.test_authorization import ready_session_api as ready_session_api
from backend.tests.support.database import database_settings as database_settings
from sqlalchemy import select

from buddy_api.agent_provider import AgentFailure, AgentSettings, CohereDecisionProvider, Selection
from buddy_api.persistence.models import AgentQuota, PresentationSession
from buddy_api.presentation_agent import (
    AgentInput,
    AgentRequest,
    AgentRuntime,
    Target,
    checked_selection,
    options,
)
from buddy_api.presentation_candidates import Candidate


def payload(**changes):
    return {
        "protocol": "presentation-agent.v1",
        "expected_state_version": 4,
        "context_epoch": 1,
        "stream_id": str(uuid4()),
        "sequence": 1,
        "sent_at": datetime.now(UTC).isoformat(),
        "text": "private speech",
        "latest_utterance": "private speech",
        "segments": [{"sequence": 1, "text": "private speech"}],
        **changes,
    }


class Provider:
    def __init__(self):
        self.calls = []
        self.answer = Selection(choice="c0", reason="stay", confidence=0.9, evidence=())
        self.callback = None

    async def select(self, passage):
        self.calls.append(passage)
        if self.callback:
            await self.callback()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def aclose(self):
        pass


@pytest.fixture
def agent_api(ready_session_api):
    client, ids, *_ = ready_session_api
    provider = Provider()
    client.app.state.presentation_agent = AgentRuntime(AgentSettings("test-key"), provider)
    return client, ids, provider


def test_context_is_coherent_and_bounded():
    AgentRequest.model_validate(payload())
    for changes in (
        {"text": "different"},
        {"latest_utterance": "other"},
        {"sequence": 2},
        {"sent_at": "2026-01-01T00:00:00"},
        {
            "segments": [{"sequence": 1, "text": "a"}, {"sequence": 1, "text": "b"}],
            "text": "a b",
            "latest_utterance": "b",
        },
    ):
        with pytest.raises(ValueError):
            AgentRequest.model_validate(payload(**changes))
    # Speech order can differ from provider revision order; watermark is the maximum.
    AgentRequest.model_validate(
        payload(
            segments=[{"sequence": 2, "text": "a"}, {"sequence": 1, "text": "b"}],
            text="a b",
            latest_utterance="b",
            sequence=2,
        )
    )


def test_options_derive_navigation_and_only_usable_authoritative_regions():
    from buddy_api.analysis.contracts import Region

    action = SimpleNamespace(
        usable=True,
        state="validated",
        semantic_verified=True,
        action=SimpleNamespace(
            instance_id="region",
            type="zoom_region",
            purpose="Payment signature",
            params=SimpleNamespace(region=Region(x=1, y=2, width=3, height=4)),
        ),
    )
    analysis = SimpleNamespace(
        slides=tuple(
            SimpleNamespace(
                slide_id=str(i),
                summary=SimpleNamespace(text="Summary " + str(i)),
                actions=(action,) if i == 1 else (),
            )
            for i in range(4)
        )
    )
    candidates = tuple(
        Candidate(kind="slide", slide_id=str(i), reason="topic", score=1) for i in range(4)
    )
    candidates += (
        Candidate(kind="zoom_region", slide_id="1", action_id="region", reason="purpose", score=1),
    )
    source = AgentInput(analysis, "1", "zoom_region", "hash")
    targets, descriptions = options(source, candidates)
    assert [t.tool for t in targets] == [
        "no_action",
        "previous_slide",
        "reset_view",
        "next_slide",
        "go_to_slide",
        "zoom_region",
    ]
    assert targets[-1].region == action.action.params.region
    selected = Selection(choice="c5", reason="topic", confidence=0.9, evidence=(0,))
    assert checked_selection(selected, targets, 1) == targets[-1]
    assert [d["id"] for d in descriptions] == [f"c{i}" for i in range(6)]
    assert not descriptions[-1]["is_current_view"]
    focused = replace(source, focus_region=action.action.params.region)
    assert options(focused, candidates)[1][-1]["is_current_view"]
    action.semantic_verified = False
    assert "zoom_region" not in {t.tool for t in options(source, candidates)[0]}


def test_natural_navigation_options_do_not_depend_on_semantic_shortlist():
    from backend.tests.api.test_presentation_candidates import analysis

    deck = analysis("introduction", "sales", "reliability", "pricing")
    source = AgentInput(deck, "slide-1", "slide", "hash")
    candidates = (
        Candidate(kind="no_change", slide_id="slide-1", reason="stay", score=0),
        Candidate(kind="slide", slide_id="slide-3", reason="topic", score=1),
    )
    targets, _ = options(source, candidates)
    assert {(t.tool, t.slide_id) for t in targets} == {
        ("no_action", "slide-1"),
        ("go_to_slide", "slide-3"),
        ("previous_slide", "slide-0"),
        ("next_slide", "slide-2"),
    }
    at_start, _ = options(replace(source, current="slide-0"), ())
    assert {t.tool for t in at_start} == {"no_action", "next_slide"}


def test_neighbor_reservation_preserves_every_retrieved_alternative_within_ten_options():
    from backend.tests.api.test_presentation_candidates import analysis

    from buddy_api.analysis.contracts import Region

    deck = analysis(*(f"Summary {i}" for i in range(10)))
    source = AgentInput(deck, "slide-5", "zoom_region", "hash", Region(x=1, y=2, width=3, height=4))
    candidates = (
        Candidate(kind="no_change", slide_id="slide-5", reason="stay", score=0),
        *(
            Candidate(kind="slide", slide_id=f"slide-{i}", reason="topic", score=1)
            for i in (5, 0, 1, 2, 7, 8, 9)
        ),
    )
    targets, descriptions = options(source, candidates)
    assert len(targets) == len(descriptions) == 10
    assert {f"slide-{i}" for i in (0, 1, 2, 7, 8, 9)} <= {t.slide_id for t in targets}
    assert descriptions[-1]["id"] == "c9"
    selection = Selection(choice="c9", reason="explicit", confidence=0.9, evidence=(0,))
    assert checked_selection(selection, targets, 1).tool == "next_slide"


@pytest.mark.postgres
def test_valid_reset_proposal_uses_persisted_focus_and_does_not_execute(agent_api):
    client, ids, provider = agent_api

    async def focus():
        async with client.app.state.database.sessions() as db, db.begin():
            record = await db.get(PresentationSession, ids["sessions"])
            record.focus_view = "zoom_region"
            record.focus_region = {"x": 1, "y": 2, "width": 20, "height": 30}

    client.portal.call(focus)
    provider.answer = Selection(choice="c1", reason="explicit", confidence=0.95, evidence=(0,))
    path = f"/api/v1/sessions/{ids['sessions']}"
    headers = {"Authorization": "Bearer owner"}
    before = client.get(path, headers=headers).json()["data"]
    reply = client.post(path + "/agent", headers=headers, json=payload())
    assert reply.status_code == 200, reply.text
    assert reply.json()["target"]["tool"] == "reset_view"
    assert reply.json()["evidence"] == [0]
    assert client.get(path, headers=headers).json()["data"] == before


@pytest.mark.postgres
def test_exact_navigation_boundary_does_not_spend_provider_allowance(agent_api, capsys):
    client, ids, provider = agent_api
    phrase = "Please go to the previous slide."
    reply = client.post(
        f"/api/v1/sessions/{ids['sessions']}/agent",
        headers={"Authorization": "Bearer owner"},
        json=payload(
            text=phrase, latest_utterance=phrase, segments=[{"sequence": 1, "text": phrase}]
        ),
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["target"]["tool"] == "no_action"
    assert reply.json()["reason"] == "stay"
    assert not provider.calls

    async def quota():
        async with client.app.state.database.sessions() as db:
            return await db.scalar(select(AgentQuota))

    assert client.portal.call(quota) is None
    traces = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith('{"event":"presentation_agent_decision"')
    ]
    assert len(traces) == 1
    assert traces[0]["decision_id"] == reply.json()["decision_id"]
    assert phrase not in json.dumps(traces)


@pytest.mark.postgres
@pytest.mark.parametrize(
    "phrase", ["Let's go to the next slide.", "Let’s move forward by one page."]
)
def test_natural_relative_command_overrides_prior_topic_and_exhausted_quota(agent_api, phrase):
    client, ids, provider = agent_api
    runtime = client.app.state.presentation_agent
    runtime.settings = replace(runtime.settings, monthly_limit=100)

    async def exhaust():
        await runtime.reserve(client.app.state.database)
        async with client.app.state.database.sessions() as db, db.begin():
            row = await db.scalar(select(AgentQuota))
            row.calls = runtime.settings.monthly_limit

    client.portal.call(exhaust)
    text = "Earlier we discussed a distant slide about pricing. " + phrase
    reply = client.post(
        f"/api/v1/sessions/{ids['sessions']}/agent",
        headers={"Authorization": "Bearer owner"},
        json=payload(
            text=text,
            latest_utterance=phrase,
            sequence=2,
            segments=[
                {"sequence": 1, "text": "Earlier we discussed a distant slide about pricing."},
                {"sequence": 2, "text": phrase},
            ],
        ),
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["target"]["tool"] == "next_slide"
    assert reply.json()["decision_source"] == "navigation"
    assert not provider.calls


@pytest.mark.parametrize(
    "choice,evidence,reason",
    [
        ("c7", [0], "topic"),
        ("c1", [], "topic"),
        ("c1", [-1], "topic"),
        ("c1", [1], "topic"),
        ("c1", [0, 0], "topic"),
        ("c1", [0], "uncertain"),
    ],
)
def test_invalid_target_or_evidence_abstains(choice, evidence, reason):
    targets = (Target(tool="no_action", slide_id="a"), Target(tool="next_slide", slide_id="b"))
    with pytest.raises(AgentFailure, match="invalid_output"):
        checked_selection(
            Selection(choice=choice, evidence=evidence, reason=reason, confidence=0.9), targets, 1
        )


@pytest.mark.parametrize(
    "mode",
    [
        "valid",
        "extra",
        "tool",
        "truncated",
        "nonfinite",
        "bool",
        "oversized",
        "redirect",
        "rate",
        "timeout",
    ],
)
def test_provider_strict_transport_and_private_failures(mode):
    async def run():
        def handler(request):
            request_body = json.loads(request.content)
            assert request.url == "https://api.cohere.com/v2/chat"
            assert "thinking" not in request_body
            assert "tools" not in request_body
            # Supported provider schema subset; strict ranges stay in local validation.
            encoded_schema = json.dumps(request_body["response_format"]["schema"])
            assert all(k not in encoded_schema for k in ("minimum", "maximum", "maxItems"))
            answer = {"choice": "c0", "reason": "stay", "confidence": 0.9, "evidence": []}
            if mode == "extra":
                answer["shell"] = "PRIVATE DATA"
            if mode == "nonfinite":
                answer["confidence"] = float("nan")
            if mode == "bool":
                answer["evidence"] = [True]
            if mode == "timeout":
                raise httpx.ReadTimeout("PRIVATE DATA")
            if mode == "oversized":
                return httpx.Response(200, content=b"x" * 65537)
            if mode in {"redirect", "rate"}:
                return httpx.Response(
                    302 if mode == "redirect" else 429,
                    headers={"Location": "https://other.example"},
                )
            return httpx.Response(
                200,
                json={
                    "finish_reason": "MAX_TOKENS" if mode == "truncated" else "COMPLETE",
                    "message": {
                        "content": [{"type": "text", "text": json.dumps(answer)}],
                        "tool_calls": [{"name": "shell"}] if mode == "tool" else [],
                    },
                },
            )

        provider = CohereDecisionProvider(
            AgentSettings("private-key"), httpx.MockTransport(handler)
        )
        try:
            if mode == "valid":
                assert (await provider.select({"segments": ["private"]})).choice == "c0"
            else:
                with pytest.raises(AgentFailure) as error:
                    await provider.select({"segments": ["private"]})
                assert "PRIVATE" not in str(error.value)
        finally:
            await provider.aclose()

    asyncio.run(run())


def test_shared_cohere_pool_has_one_owner_and_preserves_adapter_contract():
    from buddy_api.embeddings import CohereEmbeddings, EmbeddingSettings

    async def check():
        embeddings = CohereEmbeddings(EmbeddingSettings("test-key"))
        agent = CohereDecisionProvider(AgentSettings("test-key"), client=embeddings.client)
        assert embeddings.client is agent.client
        await agent.aclose()
        assert not embeddings.client.is_closed
        await embeddings.aclose()
        assert agent.client.is_closed

    asyncio.run(check())


@pytest.mark.postgres
def test_explicit_stay_reason_is_canonicalized_without_losing_evidence(agent_api):
    client, ids, provider = agent_api
    provider.answer = Selection(choice="c0", reason="explicit", confidence=0.9, evidence=(0,))
    reply = client.post(
        f"/api/v1/sessions/{ids['sessions']}/agent",
        headers={"Authorization": "Bearer owner"},
        json=payload(),
    )
    assert reply.status_code == 200
    assert reply.json()["target"]["tool"] == "no_action"
    assert reply.json()["reason"] == "stay"
    assert reply.json()["decision_source"] == "model"
    assert reply.json()["evidence"] == [0]


def test_manual_request_schema_has_no_dangling_local_definitions():
    from buddy_api.app import create_app
    from buddy_api.config import Environment, Settings

    app = create_app(Settings(Environment.TEST))
    schema = app.openapi()["paths"]["/api/v1/sessions/{identifier}/agent"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert "$defs" not in schema
    assert schema["properties"]["segments"]["items"]["properties"]["sequence"]["type"] == "integer"


@pytest.mark.postgres
def test_owner_gates_no_store_read_only_and_disabled(ready_session_api, caplog, capsys):
    client, ids, *_ = ready_session_api
    path = f"/api/v1/sessions/{ids['sessions']}"
    headers = {"Authorization": "Bearer owner"}
    before = client.get(path, headers=headers).json()["data"]
    for credential, status in ((None, 401), ("other", 403), ("owner", 200)):
        reply = client.post(
            path + "/agent",
            json=payload(),
            headers={"Authorization": "Bearer " + credential} if credential else {},
        )
        assert reply.status_code == status
        if status == 200:
            assert reply.headers["cache-control"] == "no-store"
            assert reply.json()["target"]["tool"] == "no_action"
            assert reply.json()["reason"] == "disabled"
    assert client.get(path, headers=headers).json()["data"] == before
    assert "private speech" not in caplog.text
    stale_version = client.post(
        path + "/agent", headers=headers, json=payload(expected_state_version=1)
    )
    assert stale_version.status_code == 409
    assert stale_version.json()["error"]["code"] == "session_state_changed"
    assert stale_version.json()["request_id"] == stale_version.headers["x-request-id"]
    expired = client.post(
        path + "/agent",
        headers=headers,
        json=payload(sent_at=(datetime.now(UTC) - timedelta(seconds=6)).isoformat()),
    )
    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "speech_context_expired"
    assert expired.json()["request_id"] == expired.headers["x-request-id"]
    conflict_events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"event":"presentation_agent_rejected"' in line
    ]
    assert [event["reason"] for event in conflict_events] == [
        "session_state_changed",
        "speech_context_expired",
    ]
    assert conflict_events[0]["expected_state_version"] == 1
    assert conflict_events[0]["current_state_version"] == before["state_version"]
    assert conflict_events[1]["speech_age_ms"] >= 5000
    assert "private speech" not in json.dumps(conflict_events)
    assert client.post(path + "/agent", headers=headers, content=b"x" * 65537).status_code == 413
    assert (
        client.post(
            path + "/agent", headers=headers, json=payload(text="sensitive invalid")
        ).status_code
        == 422
    )


@pytest.mark.postgres
@pytest.mark.parametrize(
    "mode", ["stay", "malformed", "failure", "busy", "deadline", "stale", "source_changed"]
)
def test_proposal_fallback_and_inflight_state_changes(agent_api, mode):
    client, ids, provider = agent_api
    runtime = client.app.state.presentation_agent
    path = f"/api/v1/sessions/{ids['sessions']}/agent"
    if mode == "malformed":
        provider.answer = Selection(choice="c7", reason="topic", confidence=1.0, evidence=(0,))
    if mode == "failure":
        provider.answer = AgentFailure("provider_unavailable")
    if mode == "busy":

        async def owner():
            async with client.app.state.database.sessions() as db:
                return (await db.get(PresentationSession, ids["sessions"])).owner_id

        runtime.active.add(client.portal.call(owner))
    if mode == "deadline":

        async def slow():
            await asyncio.sleep(10)

        provider.callback = slow
    if mode == "stale":

        async def changed():
            async with client.app.state.database.sessions() as db, db.begin():
                (await db.get(PresentationSession, ids["sessions"])).state_version += 1

        provider.callback = changed
    if mode == "source_changed":
        from buddy_api.persistence.models import Deck

        async def replaced_source():
            async with client.app.state.database.sessions() as db, db.begin():
                (await db.get(Deck, ids["decks"])).source_sha256 = "b" * 64

        provider.callback = replaced_source
    reply = client.post(path, headers={"Authorization": "Bearer owner"}, json=payload())
    assert reply.status_code == 200, reply.text
    assert reply.json()["target"]["tool"] == "no_action"
    assert reply.json()["reason"] == {
        "malformed": "invalid_output",
        "failure": "provider_unavailable",
        "source_changed": "stale",
    }.get(mode, mode)
    if mode != "busy":
        assert not runtime.active


@pytest.mark.postgres
def test_persistent_quota_atomic_pacing_reset_and_budget(agent_api):
    client, _, _ = agent_api
    database = client.app.state.database
    runtime = client.app.state.presentation_agent
    runtime.settings = replace(runtime.settings, requests_per_minute=3, monthly_limit=100)

    async def check():
        results = await asyncio.gather(
            *(runtime.reserve(database) for _ in range(5)), return_exceptions=True
        )
        assert sum(result is None for result in results) == 3
        assert all(
            result is None or isinstance(result, AgentFailure) and result.code == "paced"
            for result in results
        )
        async with database.sessions() as db, db.begin():
            quota = await db.scalar(select(AgentQuota))
            assert quota.calls == 3
            quota.request_times = [t - 61 for t in quota.request_times]
            quota.calls = 100
        restarted = AgentRuntime(
            AgentSettings("test-key", monthly_limit=100, requests_per_minute=3), Provider()
        )
        with pytest.raises(AgentFailure, match="budget"):
            await restarted.reserve(database)
        async with database.sessions() as db, db.begin():
            quota = await db.scalar(select(AgentQuota))
            quota.month = "2020-01"
        await restarted.reserve(database)
        async with database.sessions() as db:
            assert (await db.scalar(select(AgentQuota))).calls == 1

    client.portal.call(check)


@pytest.mark.postgres
def test_paced_admission_includes_wait_and_spends_no_additional_quota(agent_api):
    client, ids, provider = agent_api
    client.app.state.presentation_agent.settings = AgentSettings("test-key", requests_per_minute=1)
    path = f"/api/v1/sessions/{ids['sessions']}/agent"
    headers = {"Authorization": "Bearer owner"}
    assert client.post(path, headers=headers, json=payload()).status_code == 200
    reply = client.post(path, headers=headers, json=payload())
    assert reply.status_code == 200
    assert reply.json()["reason"] == "paced"
    assert reply.json()["decision_source"] == "fallback"
    assert 1 <= reply.json()["retry_after_ms"] <= 5000
    assert len(provider.calls) == 1

    async def count():
        async with client.app.state.database.sessions() as db:
            return (await db.scalar(select(AgentQuota))).calls

    assert client.portal.call(count) == 1


@pytest.mark.postgres
def test_new_speech_is_admitted_without_fixed_wait_or_default_monthly_cap(agent_api):
    client, ids, provider = agent_api
    runtime = client.app.state.presentation_agent
    database = client.app.state.database

    async def previous_usage():
        await runtime.reserve(database)
        async with database.sessions() as db, db.begin():
            row = await db.scalar(select(AgentQuota))
            row.calls = 100

    client.portal.call(previous_usage)
    for sequence in (1, 2, 3):
        phrase = f"Completed presentation passage {sequence}."
        reply = client.post(
            f"/api/v1/sessions/{ids['sessions']}/agent",
            headers={"Authorization": "Bearer owner"},
            json=payload(
                sequence=sequence,
                text=phrase,
                latest_utterance=phrase,
                segments=[{"sequence": sequence, "text": phrase}],
            ),
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["decision_source"] == "model"
    assert len(provider.calls) == 3


@pytest.mark.postgres
def test_rolling_window_survives_restart_and_expires_only_old_attempts(agent_api):
    client, _, _ = agent_api
    database = client.app.state.database
    runtime = client.app.state.presentation_agent
    runtime.settings = replace(runtime.settings, requests_per_minute=2)

    async def check():
        await runtime.reserve(database)
        await runtime.reserve(database)
        restarted = AgentRuntime(runtime.settings, Provider())
        with pytest.raises(AgentFailure, match="paced"):
            await restarted.reserve(database)
        async with database.sessions() as db, db.begin():
            row = await db.scalar(select(AgentQuota))
            row.request_times = [row.request_times[0] - 61, row.request_times[1]]
        await restarted.reserve(database)
        with pytest.raises(AgentFailure, match="paced"):
            await runtime.reserve(database)
        async with database.sessions() as db:
            row = await db.scalar(select(AgentQuota))
            assert row.calls == 3 and len(row.request_times) == 2

    client.portal.call(check)


@pytest.mark.postgres
def test_actual_provider_rate_limit_blocks_new_speech_across_instances(agent_api):
    client, ids, provider = agent_api
    provider.answer = AgentFailure("rate_limited")
    path = f"/api/v1/sessions/{ids['sessions']}/agent"
    headers = {"Authorization": "Bearer owner"}
    assert client.post(path, headers=headers, json=payload()).json()["reason"] == "rate_limited"
    assert client.post(path, headers=headers, json=payload()).json()["reason"] == "paced"
    assert len(provider.calls) == 1

    async def check():
        runtime = AgentRuntime(AgentSettings("test-key"), Provider())
        database = client.app.state.database
        with pytest.raises(AgentFailure, match="paced"):
            await runtime.reserve(database)
        async with database.sessions() as db, db.begin():
            row = await db.scalar(select(AgentQuota))
            row.blocked_until -= timedelta(seconds=61)
        await runtime.reserve(database)

    client.portal.call(check)
