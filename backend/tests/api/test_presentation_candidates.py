"""Candidate retrieval replay and real persisted session authorization."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from backend.tests.api.test_authorization import private_api as private_api
from backend.tests.api.test_authorization import ready_session_api as ready_session_api
from backend.tests.support.database import database_settings as database_settings

from buddy_api.presentation_candidates import CandidateRequest, resolve


@pytest.mark.postgres
def test_region_free_cache_keeps_authorization_and_validation_revisions_fresh(
    ready_session_api, monkeypatch
):
    from backend.tests.support.analysis import prepare

    from buddy_api.analysis.contracts import EvidenceReference, ValidationInput, digest
    from buddy_api.analysis.store import Analyses
    from buddy_api.persistence.models import PresentationSession, ProcessingJob
    from buddy_api.persistence.repositories import Repositories

    client, ids, owner, *_ = ready_session_api
    database = client.app.state.database

    async def setup():
        async with database.sessions() as db, db.begin():
            repos = Repositories.for_session(db)
            document = await prepare(
                repos, owner_id=owner, deck_id=ids["decks"], job_id=ids["jobs"], actions=True
            )
            analysis = await Analyses(db).create(
                owner_id=owner, deck_id=ids["decks"], version=2, document=document
            )
            analysis.publication = "awaiting_validation"
            (await db.get(ProcessingJob, ids["jobs"])).status = "analysis_partial"
            (await db.get(PresentationSession, ids["sessions"])).analysis_version_id = analysis.id
            return analysis.id

    analysis_id = client.portal.call(setup)
    original = Analyses.read
    reads = []

    async def measured(self, *args, **kwargs):
        reads.append(1)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Analyses, "read", measured)
    path = f"/api/v1/sessions/{ids['sessions']}/candidates"
    headers = {"Authorization": "Bearer owner"}
    for _ in range(2):
        assert client.post(path, headers=headers, json=payload()).status_code == 200
    assert len(reads) == 1
    assert (
        client.post(path, headers={"Authorization": "Bearer other"}, json=payload()).status_code
        == 403
    )
    assert (
        client.post(path, headers=headers, json=payload(expected_state_version=3)).status_code
        == 409
    )
    assert len(reads) == 1

    async def verify_region():
        async with database.sessions() as db, db.begin():
            analysis = await original(Analyses(db), analysis_id, owner_id=owner)
            action = next(a.action for s in analysis.slides for a in s.actions)
            verified = await Analyses(db).record_validation(
                owner_id=owner,
                result=ValidationInput(
                    validation_id=uuid4(),
                    analysis_id=analysis.id,
                    analysis_sha256=analysis.analysis_sha256,
                    instance_id=action.instance_id,
                    action_sha256=digest(action),
                    validator_version=analysis.inputs.validator_version,
                    renderer_version=analysis.inputs.renderer_version,
                    display_profile=analysis.inputs.display_profile,
                    state="validated",
                    reasons=(),
                    validation_kind="visual",
                    semantic_verified=True,
                    evidence=(EvidenceReference(id=uuid4(), kind="report", sha256="a" * 64),),
                ),
            )
            (await db.get(ProcessingJob, ids["jobs"])).status = (
                "analysis_ready" if verified.publication == "published" else "analysis_partial"
            )

    client.portal.call(verify_region)
    reads.clear()
    for _ in range(2):
        assert (
            client.post(path, headers=headers, json=payload(text="revenue chart")).status_code
            == 200
        )
    # New eligibility invalidates the old cache, and usable-region reads are never cached.
    assert len(reads) == 2


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
    separated = client.post(
        path + "/candidates",
        headers=headers,
        json=payload(text="revenue growth next slide", latest_utterance="next slide"),
    )
    assert separated.status_code == 200
    assert separated.json()["resolver_version"] == "summary-overlap-2"
    assert (
        client.post(
            path + "/candidates", headers=headers, json=payload(latest_utterance="next slide")
        ).status_code
        == 422
    )
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


@pytest.mark.parametrize("utterance,direction", [("next slide", 1), ("previous slide", -1)])
def test_latest_command_survives_earlier_topic_context(utterance, direction):
    deck = analysis("revenue growth", "customer retention", "security encryption")
    result = resolve(deck, "slide-1", "revenue growth " + utterance, latest_utterance=utterance)
    assert any(
        c.slide_id == f"slide-{1 + direction}" and c.reason == "explicit_navigation" for c in result
    )
    # The complete rolling window still retrieves its earlier topic.
    assert any(c.slide_id == "slide-0" for c in result)


@pytest.mark.parametrize(
    "utterance", ["do not go to the next slide", "we discussed next slide behavior"]
)
def test_latest_utterance_keeps_negation_and_embedded_command_guards(utterance):
    result = resolve(
        analysis("revenue growth", "security"),
        "slide-0",
        "revenue growth " + utterance,
        latest_utterance=utterance,
    )
    assert not any(c.reason == "explicit_navigation" for c in result)


@pytest.mark.parametrize("latest", ["", "   ", "x" * 4097, "different utterance"])
def test_latest_utterance_contract_is_bounded_and_part_of_context(latest):
    with pytest.raises(ValueError):
        CandidateRequest.model_validate(payload(latest_utterance=latest))
