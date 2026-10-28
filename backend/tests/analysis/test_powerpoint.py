import asyncio
import copy
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from backend.tests.support.powerpoint import manifest, output, source
from pydantic import ValidationError

from buddy_api.analysis.contracts import (
    AnalysisInput,
    ContentAction,
    PowerPointAnalysisInput,
    PowerPointContentAction,
)
from buddy_api.analysis.powerpoint import (
    ACTION_TYPES,
    MAX_CONTEXT_BYTES,
    PowerPointOutput,
    PowerPointProvider,
    analysis_document,
    powerpoint_manifest,
)
from buddy_api.analysis.provider import BedrockProvider, ProviderFailure
from buddy_api.validation.config import ValidatorSettings
from buddy_api.validation.powerpoint import PowerPointProcessRequest, powerpoint_validation_request
from buddy_api.validation.runner import run_validation


@pytest.mark.parametrize("width", [960, 720])
def test_real_extraction_object_bounds_all_powerpoint_actions(width):
    content, extracted = source(width)
    document = analysis_document(
        output(extracted, ACTION_TYPES),
        extracted,
        manifest(extracted),
        UUID(manifest(extracted).deck_id),
        uuid4(),
        uuid4(),
    )
    assert document.source_format == "pptx" and document.office_safe
    assert document.slides[0].summary.text.find("Notes:") >= 0
    assert document.manifest.slides[0].source.width == width
    assert set(a.type for a in document.slides[0].actions) == set(ACTION_TYPES)
    for action in document.slides[0].actions:
        geometric = action.type in {"draw_diamond", "draw_triangle", "draw_hexagon", "draw_star"}
        assert action.params.region.x == (500 if geometric else 50)
        assert action.params.region.width == (64 if geometric else 300)
        with pytest.raises(ValueError):
            ContentAction.model_validate(action.model_dump(mode="json"))
    with pytest.raises(ValueError):
        AnalysisInput.model_validate(document.model_dump(mode="json"))
    assert powerpoint_manifest(extracted, str(document.manifest.deck_id)).source_format == "pptx"
    assert content[:2] == b"PK"


def test_pdf_actions_and_pdf_sources_are_rejected_in_powerpoint_path():
    content, extraction = source()
    raw = analysis_document(
        output(extraction),
        extraction,
        manifest(extraction),
        UUID(manifest(extraction).deck_id),
        uuid4(),
        uuid4(),
    ).model_dump(mode="json")
    bad = copy.deepcopy(raw)
    bad["slides"][0]["actions"][0]["version"] = "1.1"
    bad["slides"][0]["actions"][0]["source_format"] = "pdf"
    with pytest.raises(ValueError):
        PowerPointAnalysisInput.model_validate(bad)
    with pytest.raises(ProviderFailure, match="pdf_source_mismatch"):
        BedrockProvider("eu-central-1")._signed_request(
            content, extraction, "deck", manifest(extraction)
        )
    pdf = extraction.model_copy(update={"source_format": "pdf"})
    with pytest.raises(ProviderFailure, match="pptx_source_mismatch"):
        PowerPointProvider("eu-central-1").request_body(b"%PDF-1.7", pdf, manifest(extraction))


def test_text_targets_are_grounded_and_decorative_text_frames_are_omitted():
    _, extraction = source()
    raw = output(extraction, ("highlight_region", "draw_star", "draw_diamond")).model_dump(
        mode="json"
    )
    for slide, generated in zip(extraction.slides, raw["slides"], strict=True):
        generated["actions"][0]["target_text"] = slide.objects[0].text
        for action in generated["actions"][1:]:
            action["object_id"] = slide.objects[0].id
    generated = PowerPointOutput.model_validate(raw)
    document = analysis_document(
        generated,
        extraction,
        manifest(extraction),
        UUID(manifest(extraction).deck_id),
        uuid4(),
        uuid4(),
    )
    for result, slide in zip(document.slides, extraction.slides, strict=True):
        assert [a.type for a in result.actions] == ["highlight_region"]
        assert result.actions[0].params.target_text == slide.objects[0].text
        assert result.actions[0].params.source_shape_id == slide.objects[0].source_id
    raw["slides"][0]["actions"][0]["target_text"] = "a claim absent from the source"
    filtered = analysis_document(
        PowerPointOutput.model_validate(raw),
        extraction,
        manifest(extraction),
        UUID(manifest(extraction).deck_id),
        uuid4(),
        uuid4(),
    )
    assert filtered.slides[0].actions == ()
    assert len(filtered.slides[1].actions) == 1


def test_two_different_phrases_can_be_highlighted_in_one_text_box():
    _, extraction = source()
    raw = output(extraction).model_dump(mode="json")
    for slide, generated in zip(extraction.slides, raw["slides"], strict=True):
        action = generated["actions"][0]
        action.update(object_id=slide.objects[1].id, target_text="Revenue")
        generated["actions"].append(dict(action, target_text="12%"))
    document = analysis_document(
        PowerPointOutput.model_validate(raw),
        extraction,
        manifest(extraction),
        UUID(manifest(extraction).deck_id),
        uuid4(),
        uuid4(),
    )
    assert [a.params.target_text for a in document.slides[0].actions] == ["Revenue", "12%"]
    from buddy_api.processing.service import Processor

    assert Processor._duplicate_candidate_ids(document.slides) == set()


def test_tiny_and_duplicate_suggestions_do_not_fail_the_complete_analysis():
    _, extraction = source()
    slide = extraction.slides[0]
    tiny = slide.objects[1].model_copy(
        update={"bounds": slide.objects[1].bounds.model_copy(update={"height": 5 * 12700})}
    )
    extraction = extraction.model_copy(
        update={
            "slides": (
                slide.model_copy(update={"objects": (slide.objects[0], tiny, *slide.objects[2:])}),
                *extraction.slides[1:],
            )
        }
    )
    generated = output(extraction).model_dump(mode="json")
    valid = generated["slides"][0]["actions"][0]
    generated["slides"][0]["actions"].extend([dict(valid), dict(valid, object_id=tiny.id)])
    document = analysis_document(
        PowerPointOutput.model_validate(generated),
        extraction,
        manifest(extraction),
        UUID(manifest(extraction).deck_id),
        uuid4(),
        uuid4(),
    )
    assert len(document.slides[0].actions) == 1
    assert document.slides[0].summary.text == generated["slides"][0]["summary"]["text"]
    assert document.presentation_summary.text == generated["presentation"]["text"]


@pytest.mark.parametrize(
    "kind", ["zoom_region", "next_slide", "erase_drawing", "draw_freehand", "run_code"]
)
def test_generated_catalog_rejects_noncontent_or_pdf_only_actions(kind):
    _, extracted = source()
    raw = (
        analysis_document(
            output(extracted),
            extracted,
            manifest(extracted),
            UUID(manifest(extracted).deck_id),
            uuid4(),
            uuid4(),
        )
        .slides[0]
        .actions[0]
        .model_dump(mode="json")
    )
    raw["type"] = kind
    with pytest.raises(ValidationError):
        PowerPointContentAction.model_validate(raw)


def test_budgets_unknown_objects_and_source_mismatch():
    content, extracted = source()
    result = output(extracted).model_dump(mode="json")
    result["slides"][0]["actions"][0]["object_id"] = "untrusted-object"
    filtered = analysis_document(
        PowerPointOutput.model_validate(result),
        extracted,
        manifest(extracted),
        UUID(manifest(extracted).deck_id),
        uuid4(),
        uuid4(),
    )
    assert filtered.slides[0].actions == ()
    assert len(filtered.slides[1].actions) == 1
    result = output(extracted).model_dump(mode="json")
    result["slides"][0]["actions"] *= 41
    with pytest.raises(ValidationError):
        PowerPointOutput.model_validate(result)
    oversized = extracted.model_copy(
        update={
            "slides": (
                extracted.slides[0].model_copy(update={"text": "x" * MAX_CONTEXT_BYTES}),
                *extracted.slides[1:],
            )
        }
    )
    with pytest.raises(ProviderFailure, match="pptx_context_limit"):
        PowerPointProvider("eu-central-1").request_body(content, oversized, manifest(oversized))
    with pytest.raises(ProviderFailure, match="pptx_source_mismatch"):
        PowerPointProvider("eu-central-1").request_body(
            content + b"tampered", extracted, manifest(extracted)
        )


def test_provider_transport_structured_output_and_recoverable_failures(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-test-secret")
    content, extracted = source()
    generated = output(extracted)
    calls = []

    def handle(request):
        body = json.loads(request.content)
        assert "original PDF" not in body["messages"][0]["content"]
        assert extracted.source_sha256 in body["messages"][0]["content"]
        assert "Discuss the measured pilot results" in body["messages"][0]["content"]
        assert body["max_tokens"] == 16000
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "submit_powerpoint_analysis",
                        "input": generated.model_dump(mode="json"),
                    }
                ],
            },
        )

    provider = PowerPointProvider("eu-central-1", transport=httpx.MockTransport(handle))
    assert (
        asyncio.run(
            provider.analyze_async(
                content, extracted, manifest(extracted).deck_id, manifest(extracted)
            )
        )
        == generated
    )
    assert len(calls) == 1
    denied = PowerPointProvider(
        "eu-central-1", transport=httpx.MockTransport(lambda _: httpx.Response(403))
    )
    with pytest.raises(ProviderFailure, match="provider_access_denied"):
        asyncio.run(
            denied.analyze_async(
                content, extracted, manifest(extracted).deck_id, manifest(extracted)
            )
        )


def test_powerpoint_schema_packaging_and_stale_validator_source():
    import sys

    root = Path(__file__).resolve().parents[3]
    assert (
        root / "backend/src/buddy_api/analysis/powerpoint_contracts/action-v2.schema.json"
    ).read_bytes() == (
        root / "frontend/src/buddy_desktop/powerpoint_contracts/action-v2.schema.json"
    ).read_bytes()
    content, extracted = source()
    deck = uuid4()
    doc = analysis_document(
        output(extracted), extracted, manifest(extracted, deck), deck, uuid4(), uuid4()
    )
    request = PowerPointProcessRequest(
        **powerpoint_validation_request(doc.slides[0].actions[0], doc).model_dump(),
        deck_id=deck,
        source_revision=extracted.source_revision,
        source_sha256="f" * 64,
    )
    result = run_validation(content, request, ValidatorSettings(python=sys.executable))
    assert result.result.state == "needs-review"
    assert result.result.reasons == ["powerpoint_source_mismatch"]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"stop_reason": "tool_use", "content": ["invalid"]},
        {"stop_reason": "tool_use", "content": []},
        {"stop_reason": "tool_use", "content": {"unexpected": "object"}},
    ],
)
def test_malformed_provider_envelopes_are_recoverable_failures(monkeypatch, payload):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-test-secret")
    content, extracted = source()
    provider = PowerPointProvider(
        "eu-central-1", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    with pytest.raises(ProviderFailure, match="provider_malformed_output") as failed:
        asyncio.run(
            provider.analyze_async(
                content, extracted, manifest(extracted).deck_id, manifest(extracted)
            )
        )
    assert not failed.value.permanent


@pytest.mark.parametrize("ambiguous", [False, True])
def test_provider_accepts_commentary_but_never_ambiguous_tool_calls(monkeypatch, ambiguous):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-test-secret")
    content, extracted = source()
    tool = {
        "type": "tool_use",
        "name": "submit_powerpoint_analysis",
        "input": output(extracted).model_dump(mode="json"),
    }
    blocks = [{"type": "text", "text": "Here is the analysis."}, tool]
    if ambiguous:
        blocks.append(dict(tool))
    provider = PowerPointProvider(
        "eu-central-1",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"stop_reason": "tool_use", "content": blocks})
        ),
    )

    async def run():
        return await provider.analyze_async(
            content, extracted, manifest(extracted).deck_id, manifest(extracted)
        )

    if ambiguous:
        with pytest.raises(ProviderFailure, match="provider_malformed_output"):
            asyncio.run(run())
    else:
        assert asyncio.run(run()) == output(extracted)


@pytest.mark.parametrize("text", ["Grounded presentation summary.", "", "x" * 16001])
def test_plain_summary_text_is_normalized_without_weakening_validation(monkeypatch, text):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-test-secret")
    content, extracted = source()
    generated = output(extracted).model_dump(mode="json")
    generated["presentation"] = text
    generated["slides"][0]["summary"] = "Exact slide summary."
    envelope = {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "submit_powerpoint_analysis", "input": generated}],
    }
    provider = PowerPointProvider(
        "eu-central-1", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=envelope))
    )

    async def run():
        return await provider.analyze_async(
            content, extracted, manifest(extracted).deck_id, manifest(extracted)
        )

    if not text or len(text) > 16000:
        with pytest.raises(ProviderFailure, match="provider_malformed_output"):
            asyncio.run(run())
    else:
        parsed = asyncio.run(run())
        assert parsed.presentation.text == text and parsed.presentation.reason is None
        assert parsed.slides[0].summary.text == "Exact slide summary."


@pytest.mark.parametrize("count", [100, 101])
def test_comprehensive_candidate_budget_is_bounded(count):
    _, extracted = source()
    action = output(extracted).model_dump(mode="json")["slides"][0]["actions"][0]
    raw = output(extracted).model_dump(mode="json")
    raw["slides"] = [
        dict(
            number=i + 1,
            summary={"text": "Grounded summary.", "reason": None},
            actions=[dict(action) for _ in range(10 + (i == 9 and count == 101))],
        )
        for i in range(10)
    ]
    if count == 101:
        with pytest.raises(ValidationError, match="budget"):
            PowerPointOutput.model_validate(raw)
    else:
        assert sum(len(s.actions) for s in PowerPointOutput.model_validate(raw).slides) == 100
