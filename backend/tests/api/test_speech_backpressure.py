"""Disconnect and bounded-audio behavior while an upstream send is stalled."""

import asyncio
import struct
import time
from uuid import uuid4

import pytest

from buddy_api import speech
from buddy_api.auth import Principal


def packet(sequence):
    return struct.pack(">Q", time.monotonic_ns() + sequence) + b"\0" * 1600


class StalledProvider:
    def __init__(self):
        self.entered = asyncio.Event()
        self.cancelled = False

    async def send(self, _data):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True

    async def recv(self):
        await asyncio.Event().wait()


def test_disconnect_is_observed_while_provider_send_is_blocked(monkeypatch):
    async def run():
        provider = StalledProvider()

        class Socket:
            first = True

            async def receive(self):
                if self.first:
                    self.first = False
                    return {"type": "websocket.receive", "bytes": packet(0)}
                await provider.entered.wait()
                return {"type": "websocket.disconnect"}

        await asyncio.wait_for(
            speech.relay(
                Socket(),
                provider,
                None,
                uuid4(),
                speech.StartSpeech(version=1, language="en-US", sample_rate=16000),
                uuid4(),
                Principal(uuid4(), "generated@example.test"),
            ),
            0.5,
        )
        assert provider.cancelled

    asyncio.run(run())


def test_stalled_provider_cannot_accumulate_unbounded_audio():
    async def run():
        provider = StalledProvider()

        class Socket:
            sequence = 0

            async def receive(self):
                await asyncio.sleep(0)
                self.sequence += 1
                return {"type": "websocket.receive", "bytes": packet(self.sequence)}

        with pytest.raises(speech.SpeechFailure, match="audio_backlog") as failure:
            await asyncio.wait_for(
                speech.relay(
                    Socket(),
                    provider,
                    None,
                    uuid4(),
                    speech.StartSpeech(version=1, language="en-US", sample_rate=16000),
                    uuid4(),
                    Principal(uuid4(), "generated@example.test"),
                ),
                0.5,
            )
        assert failure.value.retryable and provider.cancelled

    asyncio.run(run())


def test_short_socket_burst_drains_without_restart():
    async def run():
        completed = asyncio.Event()

        class Provider:
            sent = 0

            async def send(self, _data):
                await asyncio.sleep(0.002)
                self.sent += 1
                if self.sent == 24:
                    completed.set()

            async def recv(self):
                await asyncio.Event().wait()

        class Socket:
            sequence = 0

            async def receive(self):
                if self.sequence < 24:
                    self.sequence += 1
                    # A short burst after scheduler/network jitter, below the
                    # existing one-second ingress grace and PCM size limit.
                    return {
                        "type": "websocket.receive",
                        "bytes": struct.pack(">Q", time.monotonic_ns()) + b"\0" * 640,
                    }
                await completed.wait()
                return {"type": "websocket.disconnect"}

        provider = Provider()
        await asyncio.wait_for(
            speech.relay(
                Socket(),
                provider,
                None,
                uuid4(),
                speech.StartSpeech(version=1, language="en-US", sample_rate=16000),
                uuid4(),
                Principal(uuid4(), "generated@example.test"),
            ),
            1,
        )
        assert provider.sent == 24

    asyncio.run(run())
