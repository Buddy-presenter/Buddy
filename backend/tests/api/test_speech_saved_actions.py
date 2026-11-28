"""#202: live speech selects and admits only executed, visually verified saved actions.

Runs the real session, candidate, agent and transition-check API with PostgreSQL. The
vision model and the decision model are deterministic substitutes at their external
boundaries; everything between speech and the accepted session action is real.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from backend.tests.api.test_authorization import private_api as private_api
from backend.tests.api.test_authorization import ready_session_api as ready_session_api
from backend.tests.support.database import database_settings as database_settings

from buddy_api.agent_provider import AgentSettings, Selection
from buddy_api.analysis.contracts import EvidenceReference, ValidationInput, digest
from buddy_api.persistence.models import PresentationSession
from buddy_api.presentation_agent import AgentRuntime

pytestmark = pytest.mark.postgres
HEADERS = {"Authorization": "Bearer owner"}


class ChoosingProvider:
    """Stands in for the decision model: picks an option by tool, or abstains."""

    def __init__(self):
        self.passages = []
        self.tool = None

    async def select(self, passage):
        self.passages.append(passage)
        if self.tool is None:
            return Selection(choice="c0", reason="uncertain", confidence=0.4, evidence=())
        option = next(o for o in passage["options"] if o["tool"] == self.tool)
        return Selection(choice=option["id"], reason="topic", confidence=0.93, evidence=(0,))

    async def aclose(self):
        pass


def speech(text, version):
    return {
        "protocol": "presentation-agent.v1",
        "expected_state_version": version,
        "context_epoch": 1,
        "stream_id": str(uuid4()),
        "sequence": 1,
        "sent_at": datetime.now(UTC).isoformat(),
        "text": text,
        "latest_utterance": text,
        "segments": [{"sequence": 1, "text": text}],
    }


@pytest.fixture
def verified_deck(ready_session_api):
    """A published PDF analysis whose first action passed execution and visual QA."""
    client, ids, owner_id, _, _ = ready_session_api
    database = client.app.state.database

    async def publish():
        from backend.tests.support.analysis import prepare

        async with database.transaction() as repos:
            document = await prepare(
                repos, owner_id=owner_id, deck_id=ids["decks"], job_id=ids["jobs"], actions=True
            )
            saved = await repos.analyses.create(
                owner_id=owner_id, deck_id=ids["decks"], version=2, document=document
            )
            result = await repos.analyses.read(saved.id, owner_id=owner_id)
            slide = result.slides[0]
            verified, unverified = slide.actions[0], slide.actions[1:]
            evidence = tuple(
                EvidenceReference(id=uuid4(), kind=kind, sha256="e" * 64)
                for kind in ("before_image", "after_image", "report")
            )
            result = await repos.analyses.record_validation(
                owner_id=owner_id,
                result=ValidationInput(
                    validation_id=uuid4(),
                    analysis_id=result.id,
                    analysis_sha256=result.analysis_sha256,
                    instance_id=verified.action.instance_id,
                    action_sha256=digest(verified.action),
                    validator_version=result.inputs.validator_version,
                    renderer_version=result.inputs.renderer_version,
                    display_profile=result.inputs.display_profile,
                    validation_kind="visual",
                    semantic_verified=True,
                    state="validated",
                    evidence=evidence,
                ),
            )
            return result, slide.slide_id, verified.action, [a.action for a in unverified]

    analysis, slide_id, action, unverified = client.portal.call(publish)
    started = client.post("/api/v1/sessions", headers=HEADERS, json={"deck_id": str(ids["decks"])})
    assert started.status_code == 200, started.text
    session = started.json()["data"]

    async def autonomous():
        async with database.sessions() as db, db.begin():
            record = await db.get(PresentationSession, session["id"])
            record.autonomy_mode = "autonomous"
            record.active_slide_id = slide_id

    client.portal.call(autonomous)
    provider = ChoosingProvider()
    client.app.state.presentation_agent = AgentRuntime(AgentSettings("test-key"), provider)
    session = client.get(f"/api/v1/sessions/{session['id']}", headers=HEADERS).json()["data"]
    return client, session, analysis, slide_id, action, unverified, provider


def test_speech_about_a_verified_target_selects_and_admits_that_saved_action(verified_deck):
    client, session, analysis, slide_id, action, unverified, provider = verified_deck
    path = f"/api/v1/sessions/{session['id']}"
    provider.tool = action.type
    reply = client.post(
        path + "/agent",
        headers=HEADERS,
        json=speech(f"so here {action.purpose.lower()}", session["state_version"]),
    )
    assert reply.status_code == 200, reply.text
    proposal = reply.json()
    target = proposal["target"]
    # The model only chose among offered candidates; the target is the saved action.
    assert target["tool"] == action.type and target["action_id"] == action.instance_id
    region = action.params.region
    assert target["region"] == {
        "x": region.x,
        "y": region.y,
        "width": region.width,
        "height": region.height,
    }
    # Only the one verified action is offered as a region option; unverified actions
    # on the same slide never reach the model (options carry no internal IDs).
    regions = [
        o
        for o in provider.passages[-1]["options"]
        if o["tool"] in {"zoom_region", "highlight_region"}
    ]
    assert len(regions) == 1 and regions[0]["tool"] == action.type
    # Server-side re-check before execution, then the session admits the action.
    check = client.post(
        path + "/transition-check",
        headers=HEADERS,
        json={
            "expected_state_version": session["state_version"],
            "analysis_id": str(analysis.id),
            "active_slide_id": slide_id,
            "target": target,
        },
    )
    assert check.status_code == 200, check.text
    accepted = client.patch(
        path + "/state",
        headers=HEADERS,
        json={
            "expected_state_version": session["state_version"],
            "active_slide_id": slide_id,
            "focus_view": action.type,
            "focus_region": target["region"],
            "autonomy_mode": "autonomous",
            "last_accepted_action_id": action.instance_id,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["state"]["last_accepted_action_id"] == action.instance_id
    # Duplicate decisions are refused by the desktop policy (duplicate evidence and
    # current-focus rules, covered in frontend/tests/test_transition_policy.py); the
    # server check intentionally tolerates a lagging saved position (#21).


@pytest.mark.parametrize(
    "utterance",
    [
        "let's not look at {purpose} right now",
        "if we had time we might {purpose}",
        "the weather is nice today",
    ],
)
def test_the_agent_can_abstain_and_nothing_executes(verified_deck, utterance):
    client, session, analysis, slide_id, action, _, provider = verified_deck
    provider.tool = None  # the model abstains on negated, hypothetical or unrelated speech
    reply = client.post(
        f"/api/v1/sessions/{session['id']}/agent",
        headers=HEADERS,
        json=speech(utterance.format(purpose=action.purpose.lower()), session["state_version"]),
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["target"]["tool"] == "no_action"
    after = client.get(f"/api/v1/sessions/{session['id']}", headers=HEADERS).json()["data"]
    assert after["state_version"] == session["state_version"]
    assert after["state"]["last_accepted_action_id"] is None


def test_unverified_or_mismatched_saved_actions_never_pass_the_server_check(verified_deck):
    client, session, analysis, slide_id, action, unverified, _ = verified_deck
    path = f"/api/v1/sessions/{session['id']}"
    region = action.params.region
    base = {
        "tool": action.type,
        "slide_id": slide_id,
        "action_id": action.instance_id,
        "region": {"x": region.x, "y": region.y, "width": region.width, "height": region.height},
    }
    cases = [
        {**base, "action_id": unverified[0].instance_id} if unverified else None,
        {**base, "region": {**base["region"], "x": region.x + 5}},
        {**base, "action_id": "made-up-action"},
    ]
    for target in filter(None, cases):
        check = client.post(
            path + "/transition-check",
            headers=HEADERS,
            json={
                "expected_state_version": session["state_version"],
                "analysis_id": str(analysis.id),
                "active_slide_id": slide_id,
                "target": target,
            },
        )
        assert check.status_code in {409, 422}, (target, check.text)
