"""Real Qt/network/API/PostgreSQL path; paid provider is an explicit opt-in only."""

import asyncio
import json
import os
import socket
import struct
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest
import uvicorn
from backend.tests.support.analysis import create
from backend.tests.support.database import database_settings as database_settings
from backend.tests.support.database import migrate
from PySide6.QtCore import QTimer
from PySide6.QtMultimedia import QMediaDevices
from sqlalchemy import select

from buddy_api import session_events
from buddy_api.app import create_app
from buddy_api.auth import CognitoVerifier, VerifiedIdentity
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import AnalysisSlide, ProcessingJob, SessionEvent
from buddy_api.speech import SpeechSettings
from buddy_desktop.candidate_service import CandidateService
from buddy_desktop.config import AppConfig
from buddy_desktop.microphone import MicrophoneController
from buddy_desktop.presentation_session_service import parse_snapshot
from buddy_desktop.session_events import SessionEventStream
from buddy_desktop.speech import SpeechController
from buddy_desktop.transcript_context import TranscriptContext
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
        self.audio_bytes = getattr(self, "audio_bytes", 0) + len(data)
        if not self.sent and self.audio_bytes >= 4800:
            self.sent = True
            for start, text in ((0, "Revenue growth"), (0.1, "Next slide")):
                for final in (False, True):
                    await self.queue.put(
                        json.dumps(
                            {
                                "type": "Results",
                                "start": start,
                                "duration": 0.05,
                                "is_final": final,
                                "channel": {"alternatives": [{"transcript": text}]},
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
                db = repos.users.session
                slides = list(
                    (
                        await db.scalars(
                            select(AnalysisSlide)
                            .where(AnalysisSlide.analysis_id == analysis.id)
                            .order_by(AnalysisSlide.position)
                        )
                    ).all()
                )
                job = await db.get(ProcessingJob, analysis.job_id)
                deck.source_key = "synthetic-source"
                deck.source_revision = analysis.provenance["source_revision"]
                deck.source_sha256 = analysis.provenance["source_sha256"]
                deck.source_size_bytes = 1024
                deck.source_version_id = "synthetic-version"
                deck.source_slide_count = len(slides)
                job.status = "succeeded"
                job.source_revision = deck.source_revision
                analysis.publication = "published"
                session.status = "active"
                session.active_slide_id = slides[0].slide_id
                return str(session.id), slides[1].slide_id
        finally:
            await database.close()

    session_id, second_slide_id = asyncio.run(seed())

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
    if not live:
        # Exercise the production client's reconnect against the real SSE route.
        monkeypatch.setattr(session_events, "STREAM_SECONDS", 1)
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
    observers = []
    observed = [[], []]
    connections = [[], []]
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
        endpoint = config.api_url + f"/api/v1/sessions/{session_id}"
        headers = {"Authorization": "Bearer speech-test-access"}
        with urlopen(Request(endpoint, headers=headers), timeout=5) as response:
            initial = parse_snapshot(json.load(response))
        context = TranscriptContext()
        context.bind(initial.id, initial.state_version, "en-US")
        control.transcript_received.connect(context.accept)
        control.cleared.connect(context.invalidate)
        for index in range(2):
            observer = SessionEventStream()
            observers.append(observer)

            def receive_event(generation, event, *, position=index, stream=observer):
                observed[position].append(event)
                stream.acknowledge(generation)

            observer.received.connect(receive_event)
            observer.connection_changed.connect(
                lambda _generation, state, position=index: connections[position].append(state)
            )
            observer.bind(config, "speech-test-access", initial)
        qtbot.waitUntil(lambda: all(items for items in observed), timeout=10000)
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

        if not live:
            snapshot = context.snapshot()
            assert snapshot.fresh
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    CandidateService(config).resolve, "speech-test-access", initial, snapshot
                ).result(timeout=10)
            assert result["state_version"] == initial.state_version
            assert result["context_epoch"] == snapshot.epoch
            assert result["candidates"][0]["kind"] == "no_change"
            assert snapshot.text == "Revenue growth Next slide"
            assert any(c["reason"] == "explicit_navigation" for c in result["candidates"])

        async def journal():
            database = Database(database_settings)
            try:
                async with database.sessions() as db:
                    rows = (
                        await db.scalars(select(SessionEvent).order_by(SessionEvent.sequence))
                    ).all()
                    return [row.document for row in rows]
            finally:
                await database.close()

        speech_events = [
            event["payload"]
            for event in asyncio.run(journal())
            if event["payload"]["kind"] == "speech.transcript"
        ]
        assert len(speech_events) == len(updates)
        assert [event["text"] for event in speech_events] == [event["text"] for event in updates]
        assert any(event["final"] for event in speech_events)
        qtbot.waitUntil(
            lambda: all(
                len([event for event in items if event.payload["kind"] == "speech.transcript"])
                >= len(speech_events)
                for items in observed
            ),
            timeout=10000,
        )
        for items in observed:
            speech = [event for event in items if event.payload["kind"] == "speech.transcript"]
            assert [event.payload["text"] for event in speech] == [
                event["text"] for event in speech_events
            ]
            assert any(event.payload["final"] for event in speech)
        if not live:
            command = {
                "kind": "player.state",
                "command_id": str(uuid4()),
                "sent_at": datetime.now(UTC).isoformat(),
                "state": {
                    "expected_state_version": initial.state_version,
                    "active_slide_id": second_slide_id,
                    "focus_view": "slide",
                    "focus_region": None,
                    "autonomy_mode": "manual",
                    "last_accepted_action_id": None,
                },
            }
            body = json.dumps(command).encode()
            for duplicate in (False, True):
                request = Request(
                    endpoint + "/sync/commands",
                    data=body,
                    method="POST",
                    headers={**headers, "Content-Type": "application/json"},
                )
                with urlopen(request, timeout=5) as response:
                    assert json.load(response)["duplicate"] is duplicate
            qtbot.waitUntil(
                lambda: all(
                    any(event.snapshot and event.snapshot.state_version == 1 for event in items)
                    for items in observed
                ),
                timeout=10000,
            )
            for items in observed:
                positions = [
                    event.snapshot
                    for event in items
                    if event.snapshot and event.snapshot.state_version == 1
                ]
                assert len(positions) == 1
                assert positions[0].state.active_slide_id == second_slide_id
            qtbot.waitUntil(
                lambda: all(states.count("connected") >= 2 for states in connections), timeout=12000
            )
            qtbot.wait(600)
            for items in observed:
                positions = [
                    event for event in items if event.snapshot and event.snapshot.state_version == 1
                ]
                assert len(positions) == 1
        control.stop()
        assert not control._active and not control._retry.isActive()
    finally:
        timer.stop()
        for observer in observers:
            observer.stop()
            if observer._thread is not None:
                observer._thread.join(timeout=3)
                assert not observer._thread.is_alive()
        if microphone is not None:
            microphone.shutdown()
        control.stop()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()
    if not live:
        assert provider.closed
