# Rolling speech context (#18)

The presenting desktop owns an ephemeral decision-input window, exposed as
`PresentWidget.transcript_context.snapshot()`. It consumes the actual validated
`SpeechController` partial/final results for the authenticated saved session.
It does not build decision context from journal replay: those notifications have
publication timestamps, which cannot prove which slide was visible at capture.

A frozen snapshot includes session ID, authoritative state version, a local
invalidation epoch, stream ID, language, ordered segments and local monotonic
observation time. Segments include audio offsets, capture-end timestamps and
revision sequence, plus a local monotonic arrival timestamp for request settling.
`text` joins segments once; partial corrections replace the
same segment and a final segment cannot be overwritten. Duplicate and older
updates are ignored. SpeechController enforces wire validation and current-stream
identity before delivery. Consumers must verify session/version/epoch are still
current and require `fresh` immediately before using a snapshot; holding a snapshot
does not extend its lifetime or authorize an action. #19 and #20 own resolving
candidates and making decisions, respectively.

The window retains at most 30 seconds of capture, 64 segments and 8192 characters
including separators. After five seconds without recent captured speech the
snapshot is not fresh. Snapshot reads expire text; a one-second Qt timer also
expires stored text during silence. Time is measured against the microphone's
local monotonic capture clock, never a server wall clock. Hardware capture delay
has the same limitations as the existing recognition latency measurement.

When the next accepted segment starts more than five seconds after the last captured
segment, the old thought is cleared and the epoch advances. The reset boundary uses the
capture clock, allowing delayed delivery of a genuinely new utterance while rejecting
older speech. Out-of-order or finalized revisions cannot reset the thought. The last
capture timestamp is retained even if the 30-second text window has already expired.

`candidate_segments` selects up to four consecutive final segments, capped at 4096
characters. It stops at a partial or the size boundary rather than joining text across a
hole or splitting a segment. `CandidateService` waits for 200 ms of stability after their
most recent arrival before allowing a request. This is an off-thread caller scheduling
contract for #20; this window itself does not start provider requests.

Manual navigation/focus changes invalidate immediately before the server write.
Remote state/version changes, identity/session/deck/language changes, speech
stop/pause/mute/reconnect and session transport connection transitions clear the
window. Context is not accepted while sync is disconnected, local state is pending,
or saved position is being restored. A delayed segment whose estimated capture
start precedes the boundary is discarded, including partial/final revisions that
straddle that boundary. New speech can populate the window once state is stable.
This deliberately sacrifices an ambiguous utterance rather than attach it to a
new slide. These rules do not reset the speech provider or consume retry attempts.

No extra database retention, audio recording, operational logging or provider
request is introduced. The existing five-minute session journal and backup policy
remain as documented in [session events](session-events.md). The window is local
memory only and never restored after restart. Snapshot and segment reprs omit text.
The recognition UI continues to show the latest result; this window supplies later
decision consumers and does not itself change slides.

Run `python -m pytest frontend/tests/test_transcript_context.py
frontend/tests/test_speech.py frontend/tests/test_presentation_stage.py
frontend/tests/test_session_events.py` for merging, bounds, freshness, invalidation,
wire validation and Qt integration. Tests use generated transcript events and local
transport fixtures; they do not require a paid speech provider or record a person.
