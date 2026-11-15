"""Live context merging, capture boundaries and retention without private logs."""

from buddy_desktop.transcript_context import TranscriptContext


def setup(monkeypatch):
    clock = [1_000_000_000]
    monkeypatch.setattr("buddy_desktop.transcript_context.time.monotonic_ns", lambda: clock[0])
    context = TranscriptContext()
    context.bind("session", 1, "en-US")
    clock[0] += 1_000_000_000
    return context, clock


def event(clock, **changes):
    return {
        "session_id": "session",
        "stream_id": "stream",
        "sequence": 1,
        "segment_id": "a",
        "text": "private speech",
        "final": False,
        "start_seconds": 0.0,
        "end_seconds": 0.1,
        "capture_end_ns": clock[0],
        **changes,
    }


def test_revisions_replace_and_finals_are_immutable(monkeypatch):
    context, clock = setup(monkeypatch)
    context.accept(event(clock))
    context.accept(event(clock, sequence=2, text="corrected", final=True))
    context.accept(event(clock, sequence=3, text="wrong"))
    assert context.snapshot().text == "corrected"
    assert len(context.snapshot().segments) == 1
    assert context.snapshot().fresh


def test_order_duplicates_and_cross_session(monkeypatch):
    context, clock = setup(monkeypatch)
    context.accept(event(clock, sequence=2))
    context.accept(event(clock, sequence=1, text="wrong"))
    context.accept(event(clock, session_id="other", sequence=3, text="wrong"))
    assert context.snapshot().text == "private speech"


def test_manual_change_rejects_delayed_and_straddling_speech(monkeypatch):
    context, clock = setup(monkeypatch)
    old = event(clock)
    context.accept(old)
    clock[0] += 100_000_000
    context.bind("session", 2, "en-US")
    context.accept(old)
    assert not context.snapshot().text
    clock[0] += 50_000_000
    context.accept(event(clock, sequence=2))
    assert not context.snapshot().text
    clock[0] += 1_000_000_000
    context.accept(event(clock, sequence=3, segment_id="new"))
    assert context.snapshot().text == "private speech"


def test_silence_expires_without_new_events(monkeypatch):
    context, clock = setup(monkeypatch)
    context.accept(event(clock))
    clock[0] += 6_000_000_000
    assert context.snapshot().text and not context.snapshot().fresh
    clock[0] += 30_000_000_000
    assert not context.snapshot().text


def test_pause_preserves_references_without_new_decisions(monkeypatch):
    context, clock = setup(monkeypatch)
    context.accept(event(clock, final=True, text="The provider sends payment confirmations."))
    before = context.snapshot()
    clock[0] += 7_000_000_000
    assert not context.snapshot().fresh
    assert context.snapshot().text == before.text
    context.accept(
        event(
            clock,
            sequence=2,
            segment_id="b",
            start_seconds=7,
            end_seconds=7.1,
            final=True,
            text="Its signature must be verified.",
        )
    )
    after = context.snapshot()
    assert after.epoch == before.epoch and after.fresh
    assert after.text == before.text + " Its signature must be verified."
    assert len(after.candidate_segments) == 2


def test_long_pause_still_obeys_memory_retention(monkeypatch):
    context, clock = setup(monkeypatch)
    context.accept(event(clock, final=True))
    epoch = context.snapshot().epoch
    clock[0] += 31_000_000_000
    assert not context.snapshot().text
    context.accept(event(clock, sequence=2, segment_id="new", final=True, text="Fresh topic."))
    assert context.snapshot().text == "Fresh topic."
    assert context.snapshot().epoch == epoch


def test_candidate_passage_keeps_only_recent_contiguous_completed_segments(monkeypatch):
    context, clock = setup(monkeypatch)
    for n in range(6):
        clock[0] += 200_000_000
        context.accept(
            event(
                clock,
                segment_id=str(n),
                sequence=n + 1,
                start_seconds=n * 0.2,
                end_seconds=n * 0.2 + 0.1,
                final=True,
                text=f"Sentence {n}.",
            )
        )
    assert [s.text for s in context.snapshot().candidate_segments] == [
        f"Sentence {n}." for n in range(6)
    ]
    clock[0] += 200_000_000
    context.accept(
        event(clock, segment_id="partial", sequence=7, start_seconds=1.2, end_seconds=1.3)
    )
    assert not context.snapshot().candidate_segments
    clock[0] += 200_000_000
    context.accept(
        event(
            clock,
            segment_id="after",
            sequence=8,
            start_seconds=1.4,
            end_seconds=1.5,
            final=True,
            text="Completed after partial.",
        )
    )
    assert [s.text for s in context.snapshot().candidate_segments] == ["Completed after partial."]


def test_candidate_passage_drops_whole_old_segments_at_character_bound(monkeypatch):
    context, clock = setup(monkeypatch)
    for n in range(4):
        clock[0] += 200_000_000
        context.accept(event(clock, sequence=n + 1, segment_id=str(n), final=True, text="x" * 1500))
    selected = context.snapshot().candidate_segments
    assert [s.segment_id for s in selected] == ["2", "3"]
    assert len(" ".join(s.text for s in selected)) <= 4096
    assert all(len(s.text) == 1500 for s in selected)


def test_reconnect_clear_and_privacy(monkeypatch):
    context, clock = setup(monkeypatch)
    context.accept(event(clock))
    assert "private speech" not in repr(context.snapshot())
    assert "private speech" not in repr(context.snapshot().segments[0])
    context.invalidate()
    context.accept(event(clock))
    assert not context.snapshot().text


def test_size_and_segment_bounds(monkeypatch):
    context, clock = setup(monkeypatch)
    for index in range(100):
        context.accept(
            event(
                clock,
                sequence=index + 1,
                segment_id=str(index),
                start_seconds=index / 1000,
                end_seconds=0.1,
                text="x" * 200,
            )
        )
    snapshot = context.snapshot()
    assert len(snapshot.segments) <= 64
    assert sum(len(s.text) for s in snapshot.segments) <= 8192


def test_presenter_consumes_live_results_and_clears_on_sync_gap(qtbot, tmp_path, monkeypatch):
    from dataclasses import replace

    from PySide6.QtCore import QSettings
    from test_presentation_stage import _session

    from buddy_desktop.config import AppConfig
    from buddy_desktop.present_widget import PresentWidget
    from buddy_desktop.presentation_session_service import SessionState
    from buddy_desktop.session_events import SessionEventStream

    monkeypatch.setattr(SessionEventStream, "bind", lambda *args: None)
    clock = [1_000_000_000]
    monkeypatch.setattr("buddy_desktop.transcript_context.time.monotonic_ns", lambda: clock[0])
    widget = PresentWidget(
        AppConfig("http://127.0.0.1:8000"),
        QSettings(str(tmp_path / "context.ini"), QSettings.Format.IniFormat),
    )
    qtbot.addWidget(widget)
    try:
        widget._session = replace(_session("deck", SessionState("slide")), id="session")
        widget._refresh()
        widget._context_connection_changed(widget.session_events.generation, "connected")
        clock[0] += 1_000_000_000
        widget.speech.transcript_received.emit(event(clock, latency_ms=10))
        assert widget.transcript_context.snapshot().text == "private speech"
        widget.session_events.connection_changed.emit(
            widget.session_events.generation, "reconnecting"
        )
        assert not widget.transcript_context.snapshot().text
        widget._context_connection_changed(widget.session_events.generation, "connected")
        clock[0] += 1_000_000_000
        widget.speech.transcript_received.emit(event(clock, sequence=2, latency_ms=10))
        assert widget.transcript_context.snapshot().text
        widget.speech.cleared.emit()
        assert not widget.transcript_context.snapshot().text
        clock[0] += 1_000_000_000
        widget.speech.transcript_received.emit(event(clock, sequence=3, latency_ms=10))
        assert widget.transcript_context.snapshot().text
        widget._session_stream_failed(
            widget.session_events.generation, "Session updates stopped.", False
        )
        assert not widget._context_connected
        assert not widget.transcript_context.snapshot().text
        # Restarted speech cannot repopulate decision context without live sync.
        clock[0] += 1_000_000_000
        widget.speech.transcript_received.emit(event(clock, sequence=4, latency_ms=10))
        assert not widget.transcript_context.snapshot().text
        widget._context_connection_changed(widget.session_events.generation, "connected")
        clock[0] += 1_000_000_000
        widget.speech.transcript_received.emit(event(clock, sequence=5, latency_ms=10))
        assert widget.transcript_context.snapshot().text
    finally:
        widget.shutdown()


def test_actual_speech_parser_feeds_context(qapp, monkeypatch):
    import json
    from uuid import uuid4

    from buddy_desktop.speech import SpeechController

    context, clock = setup(monkeypatch)
    session, stream = str(uuid4()), str(uuid4())
    context.bind(session, 1, "en-US")
    control = SpeechController()
    control._active = True
    control._session = session
    control.transcript_received.connect(context.accept)
    control.cleared.connect(context.invalidate)
    try:
        control._message(json.dumps({"type": "ready", "version": 1, "stream_id": stream}))
        clock[0] += 1_000_000_000
        payload = event(
            clock,
            session_id=session,
            stream_id=stream,
            segment_id=stream + ":0",
            type="transcript",
            version=1,
        )
        control._message(json.dumps(payload))
        assert context.snapshot().text == "private speech"
        control.stop()
        assert not context.snapshot().text
    finally:
        control.stop()


def test_language_or_account_change_clears_context(monkeypatch):
    context, clock = setup(monkeypatch)
    context.accept(event(clock))
    epoch = context.snapshot().epoch
    context.bind("session", 1, "ar")
    assert context.snapshot().epoch > epoch
    assert not context.snapshot().text
    context.bind("other-session", 1, "en-US")
    clock[0] += 1_000_000_000
    context.accept(event(clock))
    assert not context.snapshot().text


def test_long_utterance_does_not_bypass_time_bound(monkeypatch):
    context, clock = setup(monkeypatch)
    clock[0] += 40_000_000_000
    context.accept(event(clock, start_seconds=0, end_seconds=35))
    assert not context.snapshot().text


def test_accepted_long_utterance_expires_from_capture_start(monkeypatch):
    context, clock = setup(monkeypatch)
    clock[0] += 29_000_000_000
    context.accept(event(clock, start_seconds=0, end_seconds=29, final=True))
    assert context.snapshot().text
    clock[0] += 2_000_000_000
    snapshot = context.snapshot()
    assert not snapshot.text
    assert not snapshot.fresh
    assert not context._segments


def test_natural_correction_and_topic_change_invalidate_pending_results(monkeypatch):
    context, clock = setup(monkeypatch)
    phrases = [
        "We considered storing passwords directly.",
        "But we rejected that; we store only salted password hashes.",
        "An audience member asks how refunds work.",
        "Returning to authentication, a leaked database must not expose passwords.",
        "Now let's discuss privacy: combining location traces can identify a person.",
    ]
    pending = None
    for n, phrase in enumerate(phrases):
        clock[0] += 1_000_000_000
        context.accept(
            event(
                clock,
                sequence=n + 1,
                segment_id=str(n),
                start_seconds=n,
                end_seconds=n + 0.1,
                text=phrase,
                final=True,
            )
        )
        latest = context.snapshot()
        if pending:
            assert not pending.matches(latest)
        assert latest.matches(context.snapshot())
        assert latest.candidate_segments[-1].text == phrase
        pending = latest
    # More than four provider chunks still form the actual bounded passage.
    assert len(context.snapshot().candidate_segments) == 5
    clock[0] += 6_000_000_000
    assert not pending.matches(context.snapshot())


def test_different_recognition_chunk_sizes_preserve_same_passage(monkeypatch):
    words = [
        "The",
        "provider",
        "confirms",
        "payments",
        "and",
        "we",
        "check",
        "its",
        "signature",
        "before",
        "accepting",
        "the",
        "message",
    ]
    context, clock = setup(monkeypatch)
    for n, word in enumerate(words):
        clock[0] += 100_000_000
        context.accept(
            event(
                clock,
                sequence=n + 1,
                segment_id=str(n),
                start_seconds=n * 0.1,
                end_seconds=(n + 1) * 0.1,
                text=word,
                final=True,
            )
        )
    assert " ".join(s.text for s in context.snapshot().candidate_segments) == " ".join(words)


def test_snapshot_match_checks_all_bindings_and_expiration(monkeypatch):
    from dataclasses import replace

    context, clock = setup(monkeypatch)
    context.accept(event(clock, final=True))
    snapshot = context.snapshot()
    assert snapshot.matches(replace(snapshot, observed_ns=snapshot.observed_ns + 1))
    for changes in (
        {"session_id": "other"},
        {"state_version": 2},
        {"epoch": snapshot.epoch + 1},
        {"stream_id": "other"},
        {"language": "ar"},
        {"fresh": False},
        {"segments": ()},
    ):
        assert not snapshot.matches(replace(snapshot, **changes))
