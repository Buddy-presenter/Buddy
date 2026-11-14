# Presentation candidates (#19)

## Semantic retrieval with Cohere Embed 5

Richer-PDF preparation is also repaired on this issue branch: private provider/page
checkpoints prevent a whole-deck grounding timeout from discarding successful AI work.
See [resumable PDF processing](pdf-workers.md#resumable-pdf-region-processing). Migration
`0020_pdf_checkpoints` follows the embedding migration; both require the reviewed rollout.
Pending-summary inspection does not bypass the candidate route's completed-analysis gates.

When the backend has `BUDDY_COHERE_API_KEY`, retrieval uses `cohere-embed5-v2`.
Without it, the lexical resolver remains available and identifies itself as
`summary-overlap-2`; it must not be described as semantic understanding. A configured
semantic service never silently downgrades after provider failure.

Call `CandidateService.prepare(access, session)` off the GUI thread before live
queries, or POST `/api/v1/sessions/{id}/candidates/prepare` with the bearer header.
Preparation embeds available slide summaries and currently usable action purposes
with `embed-v5.0-pro`, saving normalized 512-dimensional vectors in the database.
The index binds analysis, deck, owner, model space and document fingerprint. Repeated
preparation of unchanged content makes no model calls. Changes in summaries or
action eligibility require preparation again. Stale vectors cannot enable actions.

Live queries use `embed-v5.0-fast` to embed one coherent recent passage, bounded to
4096 characters. Version 2 removes the separate latest/recent vector mixture: preceding
sentences and references such as "these" reach the encoder together. New desktop callers
send at most four contiguous completed recognition segments, dropping older segments
whole to fit the bound. The server also bounds older clients' text, avoiding a partial
leading word when whitespace permits. This is a bounded capture window, not a semantic
topic classifier or a new linguistic sentence parser. Latest utterance remains separate
for whole-utterance navigation; it does not receive an extra semantic weight. Document
vectors and their model space are unchanged, so upgrading this query algorithm does not
require reindexing a prepared presentation.

Document indexing uses `search_document`; queries use `search_query`.
Positive-cosine matches form a ranked shortlist of at
most five slide destinations including current, plus region options and no change,
with eight candidates maximum. Similarity is a ranking signal, not a calibrated
confidence probability. Exact whole-utterance next/previous navigation keeps a
validated no-model fast path, including a no-change result at deck boundaries.
#20 owns decisions, abstention and execution policy.

### Limits and usage accounting

- Prepare: at most 1500 documents and two million characters, batches of 64, two
  simultaneous preparations per process, one per owner, 120-second deadline.
- Queries: one concurrent request per owner, at most one new call every two seconds
  per process, 16 concurrent owners total. A bounded 128-entry memory cache may
  reuse identical query vectors for five seconds. Speech vectors are never persisted.
- Provider: fixed HTTPS endpoint, no redirects, persistent connection pool, bounded
  responses, strict finite/nonzero 512-dimensional vectors, two-second query and
  20-second document-call deadlines. There are no automatic rate-limit retries.
- PostgreSQL atomically reserves at most 750 attempted calls per API-key hash per
  UTC calendar month, shared across API replicas and restarts. Preparation batches
  and query calls both count; failed attempts count conservatively. This leaves a
  buffer below the documented 1000-call trial limit but cannot account for key use
  outside this database. Disposable test schemas have separate counters. Account or
  key rotation is not a quota-control mechanism.

### Setup, deployment and privacy

Apply migration `0019_candidate_indexes` before upgrading API/workers. Both services
use strict schema readiness; use the normal reviewed compatibility-bridge and
paused-worker rollout. The additive index has an analysis/deck/owner foreign key
and cascades with analysis deletion. Vectors remain private database data subject
to the existing encryption and backup policies. No database extension is needed.

Terraform `enable_semantic_candidates` defaults false. Enabling it creates an
operator-populated `embeddings/cohere` Secrets Manager secret and grants only the
API execution role access to inject `BUDDY_COHERE_API_KEY`. No secret value belongs
in Terraform, Git, desktop settings, logs or command arguments. Worker and migration
containers do not receive it. This implementation applies no infrastructure itself.
For local development, load the DPAPI-protected key only into the backend process,
configure database/identity normally, and remove its process variable after shutdown.

The standard Cohere API receives summaries/action purposes during preparation and
latest/recent transcript text during queries. No audio is sent to Cohere. This is
additional processing beyond Deepgram and Buddy's journal. Retention and training
controls depend on the account; the requester explicitly authorized training-enabled
trial use and provider retention for this development. This is not EU-only or
zero-retention processing. Before a wider automatic agent/microphone rollout,
disclose Cohere processing in the product's consent/status flow. See
[Cohere data commitments](https://cohere.com/enterprise-data-commitments).

### Verification and limitations

Tests cover preparation caching, persistent indexes, temporary query caching,
cross-owner denial, atomic concurrent quota reservation, restart budget persistence,
malformed/nonfinite vectors, rate limits and no-model navigation. Paid provider
checks are explicit; CI substitutes the external provider boundary.

Actual CH8 SOL analysis was replayed through local HTTP and isolated PostgreSQL
with real Pro/Fast calls. Totient and simultaneous-congruence references rank the
intended page first. The correct page for “question two” is in the shortlist but
ranks behind page 2: embeddings do not solve every reference. There are zero usable
region actions in that deck, so no positive region-accuracy claim is made. Initial
semantic HTTP requests took about 0.85–1.3 seconds before connection pooling. Two
follow-up requests with pooling took 501 and 486 ms; this small sample does not
demonstrate the 500 ms P95 target. This is not deployed or microphone evidence.

## Legacy lexical protocol and behavior

Issue #19 uses the current persisted slide summaries and validated action purposes,
as agreed for the PDF-first analysis design. It does not reintroduce the historical
semantic graph or add a model/provider request. Candidate retrieval proposes targets;
#20 owns choosing an action and subsequent policy/executor checks still apply.

`POST /api/v1/sessions/{id}/candidates` is authenticated, owner-scoped and read-only.
The API checks the active session, expected state version, current deck/source/job
and pinned analysis. It reads the actual typed analysis and current validation state
in one repeatable-read transaction. Stale or changed state/analysis returns 409;
missing/foreign identity follows existing 401/403/404 behavior. Invalid input is a
redacted 422; oversized input is 413; dependency errors are recoverable 503 responses.
Responses are no-store. Request bodies and speech are never added to ordinary logs
or persisted by this route. Existing speech journal retention is unchanged.

To avoid reconstructing hundreds of unavailable regions on every live request, the API
may keep up to eight region-free typed analysis reads in memory for 30 seconds. Each
request still rechecks identity/ownership, active session/state, current source/job and
pinned analysis in PostgreSQL. The cache also binds the immutable content hash and the
append-only validation-history count in the same repeatable-read snapshot; a new check
invalidates the cached eligibility. Analyses containing any usable region always take
the canonical evidence-validation read path. This cache contains presentation data only,
never speech, and cannot cache an authorization decision or enable a region.

## Protocol and freshness

The versioned request `presentation-candidates.v1` carries expected_state_version,
context_epoch, stream_id, sequence, an aware sent_at timestamp and at most 8192
characters of recent text. The optional `latest_utterance` preserves the final
recognition segment separately (at most 4096 characters); when supplied it must
be the whole text or an exact final segment suffix. It is also omitted from reprs.
Older clients may omit it, retaining whole-window command matching. Bodies are limited to 64 KiB even with chunked transfer;
body receipt and the database operation each have five-second deadlines. Timestamps
older than five seconds or more than two seconds in the future are rejected. Clients
must keep wall clocks synchronized. This timestamp bounds request transit, not proof
of audio capture; the live desktop capture clock owns capture freshness.

`CandidateService.resolve` accepts #18's actual ContextSnapshot and saved session,
checks capture freshness/window and session/version before network access, rechecks
capture age/window before returning a response, sends the
bearer token only as a header and validates the bounded response. Call it outside the
GUI thread. The caller must recheck the live session/version/epoch against the result
before using it; a frozen snapshot or returned candidate cannot authorize an action.
This service is the callable boundary for #20; Present does not yet run an autonomous
agent or make a provider call for each partial transcript.

The service refuses partial latest segments and requests made within 200 ms of the
last change to the selected completed segments. It does not sleep or start an agent loop:
the #20 caller owns scheduling the off-thread request after this settling interval and
refreshing the snapshot. Freshness checks still run before and after network access.

The unchanged five-passage presenter-narration replay on the 61-page saved analysis
ranked all five intended slides first with v2 (v1 included four, with two ranked first).
HTTP requests measured 443–526 ms in that small local replay. Identity/source metadata
were mirrored locally; Cohere and PostgreSQL were real. This is typed narration evidence,
not a microphone, staging, general-accuracy or P95 guarantee.

The response binds session/analysis/state version, epoch, stream, revision sequence,
resolver version and SHA-256 of the exact request. Identical requests against identical
saved analysis/validation produce the same result. No transcript is echoed. Target
records contain kind, existing slide_id, optional validated action_id, bounded score
and a finite reason code. Region parameters remain in the authoritative action record;
clients cannot propose rectangles or assert usability through this API.

## Retrieval and bounds

Resolver `summary-overlap-2` normalizes Unicode (NFKC/casefold), tokenizes letters/numbers
and removes a small English stop-word set. It scans at most the validated 500-slide map
and the bounded saved action list. Scoring counts distinct overlapping words in available
summaries and usable action purposes. A unique highest positive match may propose an
earlier/later slide; a tie abstains from non-linear navigation. Current-slide and explicit
no-change candidates are always present. Whole-utterance English next/previous commands from `latest_utterance` (or the full
text for older requests)
produce a valid adjacent target except at deck boundaries. Negated or embedded command
phrases are not interpreted as navigation commands. At most two matching usable region
views from the shortlisted slides are included, with stable ordering and at most eight
candidates overall. PDF actions require validated, usable and semantic-verified results.
No rejected, pending or execution-only region action becomes eligible.

This first resolver is deliberately lexical: it does not guarantee synonym, pronoun,
translation or conceptual inference. Unicode topic matching works in other languages
when words agree, but explicit navigation phrases and stop words are English in v1.
Missing/ambiguous evidence returns conservative choices, including no change. Future
semantic retrieval can replace this versioned implementation if replay evaluation
shows a concrete need, without expanding the executable action contract.

## Verification

Run backend/tests/api/test_presentation_candidates.py with a disposable PostgreSQL
*_test database for real authorization, persisted session/source/analysis checks,
staleness, repeatability, read-only behavior, bounds and privacy. Replay tests cover
current, adjacent and earlier topics, ambiguous speech, validated views and 500 slides.
frontend/tests/test_candidates.py uses actual local HTTP for response binding, malformed
responses and stale-context refusal. tests/integration/test_speech_stream.py's deterministic
case exercises the production Qt speech parser, live context, candidate client, HTTP API
and PostgreSQL together. Only identity/speech-provider boundaries are substituted. No paid
provider or human microphone recording is required. These tests are local evidence,
not a claim of deployed or human-language accuracy acceptance.

Recognition segment boundaries are preserved rather than inferred from punctuation
or substring search. Thus an earlier topic followed by a separate "next slide"
segment can propose the next slide, while "do not go to the next slide" in that
same latest segment cannot. A segment is the existing speech provider segment,
not a new linguistic sentence detector; words split across provider segments are
not combined into an invented command. The full rolling text still drives topic
retrieval. Desktop clients accept resolver versions 1 and 2 during upgrades; the
server now reports version 2 so exact-request replay comparisons name the algorithm.
