"""Synthetic speech through the real session WebSocket and Deepgram EU.

Uses Windows' installed SAPI voice, not a microphone or a human-voice benchmark.
Only the identity-provider boundary is substituted by the caller; ownership,
audio framing, streaming, transcript normalization and persistence remain real.
"""

import queue
import struct
import threading
import time
import wave
from pathlib import Path


def synthesize(text, destination):
    import win32com.client

    voice = win32com.client.Dispatch("SAPI.SpVoice")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    audio_format = win32com.client.Dispatch("SAPI.SpAudioFormat")
    audio_format.Type = 18  # SAFT16kHz16BitMono
    stream.Format = audio_format
    stream.Open(str(Path(destination).resolve()), 3, False)
    try:
        voice.AudioOutputStream = stream
        voice.Rate = 0
        voice.Speak(text)
    finally:
        stream.Close()
    with wave.open(str(destination), "rb") as source:
        if (source.getframerate(), source.getsampwidth(), source.getnchannels()) != (16000, 2, 1):
            raise ValueError("SAPI did not produce 16 kHz mono PCM")
        return source.readframes(source.getnframes())


def replay(client, session, case, folder):
    folder = Path(folder)
    folder.mkdir(exist_ok=True)
    pcm = synthesize(" ".join([*case["earlier"], case["text"]]), folder / (case["id"] + ".wav"))
    events = queue.Queue()
    finals = []
    with client.websocket_connect(f"/api/v1/sessions/{session['id']}/speech") as socket:
        socket.send_json(dict(version=1, language="en-US", sample_rate=16000))
        ready = socket.receive_json()
        if ready.get("type") != "ready":
            raise RuntimeError(
                "Speech provider did not become ready: " + ready.get("code", "unknown")
            )

        def receive():
            try:
                while True:
                    event = socket.receive_json()
                    events.put((time.monotonic_ns(), event))
            except Exception:
                pass  # Expected socket close after bounded playback.

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        samples = pcm + bytes(32000)  # One second of real-time silence for endpointing.
        started = time.monotonic()
        for offset in range(0, len(samples), 3200):
            deadline = started + offset / 32000
            time.sleep(max(0, deadline - time.monotonic()))
            socket.send_bytes(
                struct.pack(">Q", time.monotonic_ns()) + samples[offset : offset + 3200]
            )
        finished = time.monotonic() + 2
        while time.monotonic() < finished:
            try:
                stamp, event = events.get(timeout=max(0.001, finished - time.monotonic()))
            except queue.Empty:
                break
            if event.get("type") == "error":
                raise RuntimeError("Speech stream failed: " + event["code"])
            if event.get("type") == "transcript" and event.get("final"):
                finals.append((stamp, event))
    reader.join(timeout=2)
    if not finals:
        raise RuntimeError("Deepgram produced no final transcript")
    # Preserve final segment order; both selectors receive these same recognized words.
    recognized = " ".join(event["text"] for _, event in finals)
    final_stamp, final_event = finals[-1]
    return dict(
        text=recognized,
        final_segments=[event["text"] for _, event in finals],
        audio_seconds=round(len(pcm) / 32000, 3),
        recognition_final_ms=round((final_stamp - final_event["capture_end_ns"]) / 1e6, 2),
        scope="SAPI audio through session WebSocket/Deepgram EU; no physical microphone",
    )
