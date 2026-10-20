"""The external provider boundary preserves bytes and rejects untrusted output."""

import asyncio
import base64
import hashlib
import json
from uuid import UUID

import httpx
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
        self.response, self.request = response, None

        async def handler(request):
            self.request = request
            return httpx.Response(200, json=self.response)

        self.transport = httpx.MockTransport(handler)


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
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": generated}],
    }


def test_bedrock_request_submits_exact_pdf_with_native_claude_api(monkeypatch):
    extraction, manifest = inputs()
    opener = Opener(response_for())
    monkeypatch.setattr(provider, "Session", Session)

    result = BedrockProvider("eu-central-1", transport=opener.transport).analyze(
        PDF, extraction, str(DECK_ID), manifest
    )

    assert result.slides[0].number == 1
    assert opener.request is not None
    headers = opener.request.headers
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert headers["x-amz-security-token"] == "synthetic-session-token"
    body = json.loads(opener.request.content)
    assert opener.request.url.path.endswith("/model/eu.anthropic.claude-sonnet-4-6/invoke")
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["max_tokens"] == provider.MAX_OUTPUT_TOKENS
    assert body["temperature"] == 0
    content = body["messages"][0]["content"]
    document = next(part for part in content if part["type"] == "document")
    assert document["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(document["source"]["data"], validate=True) == PDF
    assert "exactly one of these forms" in next(
        part["text"] for part in content if part["type"] == "text"
    )
    assert "call content a table only when it is visibly arranged in rows and columns" in next(
        part["text"] for part in content if part["type"] == "text"
    )
    schema = body["output_config"]["format"]["schema"]
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
        BedrockProvider("eu-central-1", transport=Opener(response_for(number=2)).transport).analyze(
            PDF, extraction, str(DECK_ID), manifest
        )


def test_provider_rejects_conflicting_summary_availability(monkeypatch):
    extraction, manifest = inputs()
    monkeypatch.setattr(provider, "Session", Session)
    conflicting = json.dumps(
        {
            "presentation": {"text": "A readable summary.", "reason": "also unavailable"},
            "slides": [
                {
                    "number": 1,
                    "text": "A readable page.",
                    "reason": None,
                    "actions": [],
                }
            ],
        }
    )
    with pytest.raises(ProviderFailure, match="provider_malformed_output"):
        BedrockProvider(
            "eu-central-1", transport=Opener(response_for(output_text=conflicting)).transport
        ).analyze(PDF, extraction, str(DECK_ID), manifest)

    duplicate = '{"presentation":{"text":"one","text":"two","reason":null},"slides":[]}'
    with pytest.raises(ProviderFailure, match="provider_malformed_output"):
        BedrockProvider(
            "eu-central-1", transport=Opener(response_for(output_text=duplicate)).transport
        ).analyze(PDF, extraction, str(DECK_ID), manifest)


def test_provider_rejects_oversized_file_before_network(monkeypatch):
    extraction, manifest = inputs()
    opener = Opener(response_for())
    monkeypatch.setattr(provider, "Session", Session)

    with pytest.raises(ProviderFailure, match="provider_file_limit"):
        BedrockProvider("eu-central-1", transport=opener.transport).analyze(
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
        BedrockProvider("eu-central-1", transport=opener.transport).analyze(
            PDF, extraction, str(DECK_ID), manifest
        )
    assert opener.request is None


def test_provider_access_denial_is_permanent_and_sanitized(monkeypatch):
    extraction, manifest = inputs()
    monkeypatch.setattr(provider, "Session", Session)

    denied = httpx.MockTransport(lambda _request: httpx.Response(403, text="denied"))

    with pytest.raises(ProviderFailure, match="provider_access_denied") as failure:
        BedrockProvider("eu-central-1", transport=denied).analyze(
            PDF, extraction, str(DECK_ID), manifest
        )
    assert failure.value.permanent
    assert str(failure.value) == "provider_access_denied"


def test_async_provider_uses_signed_request_and_returns_validated_output(monkeypatch):
    extraction, manifest = inputs()
    monkeypatch.setattr(provider, "Session", Session)
    captured = {}

    async def handler(request):
        captured["authorization"] = request.headers["authorization"]
        body = json.loads(request.content)
        document = next(
            part for part in body["messages"][0]["content"] if part["type"] == "document"
        )
        captured["pdf"] = base64.b64decode(document["source"]["data"], validate=True)
        return httpx.Response(200, json=response_for())

    async def scenario():
        transport = httpx.MockTransport(handler)
        result = await BedrockProvider("eu-central-1", transport=transport).analyze_async(
            PDF, extraction, str(DECK_ID), manifest
        )
        assert result.slides[0].number == 1

    asyncio.run(scenario())
    assert captured["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert captured["pdf"] == PDF


def test_async_provider_deadline_stops_trickling_response(monkeypatch):
    extraction, manifest = inputs()
    monkeypatch.setattr(provider, "Session", Session)
    monkeypatch.setattr(provider, "TIMEOUT_SECONDS", 0.04)

    class Trickle(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = asyncio.Event()

        async def __aiter__(self):
            try:
                while True:
                    yield b" "
                    await asyncio.sleep(0.005)
            finally:
                self.closed.set()

        async def aclose(self):
            self.closed.set()

    async def scenario():
        stream = Trickle()
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))
        with pytest.raises(ProviderFailure, match="provider_unavailable"):
            await BedrockProvider("eu-central-1", transport=transport).analyze_async(
                PDF, extraction, str(DECK_ID), manifest
            )
        assert stream.closed.is_set()

    asyncio.run(scenario())


def test_async_provider_cancellation_closes_outstanding_response(monkeypatch):
    extraction, manifest = inputs()
    monkeypatch.setattr(provider, "Session", Session)

    class WaitingStream(httpx.AsyncByteStream):
        def __init__(self):
            self.started, self.closed = asyncio.Event(), asyncio.Event()

        async def __aiter__(self):
            self.started.set()
            try:
                await asyncio.Event().wait()
                yield b"never"
            finally:
                self.closed.set()

        async def aclose(self):
            self.closed.set()

    async def scenario():
        stream = WaitingStream()
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))
        task = asyncio.create_task(
            BedrockProvider("eu-central-1", transport=transport).analyze_async(
                PDF, extraction, str(DECK_ID), manifest
            )
        )
        await asyncio.wait_for(stream.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed.is_set()

    asyncio.run(scenario())
