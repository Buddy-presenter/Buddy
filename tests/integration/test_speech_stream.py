"""Real Qt/network/API/PostgreSQL path; paid provider is an explicit opt-in only."""

import asyncio
import json
import os
import socket
import struct
import threading
import time
import wave

import pytest
import uvicorn
from backend.tests.support.analysis import create
from backend.tests.support.database import database_settings as database_settings
from backend.tests.support.database import migrate
from PySide6.QtCore import QTimer
from PySide6.QtMultimedia import QMediaDevices

from buddy_api.app import create_app
from buddy_api.auth import CognitoVerifier, VerifiedIdentity
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import Database
from buddy_api.speech import SpeechSettings
from buddy_desktop.config import AppConfig
from buddy_desktop.microphone import MicrophoneController
from buddy_desktop.speech import SpeechController
from buddy_identity.config import IdentityConfig, SessionExpired

CONFIG = IdentityConfig(
    "eu-central-1",
    "eu-central-1_Example",
    "client123",
    "https://buddy.auth.eu-central-1.amazoncognito.com",
)


class DeterministicProvider:
    async def open(self, start):
        self.queue = asyncio.Queue()
        self.sent = False
        self.closed = False
        return self

    async def send(self, data):
        if not self.sent:
            self.sent = True
            for final in (False, True):
                await self.queue.put(
                    json.dumps(
                        {
                            "type": "Results",
                            "start": 0,
                            "duration": 0.05,
                            "is_final": final,
                            "channel": {"alternatives": [{"transcript": "Next slide"}]},
                        }
                    )
                )

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        self.closed = True


@pytest.mark.postgres
@pytest.mark.parametrize("live", [False, True], ids=["deterministic", "live-deepgram"])
def test_qt_to_persisted_session_and_provider(database_settings, monkeypatch, qtbot, live):
    if live and os.environ.get("BUDDY_LIVE_SPEECH_TEST") != "1":
        pytest.skip("Set BUDDY_LIVE_SPEECH_TEST=1, server key, and generated test WAV explicitly.")
    asyncio.run(migrate(database_settings))

    async def seed():
        database = Database(database_settings)
        try:
            async with database.transaction() as repos:
                owner = await repos.users.create("speech-owner")
                deck = await repos.decks.create(owner_id=owner.id, title="Speech fixture")
                analysis = await create(repos, owner_id=owner.id, deck_id=deck.id)
                session = await repos.sessions.create(
                    owner_id=owner.id, deck_id=deck.id, analysis_version_id=analysis.id
                )
                session.status = "active"
                return str(session.id)
        finally:
            await database.close()

    session_id = asyncio.run(seed())

    def verify(self, token):
        if token != "speech-test-access":
            raise SessionExpired("Rejected test identity")
        return VerifiedIdentity("speech-owner", "speech@example.test")

    # Identity-provider boundary is deterministic. API verification and DB ownership are real.
    monkeypatch.setattr(CognitoVerifier, "verify", verify)
    settings = Settings(
        Environment.TEST,
        ("127.0.0.1",),
        database=database_settings,
        identity=CONFIG,
        speech=SpeechSettings.from_environment(os.environ) if live else None,
    )
    app = create_app(settings)
    provider = DeterministicProvider()
    if not live:
        app.state.speech_provider = provider
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    control = SpeechController()
    rejected_timing = []
    receive = control._message

    def inspect_timing(raw):
        # Diagnose real-provider boundary rounding without logging speech content.
        data = json.loads(raw)
        before = control._active
        previous_end = control._final_end
        previous_sequence = control._sequence
        receive(raw)
        if before and not control._active and data.get("type") == "transcript":
            rejected_timing.append(
                {
                    "start": data.get("start_seconds"),
                    "end": data.get("end_seconds"),
                    "previous_end": previous_end,
                    "sequence": data.get("sequence"),
                    "previous_sequence": previous_sequence,
                    "capture_age_ms": (time.monotonic_ns() - data.get("capture_end_ns", 0))
                    / 1_000_000,
                }
            )

    monkeypatch.setattr(control, "_message", inspect_timing)
    microphone = None
    updates, statuses = [], []
    captured_bytes, peak_sample = 0, 0
    control.transcript_received.connect(updates.append)
    control.status_changed.connect(statuses.append)
    timer = QTimer()
    try:
        qtbot.waitUntil(lambda: server.started, timeout=10000)
        audio = b"\0" * 6400
        microphone_test = live and os.environ.get("BUDDY_SPEECH_TEST_MICROPHONE") == "1"
        if live and not microphone_test:
            with wave.open(os.environ["BUDDY_SPEECH_TEST_WAV"], "rb") as source:
                assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (
                    1,
                    2,
                    16000,
                )
                audio = source.readframes(source.getnframes()) + b"\0" * 64000
        config = AppConfig(f"http://127.0.0.1:{port}")
        if microphone_test:
            from queue import Empty

            microphone = MicrophoneController()
            microphone.set_compatibility(os.environ.get("BUDDY_SPEECH_COMPATIBILITY") == "1")
            microphone.format_ready.connect(
                lambda rate: control.start(config, "speech-test-access", session_id, rate, "en-US")
            )
            microphone.status_changed.connect(statuses.append)
            microphone.audio_gap.connect(control.audio_gap)

            def send_microphone(_size):
                nonlocal captured_bytes, peak_sample
                from array import array

                while True:
                    try:
                        packet = microphone.frames.get_nowait()
                    except Empty:
                        return
                    captured_bytes += len(packet) - 8
                    samples = array("h", packet[8:])
                    peak_sample = max(peak_sample, max(map(abs, samples), default=0))
                    control.send(packet)

            microphone.audio_received.connect(send_microphone)
            hint = os.environ.get("BUDDY_SPEECH_MICROPHONE_NAME", "").casefold()
            candidates = [
                d
                for d in QMediaDevices.audioInputs()
                if not hint or hint in d.description().casefold()
            ]
            if hint:
                assert len(candidates) == 1, "Choose one exact microphone for live acceptance."
                device = candidates[0]
            else:
                device = QMediaDevices.defaultAudioInput()
            assert not device.isNull(), "No real microphone is available."
            microphone.start(bytes(device.id().data()))
        else:
            control.start(config, "speech-test-access", session_id, 16000, "en-US")
        qtbot.waitUntil(
            lambda: (
                control._ready
                or any(
                    message.startswith(("Microphone unavailable", "Microphone permission denied"))
                    for message in statuses
                )
            ),
            timeout=20000,
        )
        assert control._ready, statuses
        chunks = iter(audio[index : index + 1600] for index in range(0, len(audio), 1600))

        def send():
            chunk = next(chunks, None)
            if chunk is None:
                timer.stop()
            else:
                control.send(struct.pack(">Q", time.monotonic_ns()) + chunk)

        if microphone_test:
            duration = 30
            print(
                "MICROPHONE READY: say 'Please go to the next slide' now. "
                f"Listening for {duration} seconds.",
                flush=True,
            )
            qtbot.wait(duration * 1000)
            microphone.stop()
            qtbot.wait(1000)
        else:
            timer.timeout.connect(send)
            timer.start(50)
            qtbot.waitUntil(
                lambda: not timer.isActive() and any(e["final"] for e in updates), timeout=30000
            )
        finals = [e for e in updates if e["final"]]
        if live:
            # No credentials or transcripts: only sanitized measured evidence.
            print(
                json.dumps(
                    {
                        "provider": "deepgram",
                        "model": "nova-3",
                        "region": "EU",
                        "input": "real microphone" if microphone_test else "generated speech",
                        "partials": sum(not e["final"] for e in updates),
                        "finals": len(finals),
                        "final_latency_ms": [e["latency_ms"] for e in finals],
                        "captured_bytes": captured_bytes,
                        "peak_sample": peak_sample,
                        "statuses": statuses,
                        "rejected_timing": rejected_timing,
                    }
                )
            )
        assert "next slide" in " ".join(e["text"] for e in finals).lower()
        assert any(not e["final"] for e in updates)
        assert all(e["session_id"] == session_id and e["latency_ms"] >= 0 for e in updates)
        assert control._active, statuses
        control.stop()
        assert not control._active and not control._retry.isActive()
    finally:
        timer.stop()
        if microphone is not None:
            microphone.shutdown()
        control.stop()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()
    if not live:
        assert provider.closed
