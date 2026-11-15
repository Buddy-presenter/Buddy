# Constrained presentation agent (#20)

The runtime proposes one typed presentation target from the current candidate set,
or explicitly keeps the current view. It uses #19's saved summaries and validated
actions, not the obsolete semantic graph. It does **not** execute commands, mutate
session state, publish an applied-action event, or enable autonomous transitions.
#21 owns calibrated confidence, persistence, cooldown, hysteresis, animation gates
and the policy-to-executor connection. Model confidence is not a calibrated probability
and is never execution permission.

## Local setup and manual check

Apply migration `0021_agent_quotas` after `0020_pdf_checkpoints`. Set backend-only
`BUDDY_COHERE_API_KEY` and `BUDDY_AGENT_ENABLED=true`. Optional
`BUDDY_AGENT_MONTHLY_LIMIT` reduces the default 100 attempted decisions/month (1–100).
No key belongs in the desktop, Git, logs, command arguments or Terraform values.
Backend opt-in defaults false. Existing embeddings setup/index preparation is reused.

Run the matching API and desktop, sign in, open an analyzed PDF in Present, and enable
**Live suggestions**. The disclosure explains that recognized speech and candidate
summaries are sent to Cohere. The desktop prepares the index in a background worker;
wait for Ready before starting the microphone. If preparation fails, toggle suggestions
off/on to retry deliberately. This avoids an automatic paid retry loop.

Speak about a specific slide subject, correct yourself to another subject, then try
“Do not go to the next slide.” Watch the suggestion beneath the transcript. Slide
controls stay manual. Starting a partial, changing the view/session/language, a capture
gap/reconnect, stopping/muting/pausing the microphone, or letting context go stale
invalidates pending results through the existing rolling context. Leaving Present or
switching sessions turns suggestions off; the opt-in is not saved across launches.
Local PowerPoint without a saved API session is not eligible for this runtime.

Terraform `enable_presentation_agent` defaults false and requires
`enable_semantic_candidates` for the existing API-only secret. It injects the enable flag
only into the API, not workers or migrations. Staging requires a separately reviewed
deployment and operator-populated secret.
This issue does not apply cloud infrastructure or distribute credentials. Because API
and workers use strict schema readiness, use the existing compatibility-bridge and
paused-worker rollout for the additive table. Never downgrade the live database as an
automatic rollback step. The table contains credential hashes/counts/timestamps only.

## HTTP and tool contract

`POST /api/v1/sessions/{id}/agent` uses the existing bearer authentication/revocation
and persisted ownership checks. Responses are `Cache-Control: no-store`.
`presentation-agent.v1` input binds expected state version, context epoch, stream id,
sequence watermark, aware sent_at, coherent passage, latest utterance, and up to 64
completed speech segments with sequence/text. The passage must exactly join the
segments, end with the latest segment, and fit 4096 characters. Only the desktop's
validated fresh capture context is eligible; server timestamps bound transit, not
proof that a remote client actually captured microphone audio. No partial input is
sent by the desktop. Bodies are bounded to 64 KiB and five seconds.

The server reads active session, current source/job/analysis and canonical region
eligibility in a short repeatable-read transaction. It closes the transaction before
embeddings or Chat, builds at most eight options including no action, and resolves the
provider's opaque `c0`–`c7` choice back to a server-owned target. Missing options,
invented ids, invalid evidence, malformed JSON, tools/code, truncation and nonfinite
confidence are rejected. Region geometry is taken from the saved validated action,
never the provider. Options contain bounded summaries/purposes and current-view context;
the provider does not receive the whole deck or arbitrary callable tools.

Tools are `no_action`, `next_slide`, `previous_slide`, `go_to_slide`, `reset_view`,
`zoom_region`, and `highlight_region`. Targets contain slide_id, optional action_id,
and a region only for validated regional actions. Non-adjacent slide selection is
explicitly typed. Reset is available only when the persisted current view is focused.
Only currently usable, validated, semantically verified PDF regions qualify.

After selection, a new transaction rechecks session/version/source/analysis and action
eligibility/geometry. Changed state, analysis content or eligibility becomes no action. Ownership failures remain 401/403/404 and stale session
input returns 409. No authorization decision is cached. The native client validates
the response and rechecks the complete live snapshot both after HTTP and at GUI
consumption, including changes that occur before the next scheduling timer.

Output binds a decision UUID, session/analysis/version, epoch/stream/sequence and input
digest; it includes a typed target, decision_source (model/navigation/fallback), finite
reason, optional confidence, evidence segment
indexes and elapsed_ms. It contains no free-form model explanation. A successful HTTP
response may legitimately be no action because of uncertainty, budget, pacing, disabled
configuration, invalid provider output, provider failure, stale eligibility or a deadline.

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
otherwise valid result. Embeddings retain their own bounded adapter and allowance.

The Qt scheduler runs off the GUI thread, with one in-flight operation, newest-only
coalescing, no queue of old transcripts, no fixed post-final wait, and at least 3.2
seconds between admitted attempts. Unspent admission deferrals follow the server wait.
New completed revisions can trigger requests; the same
revision is not automatically retried after a provider failure. A `paced` response
includes bounded `retry_after_ms`: since no provider attempt was spent, the latest
still-fresh revision can be deferred at most twice without entering a FIFO. Expired or
changed input is discarded rather than retried. New partials invalidate prior results but are
not themselves submitted. Preparation has its existing 125-second client bound and
stays off the GUI thread; shutdown waits for bounded work after the event loop exits.

An atomic PostgreSQL row per API-key hash shares the 100-attempt monthly cap and
3.2-second pacing across API replicas/restarts. The DB clock sets UTC month boundaries.
Exact whole-utterance next/previous commands reuse the validated deterministic path,
including no action at deck boundaries, without spending Chat/embedding allowance.
Other admission reserves before embeddings too, so an exhausted or rapid caller cannot spend
query allowance. Failed admitted attempts count conservatively, even if Chat is never
reached. At most one decision per owner and sixteen owners are in flight per process.
The 750 embedding plus 100 agent caps leave a buffer below the documented 1000-call
trial allowance; outside key use and disposable diagnostic schemas are not counted by
this database. This is a development allowance, not unlimited presentation time.

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
