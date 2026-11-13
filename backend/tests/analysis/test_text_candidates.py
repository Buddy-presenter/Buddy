"""Coordinate-free model selection bound to real PDF geometry and source identity."""

import asyncio
import json

import httpx
import pytest
from backend.tests.analysis.test_grounding import (
    GEOMETRY_PDF,
    _load_bytes,
    _pdf_with_content,
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
    assert all(
        item.kind in {"title", "heading", "paragraph", "table_row"} for item in catalog.candidates
    )
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


def test_entity_inventory_keeps_cover_title_paragraph_and_complete_bullets_together():
    content = _pdf_with_content(
        b"BT /F1 24 Tf 90 740 Td (Main Presentation Title) Tj "
        b"/F1 11 Tf 0 -38 Td 14 TL (This paragraph wraps across the first line) Tj T* "
        b"(and its continuation on the next line.) Tj T* T* "
        b"(1. First bullet wraps as one item) Tj T* (and continues here.) Tj T* "
        b"(2. Second bullet remains a separate item.) Tj ET"
    )

    catalog = extract_candidates(content)
    titles = [item for item in catalog.candidates if item.kind == "title"]
    paragraphs = [item for item in catalog.candidates if item.kind == "paragraph"]
    bullets = [item for item in catalog.candidates if item.kind == "bullet"]

    assert any("Main Presentation Title" in item.text for item in titles)
    assert any(
        item.text
        == "This paragraph wraps across the first line and its continuation on the next line."
        for item in paragraphs
    )
    assert [item.text for item in bullets] == [
        "First bullet wraps as one item and continues here.",
        "Second bullet remains a separate item.",
    ]


def test_grounding_adds_every_text_entity_even_when_model_omits_title():
    content = _pdf_with_content(
        b"BT /F1 24 Tf 90 740 Td (Main Presentation Title) Tj "
        b"/F1 11 Tf 0 -38 Td 14 TL (A complete paragraph with useful content.) Tj ET"
    )
    catalog = extract_candidates(content)
    extraction, manifest = _load_bytes(content)
    generated = _output(extraction, {}).model_copy(update={"source_inventory_enabled": True})

    grounded = ground_actions(content, extraction, manifest, generated)
    targets = {action.target_text for slide in grounded.slides for action in slide.actions}

    assert {candidate.text for candidate in catalog.candidates} <= targets


def test_long_entity_trace_keeps_full_source_binding_with_bounded_excerpt():
    lines = [
        f"Source paragraph sentence {index:02d} stays part of one paragraph." for index in range(8)
    ]
    commands = [b"BT /F1 7 Tf "]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"1 0 0 1 50 {740 - index * 8} Tm ({escaped}) Tj ".encode())
    commands.append(b"ET")
    content = _pdf_with_content(b"".join(commands))
    catalog = extract_candidates(content)
    assert [(item.kind, item.text) for item in catalog.candidates] == [
        ("paragraph", " ".join(lines))
    ]
    extraction, manifest = _load_bytes(content)

    grounded = ground_actions(
        content,
        extraction,
        manifest,
        _output(extraction, {}).model_copy(update={"source_inventory_enabled": True}),
    )
    action = next(
        action
        for slide in grounded.slides
        for action in slide.actions
        if action.target_kind == "paragraph"
    )

    assert action.target_text == " ".join(lines)
    assert action.grounding_trace is not None
    assert action.grounding_trace.target_id is not None
    assert len(action.grounding_trace.target_text or "") == 256


def test_multi_column_table_becomes_one_complete_target_per_row():
    rows = (
        ("Name", "Region", "Revenue"),
        ("North", "US", "$10M"),
        ("South", "EU", "$12M"),
        ("West", "APAC", "$8M"),
    )
    commands = [b"BT /F1 12 Tf "]
    for row_index, row in enumerate(rows):
        baseline = 700 - row_index * 30
        for x, text in zip((90, 250, 420), row, strict=True):
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"1 0 0 1 {x} {baseline} Tm ({escaped}) Tj ".encode())
    commands.append(b"ET")

    catalog = extract_candidates(_pdf_with_content(b"".join(commands)))
    rows_found = [item for item in catalog.candidates if item.kind == "table_row"]

    assert [item.text for item in rows_found] == [
        "Name | Region | Revenue",
        "North | US | $10M",
        "South | EU | $12M",
        "West | APAC | $8M",
    ]


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
