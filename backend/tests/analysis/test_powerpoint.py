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
        assert action.params.region.x == 50
        assert action.params.region.width == 300
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
    with pytest.raises(ProviderFailure, match="pptx_unknown_target"):
        analysis_document(
            PowerPointOutput.model_validate(result),
            extracted,
            manifest(extracted),
            UUID(manifest(extracted).deck_id),
            uuid4(),
            uuid4(),
        )
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
        assert body["max_tokens"] == 8000
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
    with pytest.raises(ProviderFailure, match="provider_malformed_output"):
        asyncio.run(
            provider.analyze_async(
                content, extracted, manifest(extracted).deck_id, manifest(extracted)
            )
        )
