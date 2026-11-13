"""Candidate retrieval replay and real persisted session authorization."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from backend.tests.api.test_authorization import private_api as private_api
from backend.tests.api.test_authorization import ready_session_api as ready_session_api
from backend.tests.support.database import database_settings as database_settings

from buddy_api.presentation_candidates import CandidateRequest, resolve


def analysis(*texts):
    return SimpleNamespace(
        slides=tuple(
            SimpleNamespace(
                slide_id=f"slide-{i}",
                summary=SimpleNamespace(status="available", text=text),
                actions=(),
            )
            for i, text in enumerate(texts)
        )
    )


@pytest.mark.parametrize(
    "text,current,target",
    [
        ("revenue growth", "slide-0", "slide-0"),
        ("next slide", "slide-0", "slide-1"),
        ("previous slide", "slide-2", "slide-1"),
        ("return to revenue growth", "slide-2", "slide-0"),
        ("security encryption", "slide-0", "slide-2"),
    ],
)
def test_replay_current_adjacent_and_earlier_topics(text, current, target):
    deck = analysis("revenue growth", "customer retention", "security encryption")
    result = resolve(deck, current, text)
    assert result[0].kind == "no_change"
    assert target in {candidate.slide_id for candidate in result}
    assert result == resolve(deck, current, text)
    assert len(result) <= 8


def test_ambiguous_and_negated_navigation_is_conservative():
    deck = analysis("results revenue", "results customer", "results security")
    for text in ("results", "do not go to the next slide", "unrelated words"):
        result = resolve(deck, "slide-0", text)
        assert {candidate.slide_id for candidate in result} == {"slide-0"}


def test_unusable_region_is_never_a_candidate():
    deck = analysis("revenue", "security")
    deck.slides[0].actions = (
        SimpleNamespace(usable=False, state="candidate", semantic_verified=False),
    )
    assert all(
        candidate.kind in {"no_change", "slide"}
        for candidate in resolve(deck, "slide-0", "revenue")
    )


def payload(**changes):
    return {
        "expected_state_version": 4,
        "context_epoch": 2,
        "stream_id": str(uuid4()),
        "sequence": 1,
        "sent_at": datetime.now(UTC).isoformat(),
        "text": "private speech",
        **changes,
    }


@pytest.mark.postgres
def test_persisted_candidate_route_is_private_fresh_and_read_only(ready_session_api, caplog):
    client, ids, *_ = ready_session_api
    path = f"/api/v1/sessions/{ids['sessions']}"
    body = payload()
    headers = {"Authorization": "Bearer owner"}
    before = client.get(path, headers=headers).json()
    response = client.post(path + "/candidates", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["candidates"][0]["kind"] == "no_change"
    assert result["context_epoch"] == 2
    assert client.post(path + "/candidates", headers=headers, json=body).json() == result
    assert client.get(path, headers=headers).json()["data"] == before["data"]
    assert "private speech" not in caplog.text
    assert client.post(path + "/candidates", json=body).status_code == 401
    assert (
        client.post(
            path + "/candidates", headers={"Authorization": "Bearer other"}, json=body
        ).status_code
        == 403
    )
    assert (
        client.post(
            path + "/candidates", headers=headers, json=payload(expected_state_version=3)
        ).status_code
        == 409
    )
    old = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    assert (
        client.post(path + "/candidates", headers=headers, json=payload(sent_at=old)).status_code
        == 409
    )
    assert (
        client.post(path + "/candidates", headers=headers, content=b"x" * 65537).status_code == 413
    )


def test_context_contract_is_bounded_and_hides_text():
    assert "private speech" not in repr(CandidateRequest.model_validate(payload()))
    with pytest.raises(ValueError):
        CandidateRequest.model_validate(payload(text="x" * 8193))


def test_only_usable_views_are_retrieved_by_purpose():
    deck = analysis("intro", "details")
    deck.slides[1].actions = (
        SimpleNamespace(
            usable=True,
            state="validated",
            semantic_verified=True,
            action=SimpleNamespace(
                purpose="revenue chart", type="zoom_region", instance_id="chart"
            ),
        ),
    )
    result = resolve(deck, "slide-0", "revenue chart")
    assert any(
        c.kind == "zoom_region" and c.slide_id == "slide-1" and c.action_id == "chart"
        for c in result
    )


def test_end_of_deck_and_maximum_deck_are_bounded():
    deck = analysis(*(f"topic{i}" for i in range(500)))
    assert {c.slide_id for c in resolve(deck, "slide-499", "next slide")} == {"slide-499"}
    result = resolve(deck, "slide-499", "topic0")
    assert any(c.slide_id == "slide-0" for c in result)
    assert len(result) <= 8
