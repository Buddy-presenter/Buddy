from types import SimpleNamespace
from uuid import uuid4

import pytest

from buddy_api.analysis.store import Analyses
from buddy_api.persistence.errors import PersistenceConflict
from buddy_api.validation.visual_qa import VisualQAService


def _response(verdict="validated", issue=None, confidence=0.98):
    return {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "visual_verdict",
                            "input": {
                                "verdict": verdict,
                                "confidence": confidence,
                                "issue": issue,
                                "observation": "The requested title is visibly highlighted.",
                            },
                        }
                    }
                ]
            }
        },
    }


VALID_VERDICT = _response()["output"]["message"]["content"][0]["toolUse"]["input"]


class FakeBedrock:
    def __init__(self, response):
        self.response = response
        self.request = None

    def converse(self, **request):
        self.request = request
        return self.response


@pytest.fixture
def action():
    return SimpleNamespace(
        purpose="Highlight slide title",
        type="highlight_region",
        params=SimpleNamespace(target_text="Project Overview"),
    )


def test_judge_uses_structured_verdict_and_sends_both_audience_captures(action):
    client = FakeBedrock(_response())

    result = VisualQAService._judge(
        client,
        action=action,
        source_format="pdf",
        before=b"before-image",
        after=b"after-image",
    )

    assert result["verdict"] == "validated"
    assert client.request["modelId"] == "eu.anthropic.claude-sonnet-4-6"
    content = client.request["messages"][0]["content"]
    assert content[1]["image"]["source"]["bytes"] == b"before-image"
    assert content[2]["image"]["source"]["bytes"] == b"after-image"
    assert client.request["toolConfig"]["toolChoice"] == {"tool": {"name": "visual_verdict"}}


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (0.79, ("needs-review", False, ["uncertain"])),
        (0.8, ("validated", True, [])),
    ],
)
def test_low_confidence_cannot_mark_action_usable(confidence, expected):
    verdict = _response(confidence=confidence)["output"]["message"]["content"][0]["toolUse"][
        "input"
    ]
    assert VisualQAService._decision(verdict) == expected


def test_low_confidence_rejection_requires_review_instead_of_false_certainty():
    verdict = _response(verdict="rejected", issue="wrong_target", confidence=0.2)["output"][
        "message"
    ]["content"][0]["toolUse"]["input"]
    assert VisualQAService._decision(verdict) == ("needs-review", False, ["uncertain"])


def test_lock_current_revision_locks_deck_before_reading_latest():
    deck, latest = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())

    class Session:
        statements = []

        async def scalar(self, statement):
            self.statements.append(statement)
            return deck if len(self.statements) == 1 else latest

    async def scenario():
        session = Session()
        found = await Analyses(session).lock_current_revision(deck.id, owner_id=uuid4())
        assert found is latest
        assert len(session.statements) == 2
        assert session.statements[0]._for_update_arg is not None

    import asyncio

    asyncio.run(scenario())


def test_powerpoint_visual_qa_is_rejected_before_execution_or_persistence(monkeypatch):
    import asyncio

    analysis = SimpleNamespace(source_format="pptx", deck_id=uuid4())

    class Context:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_args):
            return None

    class Session:
        def begin(self):
            return Context(self)

    session = Session()

    class Database:
        def sessions(self):
            return Context(session)

    async def get(_self, _analysis_id, *, owner_id):
        return analysis

    monkeypatch.setattr(Analyses, "get", get)
    executor_calls = []
    executor = SimpleNamespace(validate=lambda *args: executor_calls.append(args))
    service = VisualQAService(SimpleNamespace(database=Database()), executor, "eu-west-1")

    async def scenario():
        with pytest.raises(PersistenceConflict, match="PDF actions only"):
            await service.verify(uuid4(), uuid4(), "ppt-action", uuid4())

    asyncio.run(scenario())
    assert executor_calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"stopReason": "end_turn", "output": {"message": {"content": []}}},
        {
            "stopReason": "tool_use",
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "name": "visual_verdict",
                                "input": VALID_VERDICT,
                            }
                        },
                        {
                            "toolUse": {
                                "name": "visual_verdict",
                                "input": VALID_VERDICT,
                            }
                        },
                    ]
                }
            },
        },
        _response(verdict="validated", issue="wrong_target"),
        _response(verdict="rejected", issue=None),
        _response(confidence=1.1),
        _response(confidence=True),
    ],
)
def test_judge_fails_closed_on_missing_ambiguous_or_conflicting_verdict(action, response):
    with pytest.raises(ValueError):
        VisualQAService._judge(
            FakeBedrock(response),
            action=action,
            source_format="pptx",
            before=b"before-image",
            after=b"after-image",
        )
