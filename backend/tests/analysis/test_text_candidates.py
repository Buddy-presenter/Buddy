"""Coordinate-free model selection bound to real PDF geometry and source identity."""

import asyncio
import json

import httpx
import pytest
from backend.tests.analysis.test_grounding import (
    GEOMETRY_PDF,
    _load_bytes,
    _output,
    _two_column_pdf,
)
from backend.tests.analysis.test_provider import Session, response_for

from buddy_api.analysis import provider
from buddy_api.analysis.grounding import ground_actions
from buddy_api.analysis.provider import BedrockProvider, ModelAction, ProviderFailure
from buddy_api.analysis.text_candidates import extract_candidates
from buddy_api.processing.stage import execute_text_candidates


def test_repeated_unicode_lines_have_stable_distinct_location_ids_and_no_column_union():
    content = _two_column_pdf()
    catalog = execute_text_candidates(content)
    assert catalog == execute_text_candidates(content)
    assert [item.text for item in catalog.candidates] == ["café results", "café results"]
    assert len({item.id for item in catalog.candidates}) == 2
    assert all(item.region.width < 400 for item in catalog.candidates)
    assert catalog.candidates[0].region.x < catalog.candidates[1].region.x


def test_selected_id_ignores_bad_model_geometry_and_rejects_unknown_ids():
    content = _two_column_pdf()
    catalog = extract_candidates(content)
    target = catalog.candidates[1]
    extraction, manifest = _load_bytes(content)
    action = ModelAction(
        type="highlight_region",
        purpose="Focus on the right column.",
        target_text=target.text,
        target_id=target.id,
        x=0,
        y=0,
        width=1,
        height=1,
    )
    grounded = ground_actions(content, extraction, manifest, _output(extraction, {0: (action,)}))
    result = grounded.slides[0].actions[0]
    assert result.x == target.region.x - 1.5
    assert result.y == target.region.y - 1.5
    assert result.grounding_trace.method == "pdf_candidate_line"
    assert result.grounding_trace.target_id == target.id
    for invalid in (
        action.model_copy(update={"target_id": "invented-id"}),
        action.model_copy(update={"target_text": "Different text"}),
    ):
        assert (
            not ground_actions(content, extraction, manifest, _output(extraction, {0: (invalid,)}))
            .slides[0]
            .actions
        )


@pytest.mark.parametrize("binding", ["constructor", "request"])
def test_provider_candidate_request_never_asks_for_coordinates(monkeypatch, binding):
    content = _two_column_pdf()
    catalog = extract_candidates(content)
    extraction, manifest = _load_bytes(content)
    target = catalog.candidates[1]
    monkeypatch.setattr(provider, "Session", Session)

    def handler(request):
        body = json.loads(request.content)
        action_schema = body["output_config"]["format"]["schema"]["properties"]["slides"]["items"][
            "properties"
        ]["actions"]["items"]
        assert set(action_schema["properties"]) == {"type", "purpose", "target_id"}
        return httpx.Response(
            200,
            json=response_for(
                output_text=json.dumps(
                    {
                        "presentation": {"text": "Synthetic columns.", "reason": None},
                        "slides": [
                            {
                                "number": 1,
                                "text": "Two columns.",
                                "reason": None,
                                "actions": [
                                    {
                                        "type": "highlight_region",
                                        "purpose": "Right column.",
                                        "target_id": target.id,
                                    }
                                ],
                            }
                        ],
                    }
                )
            ),
        )

    client = BedrockProvider(
        "eu-central-1",
        transport=httpx.MockTransport(handler),
        text_candidates=catalog if binding == "constructor" else None,
    )
    result = asyncio.run(
        client.analyze_async(
            content,
            extraction,
            manifest.deck_id,
            manifest,
            text_candidates=catalog if binding == "request" else None,
        )
    )
    if binding == "request":
        assert client.text_candidates is None
    action = result.slides[0].actions[0]
    assert action.target_text == target.text and action.target_id == target.id
    assert action.x == target.region.x


def test_candidate_provider_rejects_stale_source_and_cross_page_selection(monkeypatch):
    content = _two_column_pdf()
    catalog = extract_candidates(content)
    extraction, manifest = _load_bytes(content)
    monkeypatch.setattr(provider, "Session", Session)
    stale = catalog.model_copy(update={"source_sha256": "0" * 64})
    with pytest.raises(ProviderFailure, match="candidate_source_mismatch"):
        BedrockProvider("eu-central-1", text_candidates=stale)._signed_request(
            content, extraction, manifest.deck_id, manifest
        )
    raw = json.dumps(
        response_for(
            output_text=json.dumps(
                {
                    "presentation": {"text": "Synthetic.", "reason": None},
                    "slides": [
                        {
                            "number": 2,
                            "text": "Synthetic.",
                            "reason": None,
                            "actions": [
                                {
                                    "type": "highlight_region",
                                    "purpose": "Wrong page.",
                                    "target_id": catalog.candidates[0].id,
                                }
                            ],
                        }
                    ],
                }
            )
        )
    ).encode()
    with pytest.raises(ProviderFailure, match="provider_malformed_output"):
        BedrockProvider._decode_response(raw, [(1224, 1584)], catalog)


def test_rotated_page_is_not_misrepresented_as_horizontal_lines():
    catalog = execute_text_candidates(GEOMETRY_PDF.read_bytes())
    assert all(item.page != 2 for item in catalog.candidates)


@pytest.mark.parametrize("malformation", ["missing_actions", "coordinates", "unknown_id"])
def test_candidate_provider_sanitizes_invalid_selection_responses(malformation):
    catalog = extract_candidates(_two_column_pdf())
    action = {
        "type": "highlight_region",
        "purpose": "Synthetic target.",
        "target_id": catalog.candidates[0].id,
    }
    slide = {"number": 1, "text": "Synthetic.", "reason": None, "actions": [action]}
    if malformation == "missing_actions":
        del slide["actions"]
    elif malformation == "coordinates":
        action["x"] = 123
    else:
        action["target_id"] = "invented-id"
    raw = json.dumps(
        response_for(
            output_text=json.dumps(
                {
                    "presentation": {"text": "Synthetic.", "reason": None},
                    "slides": [slide],
                }
            )
        )
    ).encode()
    with pytest.raises(ProviderFailure, match="^provider_malformed_output$"):
        BedrockProvider._decode_response(raw, [(1224, 1584)], catalog)
