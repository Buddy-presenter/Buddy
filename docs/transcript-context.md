# Rolling speech context (#18)

The presenting desktop owns an ephemeral decision-input window, exposed as
`PresentWidget.transcript_context.snapshot()`. It consumes the actual validated
`SpeechController` partial/final results for the authenticated saved session.
It does not build decision context from journal replay: those notifications have
publication timestamps, which cannot prove which slide was visible at capture.

A frozen snapshot includes session ID, authoritative state version, a local
invalidation epoch, stream ID, language, ordered segments and local monotonic
observation time. Segments include audio offsets, capture-end timestamps, revision
sequence, and a stable whole-token prefix for the active interim segment.
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

Silence does not advance the epoch or clear a thought. After five seconds without
fresh captured speech, no new candidate request is allowed; resumed speech can use the
preceding context still inside the 30-second capture window. A six- or twenty-second
pause therefore preserves references, while an older context still expires under the
existing memory-retention policy. Pauses are not a topic classifier.

`candidate_segments` remains final-only for candidate retrieval. `decision_segments`
allows the agent to use consecutive final evidence plus only whole tokens shared by two
consecutive hypotheses of the active interim segment. It never sends the changing tail.
The desktop requests a provisional decision after 12 stable characters, then allows at
most two follow-ups per segment when another whole word becomes stable. If a request is
still running, only the newest materially changed stable prefix is retained and checked
when that request finishes. A completed segment remains eligible immediately; there is
no extra settling timer. Provider segmentation is not treated as sentence structure.
`ContextSnapshot.matches(current)` remains an exact snapshot check. The agent-specific
`matches_decision(current)` also accepts ordinary continuation if the submitted stable
prefix stays intact, while rejecting a revision that changes that prefix, an explicit
correction/negation, or freshness/session/state/epoch/stream/language changes. Ordinary
completed segments can continue the thought without canceling the pending action. The accepted result is
still bounded by the five-second capture freshness and authoritative slide/session checks.

`CandidateService.resolve(..., current_context=...)` can check a caller-supplied current
snapshot before and after the network request. Callers must supply a thread-safe source
of newly published immutable snapshots and recheck `matches` again at result consumption.
Without this callback, that input-revision check remains the caller's responsibility.
#20 owns worker scheduling, keeping only the newest pending input while a request is
running, honoring provider pacing, and final decision/execution policy. The presentation
agent may send the bounded stable interim prefix only when Live suggestions are enabled;
the changing partial tail is never sent. This does not change speech retention, audio
handling, or the manual-control opt-in.

Manual navigation/focus changes invalidate immediately before the server write.
Remote state/version changes, identity/session/deck/language changes, speech
stop/pause/mute/reconnect and session transport connection transitions clear the
window. Context is not accepted while sync is disconnected, local state is pending,
or saved position is being restored. A delayed segment whose estimated capture
start precedes the boundary is discarded, including partial/final revisions that
straddle that boundary. New speech can populate the window once state is stable.
This deliberately sacrifices an ambiguous utterance rather than attach it to a
new slide. These rules do not reset the speech provider or consume retry attempts.

No extra database retention, audio recording or provider request is introduced. Local
desktop diagnostics record only bounded reason codes, timing values, action tool names,
and opaque decision IDs; they never record transcript text, audio, credentials, tokens,
or provider response bodies. On Windows the rotating log is
`%LOCALAPPDATA%\Buddy\logs\desktop.log` (1 MB per file, two backups). The existing
five-minute session journal and backup policy remain as documented in
[session events](session-events.md). The window is local memory only and never restored
after restart. Snapshot and segment reprs omit text.
The recognition UI continues to show the latest result; this window supplies later
decision consumers and does not itself change slides.

Run `python -m pytest frontend/tests/test_transcript_context.py
frontend/tests/test_speech.py frontend/tests/test_presentation_stage.py
frontend/tests/test_session_events.py` for merging, bounds, freshness, invalidation,
wire validation and Qt integration. Tests use generated transcript events and local
transport fixtures; they do not require a paid speech provider or record a person.
