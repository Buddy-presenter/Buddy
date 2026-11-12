"""Speech protocol boundaries and persisted ownership; provider alone is substituted."""

import asyncio
import json
import struct
import time
from pathlib import Path
from uuid import uuid4

import pytest
from backend.tests.api.test_authorization import private_api as private_api
from backend.tests.support.database import database_settings as database_settings
from fastapi.testclient import TestClient
from sqlalchemy import update
from starlette.exceptions import HTTPException
from starlette.websockets import WebSocketDisconnect
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from buddy_api import speech
from buddy_api.app import create_app
from buddy_api.auth import Principal
from buddy_api.config import Environment, Settings
from buddy_api.persistence.models import Deck, PresentationSession


def result(text="Next slide", final=True, start=0, duration=0.1):
    return json.dumps(
        {
            "type": "Results",
            "start": start,
            "duration": duration,
            "is_final": final,
            "channel": {"alternatives": [{"transcript": text}]},
        }
    )


def packet(stamp=None):
    return struct.pack(">Q", stamp or time.monotonic_ns()) + b"\x00" * 3200


class Provider:
    def __init__(self):
        self.opened = 0
        self.closed = 0
        self.audio = []
        self.queue = None
        self.failure = None

    async def open(self, start):
        self.opened += 1
        if self.failure:
            raise self.failure
        self.queue = asyncio.Queue()
        return self

    async def send(self, data):
        self.audio.append(data)
        await self.queue.put(result(final=False))
        await self.queue.put(result())

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        self.closed += 1


@pytest.fixture
def speech_client(monkeypatch):
    owner = uuid4()

    async def verify(request, identifier):
        if request.headers.get("authorization") != "Bearer owner":
            raise HTTPException(401)
        return Principal(owner, "owner@example.test")

    monkeypatch.setattr(speech, "verify_session", verify)
    app = create_app(Settings(Environment.TEST, ("testserver",)))
    provider = Provider()
    app.state.speech_provider = provider
    with TestClient(app) as client:
        yield client, provider


def start(socket, language="en-US"):
    socket.send_json({"version": 1, "language": language, "sample_rate": 16000})
    return socket.receive_json()


def test_stream_normalizes_provider_results_and_releases_connection(speech_client):
    client, provider = speech_client
    identifier = uuid4()
    with client.websocket_connect(
        f"/api/v1/sessions/{identifier}/speech", headers={"Authorization": "Bearer owner"}
    ) as socket:
        ready = start(socket)
        assert ready["type"] == "ready"
        socket.send_bytes(packet())
        partial, final = socket.receive_json(), socket.receive_json()
        assert partial["text"] == final["text"] == "Next slide"
        assert not partial["final"] and final["final"]
        assert partial["segment_id"] == final["segment_id"]
        assert partial["sequence"] == 1 and final["sequence"] == 2
        assert final["session_id"] == str(identifier)
        assert final["stream_id"] == ready["stream_id"]
        assert final["capture_end_ns"] > 0
    assert provider.closed == 1
    assert provider.audio == [b"\x00" * 3200]


@pytest.mark.parametrize(
    "headers,suffix",
    [
        ({}, ""),
        ({"Authorization": "Bearer owner"}, "?token=secret"),
        ({"Authorization": "Bearer owner", "Origin": "https://foreign.example"}, ""),
        ({"Authorization": "Bearer owner", "Host": "foreign.example"}, ""),
    ],
)
def test_unauthorized_or_browser_stream_does_not_open_provider(speech_client, headers, suffix):
    client, provider = speech_client
    try:
        with client.websocket_connect(
            f"/api/v1/sessions/{uuid4()}/speech" + suffix, headers=headers
        ) as socket:
            assert socket.receive_json()["code"] == "session_401"
    except WebSocketDisconnect as exc:
        assert exc.code == 1008
    assert provider.opened == 0


@pytest.mark.parametrize(
    "configuration,code",
    [
        ({"version": 1, "language": "invalid", "sample_rate": 16000}, "unsupported_language"),
        ({"version": 1, "language": "en-US", "sample_rate": 0}, "invalid_configuration"),
        (
            {"version": 1, "language": "en-US", "sample_rate": 16000, "api_key": "secret"},
            "invalid_configuration",
        ),
    ],
)
def test_invalid_start_never_opens_provider(speech_client, configuration, code):
    client, provider = speech_client
    with client.websocket_connect(
        f"/api/v1/sessions/{uuid4()}/speech", headers={"Authorization": "Bearer owner"}
    ) as socket:
        socket.send_json(configuration)
        assert socket.receive_json()["code"] == code
    assert provider.opened == 0


@pytest.mark.parametrize(
    "failure",
    [
        speech.SpeechFailure("provider_rate_limited", True),
        speech.SpeechFailure("provider_configuration"),
    ],
)
def test_provider_failure_is_sanitized_and_budget_released(speech_client, failure):
    client, provider = speech_client
    provider.failure = failure
    for _ in range(2):
        with client.websocket_connect(
            f"/api/v1/sessions/{uuid4()}/speech", headers={"Authorization": "Bearer owner"}
        ) as socket:
            error = start(socket)
            assert error == {"type": "error", "code": failure.code, "retryable": failure.retryable}


def test_duplicate_stream_is_rejected(speech_client):
    client, provider = speech_client
    url = f"/api/v1/sessions/{uuid4()}/speech"
    with client.websocket_connect(url, headers={"Authorization": "Bearer owner"}) as first:
        assert start(first)["type"] == "ready"
        with client.websocket_connect(url, headers={"Authorization": "Bearer owner"}) as second:
            assert second.receive_json()["code"] == "stream_busy"
    assert provider.opened == 1


@pytest.mark.parametrize("audio", [b"short", b"x" * 8202, struct.pack(">Q", 0) + b"\0\0"])
def test_bad_audio_closes_provider(speech_client, audio):
    client, provider = speech_client
    with client.websocket_connect(
        f"/api/v1/sessions/{uuid4()}/speech", headers={"Authorization": "Bearer owner"}
    ) as socket:
        start(socket)
        socket.send_bytes(audio)
        assert socket.receive_json()["type"] == "error"
    assert not provider.audio and provider.closed == 1


def test_timeline_rejects_replay_and_audio_flood():
    timeline = speech.AudioTimeline(16000)
    timeline.accept(packet(100))
    with pytest.raises(speech.SpeechFailure, match="stale_audio"):
        timeline.accept(packet(100))
    with pytest.raises(speech.SpeechFailure, match="audio_rate_exceeded"):
        for index in range(100):
            timeline.accept(packet(101 + index))


def test_provider_timestamp_rounding_at_audio_boundary():
    timeline = speech.AudioTimeline(16000)
    for stamp in (800_000_000, 900_000_000, 1_000_000_000):
        timeline.accept(packet(stamp))
    assert timeline.capture_at(0.1 + 0.2) == 1_000_000_000


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", result(duration=float("nan")), result(text="x" * 4097), result(duration=2)],
)
def test_invalid_provider_payload_cannot_escape(raw):
    timeline = speech.AudioTimeline(16000)
    timeline.accept(packet())
    with pytest.raises(speech.SpeechFailure):
        speech.transcript_event(raw, timeline, uuid4(), uuid4(), 1)


def test_key_is_hidden_and_optional():
    assert speech.SpeechSettings.from_environment({}) is None
    assert "secret" not in repr(speech.SpeechSettings("secret"))
    with pytest.raises(ValueError):
        speech.SpeechSettings.from_environment({"BUDDY_DEEPGRAM_API_KEY": "bad key"})


def test_transcript_schema_matches_documented_contract():
    path = Path(__file__).resolve().parents[3] / "docs/speech-transcript-v1.schema.json"
    assert json.loads(path.read_text()) == speech.TranscriptEvent.model_json_schema()


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "provider_configuration", False),
        (403, "provider_configuration", False),
        (429, "provider_rate_limited", True),
        (400, "unsupported_configuration", False),
        (503, "provider_unavailable", True),
    ],
)
def test_adapter_errors_and_fixed_privacy_destination(monkeypatch, status, code, retryable):
    from urllib.parse import parse_qs, urlsplit

    async def connect(url, **options):
        parsed = urlsplit(url)
        assert parsed.scheme == "wss" and parsed.netloc == "api.eu.deepgram.com"
        query = parse_qs(parsed.query)
        assert query["mip_opt_out"] == ["true"] and query["model"] == ["nova-3"]
        assert query["sample_rate"] == ["48000"] and query["encoding"] == ["linear16"]
        assert query["channels"] == ["1"] and query["interim_results"] == ["true"]
        assert options["additional_headers"] == {"Authorization": "Token provider-secret"}
        assert options["max_queue"] == 4 and options["open_timeout"] == 8
        raise InvalidStatus(Response(status, "Failure", Headers(), b"private provider body"))

    monkeypatch.setattr(speech, "connect", connect)
    adapter = speech.DeepgramProvider(speech.SpeechSettings("provider-secret"))
    with pytest.raises(speech.SpeechFailure) as caught:
        asyncio.run(
            adapter.open(speech.StartSpeech(version=1, language="en-US", sample_rate=48000))
        )
    assert caught.value.code == code and caught.value.retryable == retryable
    assert "secret" not in str(caught.value) and "private" not in str(caught.value)


@pytest.mark.postgres
@pytest.mark.parametrize(
    "change,code",
    [("ended", "session_409"), ("deleted", "session_404"), ("revoked", "session_401")],
)
def test_active_stream_rechecks_access_and_closes_provider(private_api, monkeypatch, change, code):
    from datetime import UTC, datetime

    from buddy_api.auth import CognitoVerifier
    from buddy_identity.config import SessionExpired

    client, ids, _owner, _other = private_api
    provider = Provider()
    client.app.state.speech_provider = provider

    async def set_values(model, identifier, values):
        async with client.app.state.database.sessions() as db, db.begin():
            await db.execute(update(model).where(model.id == identifier).values(**values))

    async def activate():
        await set_values(PresentationSession, ids["sessions"], {"status": "active"})

    client.portal.call(activate)
    with client.websocket_connect(
        f"/api/v1/sessions/{ids['sessions']}/speech", headers={"Authorization": "Bearer owner"}
    ) as socket:
        assert start(socket)["type"] == "ready"
        if change == "revoked":

            def revoke(self, token):
                raise SessionExpired("Revoked identity")

            monkeypatch.setattr(CognitoVerifier, "verify", revoke)
        else:

            async def mutate():
                if change == "ended":
                    await set_values(PresentationSession, ids["sessions"], {"status": "ended"})
                else:
                    await set_values(
                        Deck, ids["decks"], {"deletion_requested_at": datetime.now(UTC)}
                    )

            client.portal.call(mutate)
        assert socket.receive_json() == {"type": "error", "code": code, "retryable": False}
    assert provider.closed == 1


@pytest.mark.postgres
def test_real_session_ownership_lifecycle_and_deletion(private_api):
    client, ids, _owner, _other = private_api
    provider = Provider()
    client.app.state.speech_provider = provider
    identifier = ids["sessions"]

    async def change(model, values):
        async with client.app.state.database.sessions() as db, db.begin():
            target = identifier if model is PresentationSession else ids["decks"]
            await db.execute(update(model).where(model.id == target).values(**values))

    async def activate():
        await change(PresentationSession, {"status": "active"})

    client.portal.call(activate)
    url = f"/api/v1/sessions/{identifier}/speech"
    for token in ("other", "revoked"):
        with client.websocket_connect(url, headers={"Authorization": "Bearer " + token}) as socket:
            assert socket.receive_json()["code"] in {"session_401", "session_403"}
    with client.websocket_connect(url, headers={"Authorization": "Bearer owner"}) as socket:
        assert start(socket)["type"] == "ready"
        socket.send_bytes(packet())
        assert socket.receive_json()["type"] == "transcript"

    async def end():
        await change(PresentationSession, {"status": "ended"})

    client.portal.call(end)
    with client.websocket_connect(url, headers={"Authorization": "Bearer owner"}) as socket:
        assert socket.receive_json()["code"] == "session_409"
    from datetime import UTC, datetime

    async def delete():
        await change(Deck, {"deletion_requested_at": datetime.now(UTC)})

    client.portal.call(delete)
    with client.websocket_connect(url, headers={"Authorization": "Bearer owner"}) as socket:
        assert socket.receive_json()["code"] == "session_404"
