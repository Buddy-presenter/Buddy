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
retain the state-version check. Manual position is saved in the background for recovery;
automatic navigation does not wait for or write a server position update.
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
Responses are bounded to 64 KiB. Chat has a three-second deadline, total backend decision
work 4.5 seconds, and native HTTP six seconds; stale capture still wins over a slow
otherwise valid result. Embeddings retain their own bounded adapter.

The Qt scheduler runs off the GUI thread, with one in-flight operation, newest-only
coalescing, no queue of old transcripts and no fixed post-final wait. The server admits
model work immediately while its rolling 60-second provider window has capacity,
rather than spacing requests 3.2 seconds apart. Unspent admission deferrals
follow the server wait for that revision only. New completed speech can be checked
immediately, so a navigation command does not inherit an earlier model admission wait.
New stable interim checkpoints and completed revisions can trigger requests; only one
operation is in flight and newer evidence is coalesced rather than queued. The same
revision is not automatically retried after a provider failure. A `paced` response
includes bounded `retry_after_ms`: since no provider attempt was spent, the latest
still-fresh revision can be deferred at most twice without entering a FIFO. Expired or
changed input is discarded rather than retried. A pending result survives ordinary
continuation only while the submitted stable prefix remains unchanged; a corrected prefix,
explicit correction/negation, or session/view change supersedes it. Preparation has its existing 125-second client bound and
stays off the GUI thread; shutdown waits for bounded work after the event loop exits.

An atomic PostgreSQL row per API-key hash shares the rolling provider window and any
explicit operator budget across API replicas/restarts. The DB clock sets UTC month
boundaries and window expiration; at most 500 timestamps are retained. Lowering the rate
does not forget prior reservations. A real Chat 429 causes a shared 60-second cooldown,
including across restarts; no automatic provider retry is issued. External uses of the
key and provider-specific enforcement windows remain outside Buddy's accounting, so
local admission cannot guarantee provider acceptance. The provider's limits win.
Unambiguous whole-utterance next/previous commands reuse the validated deterministic path,
including no action at deck boundaries, without spending Chat/embedding allowance.
Other admission reserves before embeddings too, so a caller beyond the model allowance
cannot spend query calls. Failed admitted attempts count conservatively, even if Chat is never
reached. At most one decision per owner and sixteen owners are in flight per process.
The former default 100-decision and 750-embedding monthly caps are removed. This does
not remove Cohere's trial monthly allowance, token/input limits or rate enforcement.
The two-second per-owner embedding interval is also removed; in-flight concurrency
and identical-input caching remain. These changes remove scheduling delays, not the
network/model processing time of a single decision.

Migration 0022 is additive and preserves usage counts. Because the old row has no
complete request window, existing credentials are conservatively blocked until 60 seconds
after their last recorded call during upgrade; inactive keys are unaffected. Use the
existing compatibility-bridge/worker rollout for the new schema head, not a live schema
downgrade. No cloud deployment is performed by this change.

The deterministic navigation path accepts complete next/previous instructions and
numbered destinations such as “go to slide 8”, “show me page twenty-one” and “take me to
slide 125”. It supports every numbered destination within Buddy's 500-slide deck limit,
using digits or English cardinal words. Numbered commands outside the deck or for the
current slide safely propose no action; they never fall through to a potentially unrelated
semantic match. Quoted, negated, conditional, question and embedded-command forms are not
treated as navigation. Exact navigation commands bypass embedding and chat provider calls.
Other natural phrasing and content-driven matching still use semantic retrieval and the
model. The model receives the current page number and deck size and is instructed to
prioritize the latest navigation intent over old topics.

## Privacy and decision traces

The standard Cohere API receives completed speech and selected slide summaries/purposes;
audio still goes only to the configured speech provider. The requester's acceptance of
standard API retention and training-enabled development applies here. This is not EU-only
or zero-retention processing. Agent requests/results are not persisted as speech history. Scheduler dedupe keys contain
segment ids/revision numbers, not transcript text; completed/failed in-flight snapshots
are released, and disabling/shutdown clears pending snapshots.
The existing five-minute speech journal is unchanged, as are its backup limitations.

The `presentation_agent_decision` operational log contains only decision_id, request_id, finite tool,
finite reason, decision_source, elapsed_ms, operation phase, a whitelisted dependency
failure code and fixed model id. No transcripts, summaries, prompts, raw
provider responses, evidence text, secret keys, action coordinates or exception messages
are logged. Evidence indexes/confidence live in the no-store response, not ordinary logs.
Failures return safe finite status/reason codes; they do not cause slide movement.

## Verification

See [presentation replay evaluation](presentation-replay.md) for deterministic corpus
replay, candidate allowlist gates, captured execution scoring and recording permission.
Synthetic contract gates are separate from real service/policy release evidence.

Component tests use real PostgreSQL/session state and a substituted provider boundary.
They cover owner denial, no action, valid selection, authoritative region geometry,
malformed/extra/tool/truncated output, finite confidence/evidence, stale state after
provider work, deadlines, shared atomic pacing, restart/monthly budgets, native response
validation, partials, newest-only coalescing and manual invalidation before GUI delivery.
Paid/live checks are explicit and excluded from CI. Natural-language evaluation and
latency samples are evidence for those cases, not a general accuracy or P95 guarantee.


Provider references: [Command A](https://docs.cohere.com/docs/command-a),
[structured output subset](https://docs.cohere.com/docs/structured-outputs), and
[rate limits](https://docs.cohere.com/docs/rate-limits). During live integration,
Command A+ intermittently returned prose/truncation despite JSON mode and disabled
thinking, so the shipped adapter uses the established non-reasoning Command A model.
Buddy rejected those earlier results; they were not successful abstention decisions.
This change does not replace either Embed 5 model or rebuild document indexes.
