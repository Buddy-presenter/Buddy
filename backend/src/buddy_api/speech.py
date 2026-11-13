"""Session-scoped speech streaming; provider secrets and audio stay off disk/logs."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import time
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from buddy_api.auth import Principal, authenticated
from buddy_api.presentation_sessions import owned_session

LANGUAGES = {"en-US", "en-GB", "ar", "fr", "de", "es"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeechSettings:
    api_key: str = field(repr=False)

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> SpeechSettings | None:
        key = values.get("BUDDY_DEEPGRAM_API_KEY", "").strip()
        if not key:
            return None
        if len(key) > 512 or any(char.isspace() for char in key):
            raise ValueError("Invalid BUDDY_DEEPGRAM_API_KEY.")
        return cls(key)


class StartSpeech(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    language: str = Field(min_length=2, max_length=16)
    sample_rate: int = Field(ge=8000, le=96000)


class TranscriptEvent(BaseModel):
    """Provider-independent v1 contract; partial updates share their segment ID."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: Literal["transcript"] = "transcript"
    version: Literal[1] = 1
    session_id: UUID
    stream_id: UUID
    sequence: int = Field(ge=1)
    segment_id: str
    final: bool
    text: str = Field(min_length=1, max_length=4096, repr=False)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    capture_end_ns: int = Field(gt=0)


class ProviderStream(Protocol):
    async def send(self, message: bytes | str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


class SpeechProvider(Protocol):
    async def open(self, start: StartSpeech) -> ProviderStream: ...


class SpeechFailure(Exception):
    def __init__(
        self, code: str, retryable: bool = False, *, provider_status: int | None = None
    ):
        self.code = code
        self.retryable = retryable
        self.provider_status = provider_status
        super().__init__(code)


class DeepgramProvider:
    def __init__(self, settings: SpeechSettings):
        self.settings = settings

    async def open(self, start: StartSpeech) -> ProviderStream:
        # Fixed EU destination: clients cannot select a host, model, or retention policy.
        query = urlencode(
            {
                "model": "nova-3",
                "language": start.language,
                "sample_rate": start.sample_rate,
                "encoding": "linear16",
                "channels": 1,
                "interim_results": "true",
                "endpointing": 300,
                "punctuate": "true",
                "mip_opt_out": "true",
            }
        )
        try:
            return await connect(
                "wss://api.eu.deepgram.com/v1/listen?" + query,
                additional_headers={"Authorization": "Token " + self.settings.api_key},
                open_timeout=8,
                close_timeout=2,
                max_size=65536,
                max_queue=4,
                proxy=None,
                ping_interval=10,
                ping_timeout=10,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status == 429:
                raise SpeechFailure(
                    "provider_rate_limited", True, provider_status=status
                ) from None
            if status in {401, 403}:
                raise SpeechFailure("provider_configuration", provider_status=status) from None
            if status in {400, 422}:
                raise SpeechFailure("unsupported_configuration", provider_status=status) from None
            raise SpeechFailure("provider_unavailable", True, provider_status=status) from None
        except (OSError, TimeoutError):
            raise SpeechFailure("provider_unavailable", True) from None


class SpeechBudget:
    """Per-process spend bounds, additionally bounded by ECS replica count."""

    def __init__(self) -> None:
        self.owners: set[UUID] = set()

    def acquire(self, owner: UUID) -> bool:
        if owner in self.owners or len(self.owners) >= 16:
            return False
        self.owners.add(owner)
        return True


class AudioTimeline:
    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self.samples = 0
        self.last_capture = 0
        self.points: deque[tuple[float, int]] = deque(maxlen=2048)
        self.started = time.monotonic()

    def accept(self, packet: bytes) -> bytes:
        if not 10 <= len(packet) <= 8200 or len(packet) % 2:
            raise SpeechFailure("invalid_audio")
        stamp = struct.unpack(">Q", packet[:8])[0]
        if not self.last_capture < stamp < 2**63:
            raise SpeechFailure("stale_audio")
        audio = packet[8:]
        samples = self.samples + len(audio) // 2
        if samples / self.sample_rate > (time.monotonic() - self.started) * 1.25 + 1:
            raise SpeechFailure("audio_rate_exceeded")
        self.samples = samples
        self.last_capture = stamp
        self.points.append((samples / self.sample_rate, stamp))
        return audio

    def capture_at(self, end: float) -> int:
        for audio_end, stamp in self.points:
            if audio_end + 1 / self.sample_rate >= end:
                return max(1, stamp - round(max(0, audio_end - end) * 1_000_000_000))
        raise SpeechFailure("invalid_provider_result")


def transcript_event(
    raw: str | bytes,
    timeline: AudioTimeline,
    session_id: UUID,
    stream_id: UUID,
    sequence: int,
) -> TranscriptEvent | None:
    try:
        if len(raw) > 65536:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        if data.get("type") == "Error":
            raise SpeechFailure("provider_unavailable", True)
        if data.get("type") != "Results":
            return None
        text = data["channel"]["alternatives"][0]["transcript"]
        if not isinstance(text, str) or len(text) > 4096:
            raise ValueError
        if not text.strip():
            return None
        start, duration = data["start"], data["duration"]
        if (
            type(start) not in {int, float}
            or type(duration) not in {int, float}
            or not math.isfinite(start)
            or not math.isfinite(duration)
            or start < 0
            or duration < 0
            or type(data["is_final"]) is not bool
        ):
            raise ValueError
        end = start + duration
        return TranscriptEvent(
            session_id=session_id,
            stream_id=stream_id,
            sequence=sequence,
            segment_id=f"{stream_id}:{round(start * 1_000_000)}",
            final=data["is_final"],
            text=text,
            start_seconds=start,
            end_seconds=end,
            capture_end_ns=timeline.capture_at(end),
        )
    except (ValueError, KeyError, IndexError, TypeError, OverflowError):
        raise SpeechFailure("invalid_provider_result") from None


async def verify_session(request: Request, identifier: UUID) -> Principal:
    principal = await authenticated(request, request.headers.get("authorization"))
    database = request.app.state.database
    if database is None:
        raise HTTPException(503)
    try:
        async with database.sessions() as db:
            record = await owned_session(db, principal, identifier)
            if record.status != "active":
                raise HTTPException(409)
    except SQLAlchemyError:
        raise HTTPException(503) from None
    return principal


async def relay(
    socket: WebSocket,
    provider: ProviderStream,
    request: Request,
    identifier: UUID,
    start: StartSpeech,
    stream_id: UUID,
) -> None:
    timeline = AudioTimeline(start.sample_rate)

    async def upload() -> None:
        while True:
            message = await asyncio.wait_for(socket.receive(), 10)
            if message["type"] == "websocket.disconnect":
                return
            packet = message.get("bytes")
            if packet is None:
                raise SpeechFailure("invalid_audio")
            await asyncio.wait_for(provider.send(timeline.accept(packet)), 5)

    async def download() -> None:
        sequence = 0
        while True:
            raw = await provider.recv()
            event = transcript_event(raw, timeline, identifier, stream_id, sequence + 1)
            if event is not None:
                sequence += 1
                await asyncio.wait_for(socket.send_text(event.model_dump_json()), 5)

    async def verify() -> None:
        while True:
            await asyncio.sleep(5)
            await asyncio.wait_for(verify_session(request, identifier), 10)

    tasks = [asyncio.create_task(operation()) for operation in (upload, download, verify)]
    try:
        done, _pending = await asyncio.wait(
            tasks, timeout=1800, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise SpeechFailure("stream_limit", True)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def speech_router(allowed_hosts: tuple[str, ...]) -> APIRouter:
    router = APIRouter()
    budget = SpeechBudget()

    async def error(
        socket: WebSocket, code: str, diagnostic_id: str, retryable: bool = False
    ) -> None:
        with suppress(RuntimeError, WebSocketDisconnect, TimeoutError, OSError):
            await asyncio.wait_for(
                socket.send_json(
                    {
                        "type": "error",
                        "code": code,
                        "retryable": retryable,
                        "diagnostic_id": diagnostic_id,
                    }
                ),
                2,
            )

    @router.websocket("/api/v1/sessions/{identifier}/speech")
    async def speech(socket: WebSocket, identifier: UUID) -> None:
        # Native clients use an Authorization header. Never accept URL tokens or cookies.
        try:
            hosts = socket.headers.getlist("host")
            host = urlsplit("//" + hosts[0]) if len(hosts) == 1 else None
            if (
                host is None
                or host.hostname not in allowed_hosts
                or host.username is not None
                or host.password is not None
                or (host.port is not None and not 1 <= host.port <= 65535)
                or host.path
                or host.query
                or host.fragment
                or socket.query_params
                or socket.headers.get("origin")
            ):
                await socket.close(code=1008)
                return
        except ValueError:
            await socket.close(code=1008)
            return
        # Reuse the HTTP authentication implementation, including live Cognito revocation.
        scope = dict(socket.scope, type="http", method="GET")
        request = Request(scope)
        owner: UUID | None = None
        provider: ProviderStream | None = None
        started = time.monotonic()
        outcome = "closed"
        stage = "session_verification"
        provider_status: int | None = None
        exception_type: str | None = None
        diagnostic_id = str(uuid4())
        await socket.accept()
        try:
            principal = await asyncio.wait_for(verify_session(request, identifier), 10)
            stage = "stream_admission"
            if not budget.acquire(principal.id):
                raise SpeechFailure("stream_busy", True)
            owner = principal.id
            adapter = cast(SpeechProvider | None, socket.app.state.speech_provider)
            stage = "provider_configuration"
            if adapter is None:
                raise SpeechFailure("speech_unconfigured")
            stage = "client_configuration"
            raw = await asyncio.wait_for(socket.receive_text(), 5)
            if len(raw) > 512:
                raise SpeechFailure("invalid_configuration")
            try:
                start = StartSpeech.model_validate_json(raw)
            except ValueError:
                raise SpeechFailure("invalid_configuration") from None
            if start.language not in LANGUAGES:
                raise SpeechFailure("unsupported_language")
            stage = "provider_connection"
            provider = await asyncio.wait_for(adapter.open(start), 10)
            stream_id = uuid4()
            await socket.send_json({"type": "ready", "version": 1, "stream_id": str(stream_id)})
            stage = "streaming"
            await relay(socket, provider, request, identifier, start, stream_id)
        except HTTPException as exc:
            outcome = f"session_{exc.status_code}"
            await error(socket, outcome, diagnostic_id, exc.status_code == 503)
        except SpeechFailure as exc:
            outcome = exc.code
            provider_status = exc.provider_status
            await error(socket, outcome, diagnostic_id, exc.retryable)
        except (TimeoutError, ConnectionClosed, OSError):
            outcome = "connection_lost"
            await error(socket, outcome, diagnostic_id, True)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            outcome = "speech_unavailable"
            exception_type = type(exc).__name__
            # No exception bodies: provider/network diagnostics can contain private text/tokens.
            await error(socket, outcome, diagnostic_id)
        finally:
            try:
                if provider is not None:
                    with suppress(Exception):
                        await asyncio.shield(asyncio.wait_for(provider.close(), 3))
            finally:
                if owner is not None:
                    budget.owners.discard(owner)
            if outcome != "closed":
                logger.warning(
                    "speech_stream diagnostic_id=%s outcome=%s stage=%s duration_ms=%d "
                    "provider_status=%s exception_type=%s",
                    diagnostic_id,
                    outcome,
                    stage,
                    round((time.monotonic() - started) * 1000),
                    provider_status if provider_status is not None else "none",
                    exception_type or "none",
                )
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.close(code=1000)

    return router
