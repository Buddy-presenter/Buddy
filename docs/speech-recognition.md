# Streaming speech recognition (#16)

Saved PDF presentation sessions can stream microphone audio through the authenticated
Buddy API to Deepgram Nova-3. Present displays the latest partial or final transcript,
its audio-relative time range, and measured recognition delay. Recognition does not
execute slide commands; rolling transcript context and presentation decisions remain
in #18–#21, and general session event synchronization remains in #33.

## Presenter behavior

Sign in, open a ready saved PDF from Library, wait for its presentation session to
connect, choose the microphone and recognition language in Present, and select
**Start microphone**. The privacy notice identifies Deepgram before capture starts.
English US/UK, Arabic, French, German, and Spanish are offered; actual dialect accuracy
must be evaluated with the intended speakers. A language change stops capture and
requires an explicit restart. Unsupported languages/configurations fail explicitly.

Mute, pause, stop, leaving Present, changing decks, signing out, session end, device loss,
and application shutdown close recognition and discard queued audio and displayed text.
Unmuting/resuming starts a fresh stream. Backend ownership/lifecycle/revocation checks
run before a provider connection and every five seconds afterward, with a ten-second
verification timeout. A remote lifecycle or access change can therefore take up to
15 seconds to terminate an existing stream; local stops terminate transmission immediately.

Transient connection failures, timeouts, provider rate limits, buffer overflow, and slow
connections allow at most three reconnect attempts, after 1, 2, and 4 seconds. Each
reconnect has a new stream ID and audio-relative clock. Audio produced during connection
gaps is discarded, never replayed as current speech. Status explains the gap. Permanent
configuration/access errors require stopping and restarting the microphone after repair.
Manual presentation remains available throughout.

Local-only PDF viewing and Windows PowerPoint do not have the authenticated saved PDF
session required by this endpoint. PowerPoint microphone capture stays local and its
status explicitly says transcription is unavailable. No backend session is fabricated
for a local PowerPoint slideshow.

## Backend configuration and deployment

Install the backend API dependencies, including pinned `websockets==16.0`. Set
`BUDDY_DEEPGRAM_API_KEY` on the **backend process only**. Without it, the endpoint reports
`speech_unconfigured` and never opens a provider connection. No key belongs in desktop
configuration, source control, shell arguments, transcripts, or logs. Configuration reprs
omit the key.

For the encrypted Windows key used during development, load it into only the API shell:

```powershell
$speechKey = Import-Clixml -LiteralPath "$env:LOCALAPPDATA\buddy-deepgram-key.xml"
$env:BUDDY_DEEPGRAM_API_KEY = [System.Net.NetworkCredential]::new('', $speechKey).Password
# Configure the normal local identity and PostgreSQL settings before starting the API.
.\.venv\Scripts\python.exe -m buddy_api
# Remove the process variable after the API stops.
Remove-Item Env:BUDDY_DEEPGRAM_API_KEY
```

The infrastructure input `enable_speech` defaults to false in staging and production.
When enabled, Terraform creates the `${environment-name}/speech/deepgram` secret
(for example `buddy-staging/speech/deepgram`) using the existing KMS key. An operator
must populate its **plain string value** through a secure Secrets Manager workflow
before deploying API tasks that reference it. Terraform never receives or stores the
secret value. The API execution role can read that exact secret and decrypt through
Secrets Manager. Only the API container gets `BUDDY_DEEPGRAM_API_KEY`; workers and
migration containers do not. Existing release gates and task promotion remain unchanged.
Key rotation requires deploying/restarting API tasks to load the new value.

Speech stream failures are written to the API log at warning level so they remain
visible with the deployed API's warning-level runtime configuration. The bounded
`speech_stream` event records a random `diagnostic_id`, failure outcome, processing
stage, duration, upstream HTTP status when supplied by Deepgram, and exception class
for unexpected failures. The same opaque ID is returned with an error so an operator
can find the exact event. It never records exception messages, provider response bodies,
credentials, bearer tokens, audio, or transcript text. A normal client close is not
logged as a warning. For staging diagnosis, inspect `/buddy/staging/api` in CloudWatch
Logs and filter for `speech_stream`; match the UI's reference ID, then use `outcome`,
`stage`, `provider_status`, and `exception_type` to choose the relevant server-side
investigation. The desktop distinguishes provider unavailability, unexpected API
failures, and lost speech connections in its status text.

Deployment must support authenticated WebSocket upgrades over the existing HTTPS
endpoint, including Authorization forwarding. CloudFront forwards all viewer headers in
the current configuration. Verify the actual deployed route after release; a localhost
test does not establish ingress readiness. Do not expose the private origin or bypass
WAF/authentication to make streaming work.

## Versioned speech contract

`/api/v1/sessions/{session_uuid}/speech` accepts a native WebSocket with an Authorization
Bearer header. URL tokens, query parameters, browser Origins, and foreign Host headers
are rejected. Authentication reuses the existing live Cognito verifier and account-owned,
nondeleted session lookup. The session must be active. Cookies do not authenticate.

The first text message, within five seconds after authentication, is:

```json
{"version":1,"language":"en-US","sample_rate":48000}
```

An error response contains `type: error`, a bounded `code`, `retryable`, and a random
UUID `diagnostic_id`. The desktop may display that ID as a support reference; clients
must not treat it as authentication or session state.

The schema rejects extra fields; the sample rate must be an integer from 8000 to 96000.
The backend selects the provider, model, EU endpoint, encoding, and privacy settings.
Clients cannot override them. A `ready` message returns `version: 1` and a fresh UUID
`stream_id`. Audio sent before ready is discarded by the desktop.

Each subsequent binary message is an unsigned big-endian 64-bit desktop monotonic
capture-read timestamp in nanoseconds followed by signed little-endian mono PCM16.
The native capture worker downmixes UInt8, Int16, Int32, or Float samples to PCM16 at the
device's original sample rate; there is no inaccurate sample-rate relabeling. Reads split
across sample/channel boundaries are joined before conversion. There is no audio file.

The server strips the timestamp before sending audio to the provider. Timestamps must
increase strictly. Frames are at most 8200 bytes, including the eight-byte prefix, and
audio must contain complete samples. Stream input is bounded to 1.25x real time plus a
one-second burst. No audio for ten seconds ends the stream. Each stream lasts at most
30 minutes; the desktop can reconnect within its attempt budget. Provider connection
and network operations have bounded deadlines, queues, and WebSocket ping timeouts.

Provider-independent transcript messages contain `type: transcript`, `version: 1`,
`session_id`, `stream_id`, an increasing `sequence`, a stable `segment_id`, `final`,
`text`, `start_seconds`, `end_seconds`, and `capture_end_ns`. Partial revisions for the
same segment share a segment ID; a final segment is immutable to the desktop. Provider
text is limited to 4096 characters. Malformed, nonfinite, cross-session, duplicate,
out-of-order, future, or stale results cause safe failure without presentation actions.
Adjacent segment boundaries tolerate provider float32 rounding (one microsecond or
one float32 relative precision step, whichever is larger); finalized segment starts
must still strictly increase, including for zero-duration finals.
The JSON Schema is [speech-transcript-v1.schema.json](speech-transcript-v1.schema.json).

The capture timestamp corresponding to the segment end is echoed, not compared against
the server clock. The desktop computes receipt minus capture-read time, avoiding clock
skew between machines. This includes capture queueing, network/provider processing, and
delivery for that audio boundary; it excludes unknown microphone hardware/driver delay.
It is not a guarantee about end-of-utterance detection or future slide-action latency.
Results more than 30 seconds old are rejected. The timeline retains at most 2048 points,
the capture queue eight packets, and the Qt network write backlog about 32 KiB before
recovery. Backpressure never creates an unbounded audio archive.

Only one stream per owner and at most 16 streams are admitted **per API process**.
With N processes/replicas the aggregate cap can be 16N and an owner can reach N streams.
These are not global distributed quotas; provision provider concurrency/spend limits
accordingly. Cross-replica transport/ordering coordination remains in #33.

## Privacy

The fixed destination is `wss://api.eu.deepgram.com/v1/listen`, model `nova-3`, with
`mip_opt_out=true` on every connection. Deepgram documents that this opts requests out
of model improvement and gives zero retention of audio/transcript content after the
response; request metadata and usage logs remain. EU routing promises EU processing,
not a fixed country. Consult [Deepgram's data policy](https://developers.deepgram.com/trust-security/your-data)
and the account's actual terms before deployment. Opt-out can affect pricing; do not
assume advertised base prices apply unchanged.

Buddy does not persist audio or transcripts in session records, files, or ordinary logs.
Only the current transcript is displayed, and cleanup clears it. Session control state
remains persisted through #17. Operational speech logs contain only bounded outcome
codes and stream duration, never provider error bodies, text, audio, or credentials.
Do not enable third-party WebSocket frame/debug logging with private speech.

## Verification

Default CI substitutes only external service boundaries and never calls a paid provider:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/api/test_speech.py frontend/tests/test_speech.py frontend/tests/test_microphone.py
# Set BUDDY_TEST_DATABASE_URL to a disposable database ending _test for persisted checks.
.\.venv\Scripts\python.exe -m pytest tests/integration/test_speech_stream.py
```

An explicitly opted-in integration test also runs the production Qt WebSocket client,
HTTP/API authentication path, real PostgreSQL sessions, and real Deepgram adapter. Its
identity-provider response and deck data are fixtures; it is not deployed staging evidence.
Set `BUDDY_LIVE_SPEECH_TEST=1`, the backend-only key, the disposable database URL, and
`BUDDY_SPEECH_TEST_WAV` pointing to a consented/generated mono PCM16 16-kHz WAV saying
"Please go to the next slide". Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_speech_stream.py -k live-deepgram -q -s --tb=short
```

Local live evidence on 2026-11-12 used the built-in Intel microphone array in Windows
compatibility mode, English, EU Nova-3, training opt-out, and the production
Qt/API/PostgreSQL/provider path. In the final consented 30-second run, the requested
phrase was recognized with 25 partials and seven finals. Measured final segment-end-to-client
delays were 124/151/1254/720/156/269/239 ms. Recognition remained active without retries
or rejected updates until explicitly stopped. These observations do not guarantee future
latency/accuracy or establish deployed staging acceptance.

Normal Qt/shared capture failed with Windows `OpenError` on both the built-in array and
AirPods. Compatibility mode's real-widget hardware test passed capture, fullscreen,
mute/unmute, pause/resume, and session cleanup. The microphone work also exposed adjacent
provider boundaries of 4.5500003 vs 4.55 seconds; a generated-speech regression after the
rounding fix returned seven partials and four finals without rejected updates.

The opt-in test can use the actual device instead of a WAV with
`BUDDY_SPEECH_TEST_MICROPHONE=1` and listens for 30 seconds after recognition is ready.
It requires a person saying the test phrase; never enable it unattended.

On Windows, `BUDDY_SPEECH_COMPATIBILITY=1` selects the same optional exclusive capture
mode available in Present. `BUDDY_SPEECH_MICROPHONE_NAME` can select a unique device
description substring (for example, `Microphone Array`); an ambiguous selection fails.
Without a selection, the test uses the system default input. See the
[microphone guide](microphone.md) for device-sharing limits and controls.

Before claiming staging acceptance, repeat with the deployed backend and a signed-in
human speaker using the real microphone. Verify text/timing, mute/pause/resume/stop,
device loss, account/session end, provider outage, reconnect gaps, and manual navigation.
Record provider/model, language, sanitized timing/accuracy evidence, and deployed revision.
