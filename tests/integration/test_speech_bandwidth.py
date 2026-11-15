"""Actual resampler/relay under a controlled bandwidth ceiling, without paid providers."""

import asyncio
import struct
import time
from uuid import uuid4

import pytest

from buddy_api import speech
from buddy_api.auth import Principal
from buddy_desktop.speech_audio import PCM16Resampler


@pytest.mark.parametrize(
    "normalize", [False, True], ids=["native-48k-backlogs", "speech-16k-sustains"]
)
def test_native_audio_normalization_stays_within_constrained_connection(normalize):
    async def run():
        wire_rate = 16000 if normalize else 48000
        resampler = PCM16Resampler(48000) if normalize else None
        chunks = []
        for _ in range(45):
            pcm = b"\0" * 3200  # 1600 samples at the native 48 kHz capture rate.
            chunks.append(resampler.encode(pcm) if resampler else pcm)
        expected = sum(map(len, chunks))

        class Provider:
            sent = 0

            async def send(self, data):
                await asyncio.sleep(len(data) / 40000)  # 40 kB/s usable upstream capacity.
                self.sent += len(data)

            async def recv(self):
                await asyncio.Event().wait()

        provider = Provider()

        class Socket:
            index = 0

            async def receive(self):
                while self.index < len(chunks):
                    await asyncio.sleep(1 / 30)
                    pcm = chunks[self.index]
                    self.index += 1
                    if pcm:
                        return {
                            "type": "websocket.receive",
                            "bytes": struct.pack(">Q", time.monotonic_ns()) + pcm,
                        }
                while provider.sent < expected:
                    await asyncio.sleep(0.01)
                return {"type": "websocket.disconnect"}

        task = speech.relay(
            Socket(),
            provider,
            None,
            uuid4(),
            speech.StartSpeech(version=1, language="en-US", sample_rate=wire_rate),
            uuid4(),
            Principal(uuid4(), "generated@example.test"),
        )
        if normalize:
            await asyncio.wait_for(task, 4)
            assert provider.sent == expected
            assert expected < 48000  # <1/3 of native bytes, excluding pending filter tail.
        else:
            with pytest.raises(speech.SpeechFailure, match="audio_backlog"):
                await asyncio.wait_for(task, 4)

    asyncio.run(run())
