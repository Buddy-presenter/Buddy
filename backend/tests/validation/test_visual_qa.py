from types import SimpleNamespace

import pytest

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
    assert client.request["toolConfig"]["toolChoice"] == {
        "tool": {"name": "visual_verdict"}
    }


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
