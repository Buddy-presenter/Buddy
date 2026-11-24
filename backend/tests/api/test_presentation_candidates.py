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
    clock = [0.0]
    monkeypatch.setattr(
        "buddy_api.presentation_candidates.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    path = f"/api/v1/sessions/{ids['sessions']}/candidates"
    headers = {"Authorization": "Bearer owner"}
    for _ in range(2):
        assert client.post(path, headers=headers, json=payload()).status_code == 200
    assert len(reads) == 1
    for instant in (29, 31):
        clock[0] = instant
        assert client.post(path, headers=headers, json=payload()).status_code == 200
    assert len(reads) == 1  # Active, re-authorized reads refresh the idle TTL.
    assert (
        client.post(path, headers={"Authorization": "Bearer other"}, json=payload()).status_code
        == 403
    )
    assert (
        client.post(path, headers=headers, json=payload(expected_state_version=3)).status_code
        == 409
    )
    assert len(reads) == 1
    clock[0] = 65
    assert client.post(path, headers=headers, json=payload()).status_code == 200
    assert len(reads) == 2  # Idle presentations still expire after 30 seconds.

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


def test_powerpoint_slide_summaries_are_searchable_without_promoting_office_actions():
    from buddy_api.analysis.contracts import (
        PowerPointAnalysisResult,
        PowerPointSlideResult,
        Summary,
    )
    from buddy_api.semantic_candidates import documents

    deck = PowerPointAnalysisResult(
        id=uuid4(),
        deck_id=uuid4(),
        version=1,
        publication="published",
        analysis_sha256=None,
        inputs=None,
        slides=(
            PowerPointSlideResult(
                slide_id="slide-0", summary=Summary(status="available", text="Intro"), actions=()
            ),
            PowerPointSlideResult(
                slide_id="slide-1",
                summary=Summary(status="available", text="Revenue growth"),
                actions=(),
            ),
        ),
    )
    assert [item.slide_id for item in documents(deck)] == ["slide-0", "slide-1"]
    assert any(item.slide_id == "slide-1" for item in resolve(deck, "slide-0", "revenue growth"))


def test_powerpoint_execution_validation_alone_does_not_promote_region():
    from buddy_api.semantic_candidates import documents

    deck = analysis("revenue", "security")
    deck.slides[0].actions = (
        SimpleNamespace(
            usable=True,
            state="validated",
            action=SimpleNamespace(
                instance_id="region-1", type="highlight_region", purpose="revenue"
            ),
        ),
    )
    assert all(item.action_id is None for item in documents(deck))
    assert all(item.kind in {"no_change", "slide"} for item in resolve(deck, "slide-0", "revenue"))


def test_powerpoint_region_requires_semantic_evidence_and_supported_tool():
    from buddy_api.presentation_candidates import candidate_action_eligible

    action = SimpleNamespace(
        usable=True,
        state="validated",
        action=SimpleNamespace(type="zoom_region"),
        validation=SimpleNamespace(semantic_verified=False),
    )
    assert not candidate_action_eligible(action)
    action.validation.semantic_verified = True
    assert candidate_action_eligible(action)
    action.action.type = "draw_star"
    assert not candidate_action_eligible(action)
    action.action.type = "zoom_region"
    action.usable = False
    assert not candidate_action_eligible(action)


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


def saved(identifier, purpose, target=None, *, usable=True, verified=True):
    return SimpleNamespace(
        usable=usable,
        state="validated",
        semantic_verified=verified,
        action=SimpleNamespace(
            purpose=purpose,
            type="highlight_region",
            instance_id=identifier,
            params=SimpleNamespace(target_text=target),
        ),
    )


def test_spoken_target_text_selects_the_saved_action_not_just_its_purpose():
    """#202: "the twelve percent figure" matches the action that targets that text."""
    deck = analysis("intro", "results")
    deck.slides[1].actions = (
        saved("growth", "Emphasise the key result", "Revenue grew twelve percent"),
        saved("pilot", "Emphasise the recommendation", "Expand the pilot after review"),
    )
    result = resolve(deck, "slide-1", "look at the twelve percent revenue growth")
    actions = [c for c in result if c.kind == "highlight_region"]
    assert actions and actions[0].action_id == "growth"
    assert all(c.action_id != "pilot" for c in actions)


def test_unverified_saved_actions_are_never_candidates():
    deck = analysis("intro", "results")
    deck.slides[1].actions = (
        saved("unchecked", "Revenue figure", "Revenue grew", verified=False),
        saved("off", "Revenue figure", "Revenue grew", usable=False),
    )
    result = resolve(deck, "slide-1", "the revenue figure")
    assert all(c.kind not in {"zoom_region", "highlight_region"} for c in result)


def test_action_slots_are_bounded():
    deck = analysis("intro", "results")
    deck.slides[1].actions = tuple(
        saved(f"a{i}", "revenue chart", "revenue chart") for i in range(6)
    )
    result = resolve(deck, "slide-1", "revenue chart")
    assert len([c for c in result if c.action_id]) == 3 and len(result) <= 8


def test_semantic_action_documents_include_the_targeted_slide_text():
    """#202: production retrieval embeds what the action points at, not just its purpose."""
    from buddy_api.semantic_candidates import documents

    deck = analysis(
        "intro",
        "Market analysis: a current market-share pie chart and projected sales bar chart.",
    )
    deck.slides[1].actions = (
        saved("growth", "Emphasise the key result", "Revenue grew twelve percent"),
        saved("hidden", "Unverified", "Secret", verified=False),
    )
    texts = {d.action_id: d.text for d in documents(deck) if d.action_id}
    assert texts == {
        "growth": (
            "Slide 2. Slide context: Market analysis: a current market-share pie chart "
            "and projected sales bar chart.\nAction type: highlight_region\n"
            "Emphasise the key result Target: Revenue grew twelve percent"
        )
    }


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


@pytest.mark.parametrize(
    "utterance,step",
    [
        ("Let's go to the next slide.", 1),
        ("Let’s move to the previous page, please.", -1),
        ("Please switch to the next page.", 1),
        ("Let us move forward by one page.", 1),
        ("Go back one slide.", -1),
        ("Do not go to the next slide.", None),
        ("Should we go to the next slide?", None),
        ('He said "next slide".', None),
        ("“Next slide”", None),
        ("'next slide'", None),
        ("Next slide?", None),
        ("If we go to the next slide, we lose context.", None),
        ("Let's go to the next slide. Actually, stay here.", None),
        ("Let's go to the next slide, and then review the data.", 1),
        ("Go to the next slide where we can compare results.", 1),
        ("The next slide describes refunds.", None),
        ("Next slide behavior is different.", None),
    ],
)
def test_relative_navigation_requires_a_complete_unambiguous_instruction(utterance, step):
    from buddy_api.presentation_candidates import navigation_step

    assert navigation_step(utterance) == step


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("Go to slide 8.", ("absolute", 8)),
        ("Go to slide -8.", ("absolute", -8)),
        ("Go to slide 500.", ("absolute", 500)),
        ("Go to slide five hundred.", ("absolute", 500)),
        ("Please go to slide twenty-one.", ("absolute", 21)),
        ("Let's jump to page one hundred and twenty-five.", ("absolute", 125)),
        ("Show me slide 3", ("absolute", 3)),
        ("Take me to the slide number 42, please.", ("absolute", 42)),
        ("I want to go to slide six.", ("absolute", 6)),
        ("Let's go to the next slide.", ("relative", 1)),
        ("Should I go to slide 8?", None),
        ('She said "go to slide 8".', None),
        ("Do not go to slide 8.", None),
        ("If we go to slide 8, we lose context.", None),
        ("Go to slide eight after the chart.", None),
        ("Go to slide eight, and then we can review the chart.", ("absolute", 8)),
        ("Go to slide eight, and only if the audience agrees.", None),
        ("Go to slide eight, unless we run out of time.", None),
        ("Go to the next slide and do not move until I finish.", None),
        ("Go to the next slide, but don't move yet.", None),
        ("Go to slide 7 where we explain the results.", ("absolute", 7)),
        ("Let's go back to slide one, where the agenda is.", ("absolute", 1)),
        ("Let's go back to the first slide, where the agenda is.", ("absolute", 1)),
        ("Please move to the twenty first slide.", ("absolute", 21)),
        ("Go to the 30th slide.", ("absolute", 30)),
        ("Go to slide 8 if there is time.", None),
        ("Go to slide 8, actually stay on this slide.", None),
        ("Slide 8 explains the results.", None),
    ],
)
def test_navigation_intent_accepts_exact_numbered_commands_only(utterance, expected):
    from buddy_api.presentation_candidates import navigation_intent

    assert navigation_intent(utterance) == expected


def test_numbered_navigation_targets_the_exact_slide_and_invalid_number_does_not_topic_match():
    deck = analysis("market overview", "revenue", "security", "pricing")
    target = resolve(deck, "slide-0", "Go to slide four.")
    assert [(c.slide_id, c.reason) for c in target if c.reason == "explicit_navigation"] == [
        ("slide-3", "explicit_navigation")
    ]
    invalid = resolve(deck, "slide-0", "Go to slide 99.")
    assert {c.slide_id for c in invalid} == {"slide-0"}
    negative = resolve(deck, "slide-0", "Go to slide -8.")
    assert {c.slide_id for c in negative} == {"slide-0"}
    current = resolve(deck, "slide-1", "Show me slide 2")
    assert not any(c.reason == "explicit_navigation" for c in current)


@pytest.mark.parametrize("latest", ["", "   ", "x" * 4097, "different utterance"])
def test_latest_utterance_contract_is_bounded_and_part_of_context(latest):
    with pytest.raises(ValueError):
        CandidateRequest.model_validate(payload(latest_utterance=latest))


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("Let's now have a look at the marketing strategies", True),
        ("Please review competitive analysis", True),
        ("We previously saw marketing strategies", False),
        ("Let's look at the chart if there is time", False),
        ("Could we review the objectives?", False),
        ('She said "look at marketing strategies"', False),
    ],
)
def test_topic_navigation_intent_distinguishes_requests_from_mentions(utterance, expected):
    from buddy_api.presentation_candidates import topic_navigation_intent

    assert topic_navigation_intent(utterance) is expected


def test_topic_destination_requires_a_unique_strong_retrieval_winner():
    from buddy_api.presentation_candidates import Candidate, confident_topic_destination

    candidates = (
        Candidate(kind="slide", slide_id="current", reason="current", score=700),
        Candidate(kind="slide", slide_id="marketing", reason="topic", score=1600),
        Candidate(kind="slide", slide_id="pricing", reason="topic", score=500),
    )
    assert confident_topic_destination(candidates, semantic=True) == "marketing"
    assert confident_topic_destination(candidates, semantic=False) == "marketing"
    tied = (
        Candidate(kind="slide", slide_id="current", reason="current", score=0),
        Candidate(kind="slide", slide_id="one", reason="topic", score=1600),
        Candidate(kind="slide", slide_id="two", reason="topic", score=1600),
    )
    assert confident_topic_destination(tied, semantic=True) is None
    assert confident_topic_destination(tied, semantic=False) is None
