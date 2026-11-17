# Voice-to-slide acceptance (#146)

This is the runbook for a signed-in human speaker using a real microphone, deployed
speech recognition and the configured intent provider. It does not establish acceptance
by itself. Run each case independently with an analyzed PDF and a supported saved
PowerPoint deck. Keep #146 open until both formats have real evidence for the matrix.

## Prepare the session

1. Record the desktop commit, deployed API commit, Windows version, microphone/device
   mode, language, and network conditions. For PowerPoint, record the installed desktop
   Office version/build. PowerPoint on macOS or without Office is unsupported, not passed.
2. Use a small, non-private deck with at least three distinct slide topics. Upload and
   analyze the original PDF and PPTX independently. Verify their saved source identities;
   exporting PowerPoint to PDF does not test the native PowerPoint path.
3. Sign in. For PDF, choose **Present PDF** from Library. For PowerPoint, choose
   **Open saved PowerPoint** in Review, then **Present with Buddy**. Wait for the saved
   session to connect and verify that the displayed slide matches the local player.
4. Confirm deployed speech and agent configuration with the operator without copying
   secrets. The current source uses Deepgram EU `nova-3` with `mip_opt_out=true` and
   Cohere `command-a-03-2025`. Record the actual deployed configuration, including the
   embeddings model, enabled features, and provider quotas; source defaults alone do
   not prove the deployed configuration. See [speech setup](speech-recognition.md) and
   [agent setup](presentation-agent.md).
5. Have the speaker explicitly start the microphone and enable **Live suggestions**.
   Wait for Ready, then enable **Automatic transitions**. Keep unrelated conversations
   out of range. Do not start microphone capture unattended. Stop capture after each run.

Exact next/previous/numbered commands can use deterministic navigation without a model
call. Include contextual topics to exercise the intent model as well. Report each
decision's actual source; deterministic navigation is not model-accuracy evidence.

## Execute both format columns

Record one outcome per case per format: `passed`, `failed`, `unsupported`, or `not-run`,
with an evidence reference and a reason. Start each case from a known visible slide.
Use a fresh utterance after a reset, manual override, reconnect, or retry.

| Case | Spoken input or operation | Required observation in PDF and native PowerPoint |
| --- | --- | --- |
| Direct forward/back | Say "Next slide", then "Previous slide". | Transcript, typed navigation target and actual visible slide agree; exactly one move per command. |
| Numbered/named destination | Say "Go to slide three", then request a unique slide title. | The number/title resolves to the expected saved slide; the actual player reaches it. Record whether navigation or the model selected it. |
| Contextual topic | Discuss a distinct topic on another slide without a direct command. | The model can choose only an allowed target; record intended and actual slide separately. |
| Ambiguity/uncertainty | Use a topic shared by multiple slides, an unrelated topic and an incomplete reference. | Ambiguous or low-confidence decisions abstain with an understandable status. |
| Negation/unsupported command | Say "Do not go to the next slide", then request an unavailable operation. | No unintended navigation or region action. |
| Deck boundaries | Request previous at the first slide, next at the last slide, and an out-of-range number. | No wraparound or fallback move to an unrelated slide. |
| Stale/corrected input | Change slides manually while a topic decision is pending; correct the spoken topic. | The superseded result cannot move the new view. Repeat deterministic stale/out-of-order tests separately; do not claim a natural utterance proves every ordering case. |
| Pause/resume movement | Use **Pause automatic moves**, speak a command, then resume and speak a fresh command. | Transcription can continue while movement is paused. Old paused speech does not replay; fresh speech can move after resume. |
| Mute/pause/stop capture | Exercise microphone Mute, Pause/resume and Stop. | Capture/transcription stop as indicated; late results cannot move the view. Movement pause and capture pause remain distinct. |
| Manual override/undo | Interrupt a pending automatic move manually; undo a completed automatic move. | Manual input wins. Undo restores the preceding view and pauses automatic movement. |
| Permission/device failure | Deny microphone permission where supported; remove the selected device. | No capture starts or remains active; show a usable recovery status. Record unsupported OS permission scenarios honestly. |
| Provider/network recovery | Have the operator induce a test-only provider failure and interrupt the test connection, then restore it. | No actions during the gap; bounded recovery and a fresh context on reconnect. Do not change production provider settings to simulate failure. |
| Session/privacy cleanup | End the session, leave Present, sign out, and disconnect the native slideshow in separate trials. | Capture and pending actions stop; transient transcript/context clears; no later action appears. |
| Region exclusion | Request highlight/zoom without independent semantic visual verification. | Unverified regions remain outside the live candidate set. Execution validation alone cannot make them available; #135 and its prerequisites remain separate. |

Repeat positive commands and abstention cases rather than relying on one success. A
missing transcript, wrong target, duplicate move, stale action or incorrect abstention
is a failed trial. Keep failures in the denominator and report them, including retries.

## Evidence and measurement

Keep raw audio, transcripts and deck content private. No recording is required for this
runbook. Use a synthetic/non-private deck and a case ID to connect the speaker's intended
result, visible transcript, typed target, decision source, actual player state and outcome.
Do not enable provider frame/debug logs. Ordinary Buddy diagnostics intentionally omit
speech and slide text; they cannot by themselves establish recognition accuracy.

For each format, record the total attempted and correct direct commands, topic trials,
expected abstentions, wrong moves, duplicate moves, and provider/reconnect failures.
Report recognition accuracy separately from correct action selection. Capture language,
sample count and test conditions beside every accuracy or latency result.

Use [presentation latency diagnostics](presentation-latency.md) to join safe desktop and
backend events by decision ID. Keep speech-result latency, proposal latency and observed
action-completion latency distinct. Report sample count, p50 and p95 separately for PDF
and PowerPoint. Do not treat Office animation timing as zero when it is unobserved.
Sanitize exported evidence and record which commit and case IDs it covers.

Link the exact [#25 replay results](presentation-replay.md), corpus/gate hashes and
scope beside the live report. Scripted/generated-input or mocked-provider results are
supporting code checks, not real-microphone or deployed-provider acceptance. A waiver of
human recordings in [evaluation preparation](evaluation-recordings.md) does not turn
scripted speech into the live evidence required by #146.

## Automated readiness

Run the existing deterministic boundary and player tests before a live session:

```powershell
python -m pytest frontend/tests/test_speech.py frontend/tests/test_microphone.py frontend/tests/test_transcript_context.py frontend/tests/test_agent.py frontend/tests/test_transition_policy.py frontend/tests/test_transition_player.py frontend/tests/test_session_events.py -q
python -m pytest backend/tests/api/test_presentation_candidates.py backend/tests/api/test_presentation_agent.py -q
```

Persisted API/integration tests require the disposable PostgreSQL setup described in
[development](development.md). The opt-in provider/microphone tests in
[speech recognition](speech-recognition.md) are separate from deterministic tests and
require deliberate speaker participation. Do not run them unattended.

On 2026-11-17 at `efb9695`, the desktop readiness command above passed 163 tests; its
real-microphone test was skipped because it requires opt-in participation. These results
do not measure recognition accuracy, deployed providers, or real voice-to-slide acceptance.
The backend command passed 101 tests; 25 persisted database tests were skipped because
no disposable PostgreSQL URL was configured locally. Current-head CI must run those
database contracts before this work is reviewed.

The generated-deck smoke also ran through the actual PDF player and native Windows
PowerPoint 16.0.20527 after its synthetic proposal was updated to include the current
`active_slide_id`. Automatic navigation now asserts local completion without a recovery
position write; undo still restores the preceding view and writes recovery state.
Both formats suppressed the repeated proposal. The single local samples measured PDF
navigation/undo at 15/46 ms and PowerPoint at 387/1254 ms. These are not p95 estimates,
speech latency, provider/model accuracy, or observed Office animation duration.
See the [sanitized aggregate](verification/voice-readiness-2026-11-17.json).
That aggregate also includes separate #25 synthetic development/holdout replay reports
with their exact corpus and gate hashes. Both frozen suites passed (21 frames each,
no outside-candidate targets or false transitions); `synthetic_contract` provenance,
`agent_proposals` scope and `release_gate: false` remain explicit. Their recorded timing
and synthetic cost units do not measure current live latency or provider billing.

To repeat that supporting check without recording or sending microphone audio:

```powershell
python frontend/scripts/smoke_transition_policy.py --output replay-artifacts/voice-readiness-new
```

Use a new output directory. Its speech input, eligibility HTTP and session transport
are explicit local substitutes; do not enter this smoke as a pass in the live matrix.
