# Constrained presentation agent (#20)

The runtime proposes one typed presentation target from the current candidate set,
or explicitly keeps the current view. It uses #19's saved summaries and validated
actions, not the obsolete semantic graph. It does **not** execute commands, mutate
session state, publish an applied-action event, or enable autonomous transitions.
#21 owns calibrated confidence, persistence, cooldown, hysteresis, animation gates
and the policy-to-executor connection. Model confidence is not a calibrated probability
and is never execution permission.

The separate opt-in [transition policy](transition-policy.md) now connects accepted PDF
proposals to the player after fresh server/local checks. Suggestions remain manual by
default. Its PowerPoint integration and independent release evidence are still pending;
this does not change the agent endpoint's proposal-only contract.

## Local setup and manual check

Apply migrations through `0022_agent_request_window`. Set backend-only
`BUDDY_COHERE_API_KEY` and `BUDDY_AGENT_ENABLED=true`.
`BUDDY_AGENT_REQUESTS_PER_MINUTE` defaults to the trial model allowance of 20;
increase it only to match the credential's actual plan (maximum 500).
There is no default Buddy monthly decision cap. An optional positive
`BUDDY_AGENT_MONTHLY_LIMIT` sets an operator-selected monthly budget; unset it to disable
that application budget. Usage is still counted and provider quotas still apply.
No key belongs in the desktop, Git, logs, command arguments or Terraform values.
Backend opt-in defaults false. Existing embeddings setup/index preparation is reused.

Run the matching API and desktop, sign in, open an analyzed PDF in Present, or open a
published saved PowerPoint from Library > Review, choose **Open saved PowerPoint**, then
**Present with Buddy**. The PowerPoint path requires desktop Office on Windows, the
exact saved source bytes, a matching analysis/version and native slide IDs. Wait for
the synced presentation session before starting the microphone and enable **Live
suggestions**. The disclosure explains that recognized speech and candidate
summaries are sent to Cohere. The desktop prepares the index in a background worker;
wait for Ready before starting the microphone. If preparation fails, toggle suggestions
off/on to retry deliberately. This avoids an automatic paid retry loop.

Speak about a specific slide subject, continue speaking, then correct yourself to another
subject; also try “Do not go to the next slide.” Watch the suggestion beneath the transcript.
Slide controls stay manual. A corrected interim prefix or an explicit correction/negation, changing
the view/session/language, a capture gap/reconnect, stopping/muting/pausing the microphone,
or letting context go stale invalidates pending results through the rolling context. Ordinary
continuation does not by itself cancel a decision based on an unchanged stable prefix. Leaving Present or
switching sessions turns suggestions off; the opt-in is not saved across launches.

In Present, pause or resume automatic moves with the dedicated button or
**Ctrl+Shift+P**. Use **Undo automatic move** (**Ctrl+Shift+Z**) to return to the
previous view after an automatic transition. Undo pauses automatic moves.
These controls apply to saved PDFs and verified PowerPoint shows. The PowerPoint
shortcuts work while Buddy has keyboard focus. The desktop sends its current local slide
ID with each agent request and transition check. This keeps relative commands and topic
candidates anchored to the visible slide even when the server's saved recovery position
is behind. The API still checks ownership, active-session status, analysis/source identity
and that the local slide belongs to that analysis. The desktop checks again that the local
slide has not changed while a result was in flight. Older clients without a local slide ID
retain the state-version check. Manual PDF position is saved in the background for active
session versioning, but opening a PDF starts on its first locally loaded slide and does
not restore a position from an earlier session. Active-session conflict snapshots still
use the existing conflict handling. Automatic navigation does not wait for or write a
server position update.
Local PowerPoint without a saved API session is not eligible for this runtime. A
disconnected, changed or unverified Office slideshow stops transcription and discards
the saved-session binding. The live slide is authoritative when resuming a PowerPoint
session; Buddy syncs that slide to the server and does not move the audience's show to
an old saved position. PowerPoint suggestions remain proposals, never commands.
The native session currently syncs the live slide identity; Office does not provide
Buddy with a trustworthy region state for arbitrary manual presenter annotations or
zoom, so those are not represented as a saved focus region. PDF region state remains
fully tracked. A PowerPoint regional suggestion is eligible only with independent
semantic evidence; a successful Office drawing or zoom check is insufficient.

Terraform `enable_presentation_agent` defaults false and requires
`enable_semantic_candidates` for the existing API-only secret. It injects the enable flag
only into the API, not workers or migrations. Staging requires a separately reviewed
deployment and operator-populated secret.
The existing staging worker runs on Linux. It cannot perform Office validation, so
PowerPoint regions from that worker remain unavailable for action suggestions unless
an isolated Windows/Office validation route completes its independent checks. A
published PowerPoint analysis can still support slide-level retrieval and suggestions.
Before calling staging end to end ready, confirm the deployed API image, database
migration, feature flags, Cohere secret and Deepgram secret; merging code does not
enable those services.
This issue does not apply cloud infrastructure or distribute credentials. Because API
and workers use strict schema readiness, use the existing compatibility-bridge and
paused-worker rollout for the additive table. Never downgrade the live database as an
automatic rollback step. The table contains credential hashes/counts/timestamps only.

## Buddy Mini

On a synced saved PDF or PowerPoint session, click **Enter Buddy Mini** in Present.
PDF opens its slideshow if needed. Buddy floats in its own window while the main
workspace is hidden. With two displays, Mini stays off the slideshow display.
With one display, the overlay can be visible in whole-screen shares. In Settings >
Buddy Mini, choose the fixed screen corner or enable automatic entry when opening
a saved deck from the library. Mini starts once the saved session is connected.

Hover or click the mascot to reveal icon buttons for microphone, live suggestions
and automatic moves together, undo,
Settings, and Open Buddy. Click the mascot or focus it and press Space, then Tab
through the controls. Open Buddy or Escape restores the workspace. Closing Mini,
opening Settings, ending the show, or losing the saved session stops capture.
For PDFs, Escape in either the audience window or Mini, Open Buddy, and closing
Mini also close the audience window and return the slide to Present. Capture and
live suggestions stop; enter Mini again to resume presenting.
Mini remains visible in standby when an Office slideshow or session disconnects. Its
voice controls become available again when a verified show is connected.
On Windows, sleep returns Buddy to the main workspace and stops capture and
automatic moves. Use **Enter Buddy Mini** to restart the overlay after waking.
Listening ripples appear only to either side. Reduce motion keeps the mascot and
listening marks still.

Mini is only a visual control: capture, recognition, context and decisions are the same as
with the main window (#255). It shows no live transcript. Because the main window is hidden,
Windows may power-throttle Buddy, so Buddy opts out of power throttling while the microphone
is on, and up to about two seconds of audio (was 0.4 s) can wait for the GUI thread before the
oldest is dropped. The desktop log records `presentation_mini event=entered|left`,
`presentation_speech event=audio_gap dropped_packets_total=…`, and
`presentation_speech event=recognition_restart reason=…`, so lost speech can be traced.

Enable **Buddy Mini completion sound** in Present before entering Mini to hear a
short chime after completed automatic moves. Listening and the speech context continue
during the chime, exactly as without Mini (#255).

## HTTP and tool contract

`POST /api/v1/sessions/{id}/agent` uses the existing bearer authentication/revocation
and persisted ownership checks. Responses are `Cache-Control: no-store`.
`presentation-agent.v1` input binds expected state version, context epoch, stream id,
sequence watermark, aware sent_at, coherent passage, latest utterance, and up to 64
speech segments with sequence/text/final status. The passage must exactly join the
segments, end with the latest segment, and fit 4096 characters. The latest segment may
be a stable prefix corroborated by consecutive Deepgram interim hypotheses; only whole
common tokens are sent. The desktop bounds provisional requests to three per segment,
with follow-up requests admitted when another whole word becomes stable. While one request
is running, the desktop coalesces updates to the newest materially changed stable prefix
and checks it when the request finishes. Ordinary changing tail text is not sent. Only
the desktop's validated fresh capture context is eligible;
server timestamps bound transit, not proof that a remote client actually captured
microphone audio. Bodies are bounded to 64 KiB and five seconds.

The server reads active session, current source/job/analysis and canonical region
eligibility in a short repeatable-read transaction. New clients include the locally
visible slide ID; the server validates it against the pinned analysis and uses it as the
navigation origin. If that optional field is absent, the server requires the persisted
state version to match and uses the saved slide. It closes the transaction before
embeddings or Chat, builds at most ten options including no action, and resolves the
provider's opaque `c0`–`c9` choice back to a server-owned target. Missing options,
invented ids, invalid evidence, malformed JSON, tools/code, truncation and nonfinite
confidence are rejected. Region geometry is taken from the saved validated action,
never the provider. Options contain bounded summaries/purposes and current-view context;
the provider does not receive the whole deck or arbitrary callable tools. Up to eight #19
candidates are retained, with at most two authoritative adjacent slides added; semantic
hits are not evicted to make room. Natural navigation therefore does not depend on four
English command spellings or whether a neighbor was semantically shortlisted. Current
focused purpose/current-view markers distinguish an already visible validated region;
unknown manual focus is not assigned an invented semantic label.

Tools are `no_action`, `next_slide`, `previous_slide`, `go_to_slide`, `reset_view`,
`zoom_region`, and `highlight_region`. Targets contain slide_id, optional action_id,
and a region only for validated regional actions. Non-adjacent slide selection is
explicitly typed. Reset is available only when the persisted current view is focused.
Only currently usable, validated, independently semantically verified regions qualify
for either format. Office execution proof alone does not qualify a PowerPoint action;
when independent visual checks are unavailable, PowerPoint still offers slide-level
retrieval and navigation suggestions without unsafe regional actions.

After selection, a new transaction rechecks session/source/analysis and action
eligibility/geometry. Legacy requests also require the persisted state version to match;
new requests stay anchored to the validated local slide ID. The desktop rechecks its local
slide, source, speech freshness and opt-in before execution. Ownership failures remain 401/403/404. A request rejected before provider selection returns 409 with a stable, safe reason code and request ID: `speech_context_expired`, `session_inactive`, `session_state_changed`, `analysis_changed`, or `analysis_unavailable`. The API emits a `presentation_agent_rejected` structured log containing only the request ID, reason, request age when relevant, and expected/current state versions when relevant. It never logs transcript text, tokens, or credentials. The native client shows a specific recovery hint and request reference for these known conflicts; unknown conflicts remain generic. Fresh speech is required after a stale request; rejected input is never replayed. No authorization decision is cached. The native client validates
the response and rechecks the complete live snapshot both after HTTP and at GUI
consumption, including changes that occur before the next scheduling timer.

Output binds a decision UUID, session/analysis/version/local active slide, epoch/stream/sequence and input
digest; it includes a typed target, decision_source (model/navigation/fallback), finite
reason, optional confidence, evidence segment
indexes and elapsed_ms. It contains no free-form model explanation. A successful HTTP
response may legitimately be no action because of uncertainty, budget, pacing, disabled
configuration, invalid provider output, provider failure, stale eligibility or a deadline.

New desktop requests also send the local focus view and region alongside the local slide.
This prevents a saved zoom or highlight on an older slide from being treated as the current
view after automatic navigation. Older clients without these fields retain the persisted
focus behavior; a changed local slide defaults to full-slide focus.

### Temporary decision diagnostics

For a controlled evaluation only, set `BUDDY_AGENT_DIAGNOSTICS=true` and
`BUDDY_AGENT_DIAGNOSTICS_SESSION_ID=<session UUID>` on the API. Both values are required
together; traces are emitted only for that exact session. The resulting
no-store API response includes the utterance/segments, retrieved candidates and scores, the
exact option descriptions and target mapping, embedding query views with per-document cosine
scores and per-slide ranks, the shortlist/fusion evidence, the provider request and structured
response (including Jev's per-option/action and `presenting_now` probabilities), and Buddy's
validated proposal. The normal server log remains content-free, and the desktop strips the
diagnostics before the proposal reaches the UI. Capture the response in the authorized test
harness, keep it in the private local evaluation artifact, and turn both settings off when
the test ends. This is sensitive speech and presentation content; do not commit or upload
the captured artifact. With diagnostics disabled, the response contract is unchanged.

## Provider, scheduling and limits

`DecisionProvider` separates the selection contract from its replaceable adapter.
The implementation uses `command-a-03-2025` at the fixed HTTPS Cohere Chat v2
endpoint, temperature zero, no reasoning mode, 160 output tokens and structured JSON.
There are no arbitrary tool calls, redirects, provider retries, provider-selected URLs or shell
capabilities. The Chat adapter borrows the existing embeddings HTTP pool (120-second idle
keepalive) when both are configured, avoiding a second cold connection to the same host.
The embeddings adapter owns shutdown of that pool; each operation keeps its own deadline.
This is connection reuse, not a second model request or an extra paid warm-up call. Provider messages/documents are untrusted data. Prompt instructions
encourage semantic topic selection, pronoun resolution, corrections and abstention;
prompt injection resistance is not a substitute for the deterministic allowlist/policy.

The supported Cohere schema subset omits numeric/array ranges; Buddy still validates
confidence in [0,1], finite values and at most eight unique valid evidence indexes.
Responses are bounded to 64 KiB. Chat has a 3.8-second deadline, total backend decision
work five seconds, and native HTTP six seconds; stale capture still wins over a slow
otherwise valid result. Embeddings retain their own bounded adapter.

### Decision provider: Jev alone (#243, #252)

Jev decides every slide action; Cohere takes no part in slide decisions. When
`BUDDY_JEV_API_KEY` is set (Terraform `enable_jev_decisions` and the secret
`<env>/agent/jev`), the candidate mode defaults to `direct_current`: Jev receives one option
per other slide, every eligible zoom/highlight action on the current slide, do nothing, and
reset when the view is focused, built from the saved deck analysis. There is no Cohere
embedding index, no per-sentence Cohere shortlist and no retrieval-based topic shortcut;
deck preparation returns ready at once. Explicit navigation ("next slide", "slide 5") keeps
its deterministic fast path. Jev accepts at most 255 choices, five of which are reserved for
the web-search and search-panel options, so a deck whose complete menu would exceed it
fails closed with `candidates_unavailable` and is never silently shortened.

The desktop no longer offers a decider setting. Older clients that still send
`decision_model: "cohere"` are decided by Jev. Without a Jev key the server falls back to the
earlier Cohere shortlist and Cohere decider (`BUDDY_AGENT_CANDIDATE_MODE=semantic`), which
is not used on staging or production. Cohere is still used by web search to write search words
and short answers ([voice web search](voice-web-search.md)).

Measured on 1,020 labelled sentences (decks of 8–120 slides) against the earlier Cohere
shortlist plus Jev: 85.0% against 65.7% correct on the same 220 sentences, 76.5% against 48.8%
on contextual sentences, about 244 ms against 480 ms per decision, and 0 errors in 1,020
decisions; accuracy held from 40 to 120 slides. See
[the Jev-only decision guide](jev-direct-current-prototype.md).

**Reset and future plans (#257).** While a region is zoomed or highlighted, reset has its own
option text (zoom back out, remove the highlight, show the whole slide) and Jev's state names
what is on screen (`current_view`, for example "zoomed into Show the revenue chart"). The
presenting-now question treats plans, goals and decided future events stated as fact ("we're
launching the new website in January") as presenting, while conditionals and previews of later
parts of the talk ("by the end we'll have covered...", "we'll get to that later") stay blocked.
Measured with the real Jev:

- reset requests: 16/16 correct (was 1/16);
- future-plan sentences: 13/14 (was 1/14);
- the 1,020-sentence set: unchanged at 892, with 7 false actions before and after.

**How Jev is asked.** Jev returns probabilities rather than text. One request sends the latest
sentence, the earlier sentences and the current slide summary, and asks two typed questions:

1. `action`, a choice. Each option is described in plain language, for example "Highlight this
   item on the current slide: ... It shows: ...", "Go to the next slide: ..." or "Do nothing".
2. `presenting_now`, a yes/no question: is the sentence presenting or requesting something now,
   rather than recalling, hypothetical, quoted, a question, negated or a preview?

**Unfinished sentences (#248).** When the desktop sends the stable start of a sentence that is
still being spoken (`provisional`), Jev's state is marked `latest_sentence_status: unfinished`.
The instructions also add one rule: act only if the words so far already clearly name, describe,
state a value from or ask for one option. On 528 partial sentences from the evaluation set,
wrong early actions fell from 26 to 9. Finished sentences are unchanged at 286/300.

**When Buddy acts.** Buddy acts only when `presenting_now` is at least 0.2 and the chosen option
has a probability of at least `BUDDY_JEV_CONFIDENCE` (default 0.6). Otherwise it does nothing:
reason `stay`, or `uncertain` when the chosen option is below the bar. Jev has a 2-second
deadline.

**Evaluation.** The wording and both thresholds were tuned on a 300-case labelled set across an
8-slide deck, tuning on one half and checking on the other:

- Production code: 285/300 correct (95%), 139/150 on the held-out half, 7 false triggers.
- About 250 ms average and 0.8 s worst case, with no errors.
- For comparison: Jev with the Cohere prompt scored 85%. Cohere alone scored 64% on a 100-case
  sample weighted towards hard cases, with 6 deadline misses.

**Data.** Jev is served from the US with zero data retention and no training. Enabling it sends
recent speech and slide text to TypeSafe in the US.
