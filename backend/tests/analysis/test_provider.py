"""The external provider boundary preserves bytes and rejects untrusted output."""

import base64
import hashlib
import json
from urllib.error import HTTPError
from uuid import UUID

import pytest

from buddy_api.analysis import provider
from buddy_api.analysis.contracts import Manifest, ManifestSlide, Raster, SourcePlane
from buddy_api.analysis.provider import BedrockProvider, ProviderFailure
from buddy_api.extraction.contracts import Extraction, Slide

PDF = b"%PDF-1.7\nsynthetic authorized provider-boundary test\n%%EOF"
DECK_ID = UUID("10000000-0000-0000-0000-000000000001")
SLIDE_ID = "20000000-0000-0000-0000-000000000001"


def inputs():
    digest = hashlib.sha256(PDF).hexdigest()
    extraction = Extraction(
        source_format="pdf",
        source_revision="revision-1",
        source_sha256=digest,
        slides=(
            Slide(
                id=SLIDE_ID,
                source_id=1,
                index=0,
                width=1280 * 6350,
                height=720 * 6350,
                text="",
                notes=None,
                objects=(),
            ),
        ),
        warnings=(),
    )
    manifest = Manifest(
        version="1.0",
        deck_id=str(DECK_ID),
        render_id="pdfium-5.14.0-144dpi:" + digest,
        slides=[
            ManifestSlide(
                slide_id=SLIDE_ID,
                source=SourcePlane(
                    width=1280,
                    height=720,
                    units="source_pixels",
                    origin="top_left",
                ),
                raster=Raster(
                    path=SLIDE_ID + ".png",
                    width=1280,
                    height=720,
                    transform="scale",
                    sha256="a" * 64,
                ),
            ).model_dump(mode="json")
        ],
    )
    return extraction, manifest


class Response:
    def __init__(self, value: dict[str, object]):
        self.value = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int) -> bytes:
        return self.value[:size]


class Credentials:
    access_key = "AKIDEXAMPLE"
    secret_key = "not-a-real-secret"
    token = "synthetic-session-token"

    def get_frozen_credentials(self):
        return self


class Session:
    def get_credentials(self):
        return Credentials()


class Opener:
    def __init__(self, response: dict[str, object]):
        self.response, self.request, self.timeout = response, None, None

    def __call__(self, request, *, timeout):
        self.request, self.timeout = request, timeout
        return Response(self.response)


class OversizedPdf(bytes):
    def __len__(self) -> int:
        return provider.MAX_FILE_BYTES + 1


def response_for(*, number: int = 1, output_text: str | None = None) -> dict[str, object]:
    generated = output_text or json.dumps(
        {
            "presentation": {"text": "Quarterly results overview.", "reason": None},
            "slides": [
                {
                    "number": number,
                    "text": "Revenue increased across the reported period.",
                    "reason": None,
                    "actions": [],
                }
            ],
        }
    )
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": generated}],
            }
        ],
    }


def test_bedrock_request_submits_exact_pdf_and_disables_response_storage(monkeypatch):
    extraction, manifest = inputs()
    opener = Opener(response_for())
    monkeypatch.setattr(provider, "Session", Session)

    result = BedrockProvider("eu-central-1", opener=opener).analyze(
        PDF, extraction, str(DECK_ID), manifest
    )

    assert result.slides[0].number == 1
    assert opener.timeout == provider.TIMEOUT_SECONDS
    assert opener.request is not None
    headers = {key.lower(): value for key, value in opener.request.header_items()}
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert headers["x-amz-security-token"] == "synthetic-session-token"
    body = json.loads(opener.request.data)
    assert body["model"] == "global.openai.gpt-6-luna"
    assert body["store"] is False and body["truncation"] == "disabled"
    assert body["max_output_tokens"] == provider.MAX_OUTPUT_TOKENS
    content = body["input"][0]["content"]
    file_part = next(part for part in content if part["type"] == "input_file")
    encoded = file_part["file_data"].split(",", 1)[1]
    assert base64.b64decode(encoded, validate=True) == PDF
    assert file_part["detail"] == "high"
    schema = body["text"]["format"]["schema"]
    assert schema["properties"]["slides"]["items"]["properties"]["number"]["enum"] == [1]
    action_schema = schema["properties"]["slides"]["items"]["properties"]["actions"]
    assert action_schema["items"]["properties"]["type"]["enum"] == [
        "zoom_region",
        "highlight_region",
    ]

    def assert_supported_subset(value):
        if isinstance(value, dict):
            assert not set(value).intersection(
                {
                    "minItems",
                    "maxItems",
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "minLength",
                    "maxLength",
                }
            )
            for child in value.values():
                assert_supported_subset(child)
        elif isinstance(value, list):
            for child in value:
                assert_supported_subset(child)

    assert_supported_subset(schema)
    assert "secret" not in json.dumps(body).lower()


def test_provider_rejects_invalid_page_order_and_duplicate_output_fields(monkeypatch):
    extraction, manifest = inputs()
    monkeypatch.setattr(provider, "Session", Session)
    with pytest.raises(ProviderFailure, match="provider_slide_order"):
        BedrockProvider("eu-central-1", opener=Opener(response_for(number=2))).analyze(
            PDF, extraction, str(DECK_ID), manifest
        )

    duplicate = '{"presentation":{"text":"one","text":"two","reason":null},"slides":[]}'
    with pytest.raises(ProviderFailure, match="provider_malformed_output"):
        BedrockProvider("eu-central-1", opener=Opener(response_for(output_text=duplicate))).analyze(
            PDF, extraction, str(DECK_ID), manifest
        )


def test_provider_rejects_oversized_file_before_network(monkeypatch):
    extraction, manifest = inputs()
    opener = Opener(response_for())
    monkeypatch.setattr(provider, "Session", Session)

    with pytest.raises(ProviderFailure, match="provider_file_limit"):
        BedrockProvider("eu-central-1", opener=opener).analyze(
            OversizedPdf(PDF), extraction, str(DECK_ID), manifest
        )
    assert opener.request is None


def test_provider_rejects_oversized_deck_before_network(monkeypatch):
    extraction, manifest = inputs()
    extraction = extraction.model_copy(
        update={"slides": extraction.slides * (provider.MAX_PAGES + 1)}
    )
    opener = Opener(response_for())
    monkeypatch.setattr(provider, "Session", Session)

    with pytest.raises(ProviderFailure, match="provider_page_limit"):
        BedrockProvider("eu-central-1", opener=opener).analyze(
            PDF, extraction, str(DECK_ID), manifest
        )
    assert opener.request is None


def test_provider_access_denial_is_permanent_and_sanitized(monkeypatch):
    extraction, manifest = inputs()
    monkeypatch.setattr(provider, "Session", Session)

    def denied(_request, *, timeout):
        raise HTTPError("https://bedrock.invalid", 403, "denied", {}, None)

    with pytest.raises(ProviderFailure, match="provider_access_denied") as failure:
        BedrockProvider("eu-central-1", opener=denied).analyze(
            PDF, extraction, str(DECK_ID), manifest
        )
    assert failure.value.permanent
    assert str(failure.value) == "provider_access_denied"
