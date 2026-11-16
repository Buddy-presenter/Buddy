# Presentation replay evaluation (#25)

The harness replays permissioned frozen transcript/context, analysis, candidate, and
session snapshots through the **production** lexical resolver, provider prompt builder,
Cohere HTTP-response parser, navigation fast path and candidate allowlist. It also
scores captured real service/policy execution traces using the same scorer.
It writes versioned trace/report JSON to a new directory and never overwrites evidence.
It does not enable autonomous transitions or change a presentation session.

## Reproduce the contract gates

Install the backend development project as in [development](development.md), then run
from the repository root:

```sh
python -m buddy_api.replay tests/fixtures/replay/development.json --gates tests/fixtures/replay/development-gates.json --output replay-artifacts/development
python -m buddy_api.replay tests/fixtures/replay/holdout.json --gates tests/fixtures/replay/holdout-gates.json --output replay-artifacts/holdout
python -m pytest backend/tests/evaluation/test_replay.py
python scripts/check_replay_freeze.py
```

Use a new output directory for each run. Exit codes: 0 passes the selected gates;
1 is a scored regression or missing release evidence; 2 is an invalid input,
expired permission, changed digest/prompt, or unavailable output directory.
Errors omit exception details because validation errors may contain private speech.

Each checked-in split has 21 frames with hesitation, a digression, a return to an earlier
topic, silence, ambiguity, ASR errors, corrections, negation, quoted navigation,
deck boundaries, an already focused region, reset, prompt injection, invented choices,
invalid evidence and provider throttling. Both files are **authored synthetic contract
fixtures**, including their model replies, latency and token counts. The holdout file is
a second smoke partition with minor wording differences, not an independent human
recording benchmark. A passing result is not a natural-language accuracy, latency,
PowerPoint, semantic retrieval, or autonomous-transition release claim.

Offline replay reads no microphone, credentials, database, or remote service. The HTTP transport
boundary supplies frozen raw responses; reference targets are used only by the scorer,
never by the runtime. A recorded response is bound to the SHA-256 of the exact outgoing
provider request, including the current system prompt. Prompt drift fails replay instead
of silently replaying answers to an obsolete request. Normal agent requests use the
same extracted `decision_passage` and `navigation_selection` functions. Exact relative
and numbered slide-navigation commands use the production deterministic path and do not
consume frozen/live provider replies.

## Fresh provider evaluation

To measure new decisions instead of replaying recorded responses, explicitly opt in:

```sh
python -m buddy_api.replay tests/fixtures/replay/private/development-v1.json --gates tests/fixtures/replay/private/development-v1-gates.json --live-provider --max-provider-calls 20 --output replay-artifacts/fresh-provider-v1
```

Set backend-only `BUDDY_COHERE_API_KEY` through your secret environment, never a command
argument. This mode makes paid calls to the existing fixed Cohere endpoint using the
production adapter, timeout, response validation and allowlist. It measures actual
request latency and billed input/output tokens; references are never passed to the model.
The entire call count is checked against the explicit limit before any request; there
are no retries. Shared deployed-service quota admission is outside this direct provider
benchmark; respect the provider's plan/rate limits. Missing billing (including an
unmeasured failed request) makes the cost gate fail with `billing_usage_unknown`.

Use a permissioned evaluation corpus with independent semantic references. The transport
fault fixtures in the checked-in smoke partitions test deliberately malformed **recorded**
responses and are not a fresh-model benchmark. Consented corpora additionally require
`permission.provider_processing: true`, backed by the actual cloud-processing consent.
Fresh runs are marked `captured_provider`, use the frozen candidate/analysis snapshots,
and do not measure live embeddings, database admission or policy application. Consequently
they cannot pass `--release`, which requires captured service/player evidence.

## Corpus contract and clocks

`buddy_api.replay.Corpus` is the strict `presentation-replay.v1` schema. JSON input files
are bounded to 8 MiB; extra fields, nonfinite numbers, empty corpora, duplicate frame/session
IDs, missing slides and unordered/out-of-duration frame clocks are rejected.

Each session contains its saved PDF `AnalysisResult` or native `PowerPointAnalysisResult`,
explicit duration and ordered frames.
Each frame contains `current`, `focus`, `focus_region`, capture-relative `at_ms`, the
original validated `AgentRequest`, optional saved semantic `candidates`, recorded provider
HTTP status/body/billed usage and elapsed time, independent `expected` target and scenario
tags. `request: null` represents silence: no provider call or action is synthesized.
Requests retain their stream, sequence watermark, context epoch and persisted state version;
the production request validator enforces coherent completed segments. Replaying an old
snapshot is an offline operation, not permission to send stale speech to a live session.

Absent `candidates` uses the real deterministic lexical resolver. Supplied candidates
represent a frozen semantic shortlist from the candidate service, not whole-deck tools.
Production `options` still checks regional eligibility, authoritative geometry, focus,
neighbors, duplicate options and the ten-option limit. Replaying a frozen semantic
shortlist does not measure a fresh embedding/retrieval provider. Capture a new shortlist
to evaluate a changed embedding configuration; report that distinction.

Every report and trace binds the exact corpus file SHA-256, including its references
and permissions. The gate manifest binds that same digest and reviewed thresholds.
Keep raw bytes when moving frozen files. All schemas can be exported locally, for example:

```sh
python -c "import json; from buddy_api.replay import Trace; print(json.dumps(Trace.model_json_schema(), indent=2))"
```

## Real service and transition evidence

Supply a trace from an instrumented real service/player run to score applied behavior:

```sh
python -m buddy_api.replay tests/fixtures/replay/private/holdout-v1.json --gates tests/fixtures/replay/private/holdout-v1-gates.json --trace tests/fixtures/replay/private/service-policy-trace.json --release --output replay-artifacts/release-v1
```

`presentation-replay-trace.v1` records:

- `corpus_sha256`, `provenance` (`captured_service` for an actual service run) and
  the deployed `policy_version`;
- exactly one observation for each `(session_id, frame_id)`, with the service's
  `proposed` target and complete ordered server-owned `allowed` targets;
- finite `elapsed_ms` and billed `input_tokens`/`output_tokens` from the actual request;
- ordered `applied` actions from the real player, each with its authoritative `target`,
  capture-relative `at_ms`, and `animated` boolean. An empty tuple means no transition.

Capture the real persisted session/analysis/candidate snapshot at each decision boundary;
do not reconstruct it from the expected answer. Record manual changes as new state
snapshots. Align player completion and transcript capture to one monotonic session
clock. Each applied event must belong to its frame's interval, before the next frame;
do not attach delayed actions to an obsolete candidate set. Capture observed animation
events rather than assuming every proposal animates. Keep credentials outside artifacts.

The scorer rejects omitted/duplicate observations, forged allowlists, changed corpus
digests, invalid clocks and applied actions without a policy identity. Out-of-set proposed
or applied targets fail every gate, even if the final visible state later becomes correct.
Trace provenance is an operator assertion, not a cryptographic attestation; retain the
private original service/player evidence and have an independent reviewer verify it.

The decision service remains proposal-only; the integrated #21 desktop policy owns
admission and execution. The capture adapter below records actual completion signals.
Until permissioned independent recording evidence exists, release acceptance remains open.
An imported trace can exercise the full scorer now. Unit-test policy traces are explicitly
synthetic and are never shipped as real execution evidence.

## Metrics and gates

Metrics are aggregate-only for both splits; no transcript or frame-level reference
answers are printed. Trace artifacts contain private target identifiers and require the
same access controls as the corpus.

| Metric | Definition |
| --- | --- |
| Target accuracy | Exact tool, slide, action ID and region match divided by all frames, including silence. |
| False transitions/minute | Incorrect or repeated applied non-no-action targets divided by total session minutes, including silence. Without policy evidence this counts incorrect proposals and the report scope is `agent_proposals`. |
| No-action recall | Correct abstentions divided by reference abstentions. |
| No-action precision | Correct abstentions divided by observed abstentions. Undefined denominators fail gates. |
| Latency P95 | Nearest-rank P95; recorded decision latency for proposal traces, last applied event minus capture time for policy traces. `latency_samples` reports coverage; missed transitions still fail target accuracy. |
| Unnecessary animations | Actual animation events for wrong/repeated targets or no action. `null` without policy evidence; never inferred as zero. |
| Cost | Sum of billed input/output tokens multiplied by the gate manifest's explicit USD-per-million rates. |
| Outside candidate set | Number of proposed or applied targets absent from that frame's production options; always a failure. |

Use [Cohere billed units](https://docs.cohere.com/docs/how-does-cohere-pricing-work),
not total internal tokens, for cost. Store dated plan/rate provenance in
`pricing_reference`; there are no assumed current prices. Checked-in rates are synthetic
accounting units. Failed paid attempts must retain their measured billing; missing usage
cannot honestly be replaced by zero. Include embedding costs separately when measuring
fresh retrieval; the current token metric covers the decision provider only.

Thresholds and corpus definitions must be chosen on development data before holdout
evaluation. CI runs both frozen contract gates on Python 3.12/3.14. The freeze checker
compares every established corpus/gate JSON with `origin/main` and rejects changes or
deletions. The first addition establishes the baseline; future changes require an
independently reviewed **new version**, retaining the previous corpus and gates.
Do not tune prompts, thresholds or reference answers on holdout failures. Report only
aggregates to the implementing contributor and ask the independent corpus maintainer
for a new sealed version after development changes. Synthetic fixtures cannot enforce
human blinding; access control and review are required for real holdout recordings.

## Recording permission and retention

No human audio, transcripts, external decks or consent documents are committed here.
Real evaluation requires informed permission from the presenter and every recorded
participant, including permitted cloud providers, processing region, purpose, reviewer
access, export, retention and withdrawal. Existing application sign-in or microphone
consent is not recording/evaluation consent. Do not record people who decline; redact
identifiers and unrelated audience speech before corpus admission.

Use `permission.kind: consented`, an opaque restricted consent-record `reference`, and
an explicit `expires` date. Expired corpora are refused. The deadline must be no later
than the participant's agreed retention; default operational retention is **30 days**
from capture unless a shorter term was agreed. The corpus owner must schedule deletion
of source audio, transcripts, derived traces, reports and copies/backups at that deadline,
and remove them immediately on withdrawal. The CLI does not delete recordings or backups.

Store real corpora and artifacts encrypted with named reviewer access, outside the repo
or in ignored `tests/fixtures/replay/private/` and `replay-artifacts/`. Never upload real
recordings as public CI artifacts. Keep consent evidence separately and use only its
opaque reference in the corpus. Synthetic corpus permission is declared explicitly and
does not authorize future human recordings or provider calls.


## Two-format capture and release acceptance

`Session.analysis` accepts the production PDF or PowerPoint analysis result. Native
regional targets retain PowerPoint actions and their validation evidence; a PDF analysis
must never be relabelled as a PowerPoint recording. Both use the production candidate,
prompt, navigation and allowlist functions. Proposal replay still measures proposals only.

For explicit private player capture, create `buddy_desktop.transition_capture.TransitionCapture`
with the frozen corpus digest and the correct provenance, policy/model identifiers. For
each frame, call `arm` with the actual decision identifier, proposal observation (including
its production allowed targets), local capture clock and corpus frame time. Connect
`PresentWidget.transition_observed` to `capture.completed`. Export `capture.document()`
to a new private trace file after capture. Silence/abstention frames must also be armed;
without a completion signal their applied list stays empty. The recorder rejects duplicate,
unbound and inferred completions. A mocked microphone, provider or Office boundary uses
`synthetic_contract`, never `captured_service`.

CI exercises actual Qt PDF commits and the production native adapter with Office substituted,
including persistence and undo tests. These deterministic tests cover implementation regressions.
They cannot substitute for permissioned holdout acceptance or interactive Office execution.
Office completion currently reports unknown animation timing (`animated: null`); the scorer
fails `animation_measurement_required` until that observation is independently measured.
Do not replace an unknown measurement with false just to pass a gate.

A dedicated opt-in **Recorded policy release acceptance** workflow scores private evidence
on a preconfigured interactive Windows runner labelled `replay-acceptance`. Configure the
`replay-acceptance` environment and `BUDDY_REPLAY_EVIDENCE_ROOT` variable. Install the backend's
pinned dependencies on that runner. No private artifacts are uploaded or printed. Protect
that runner/environment from untrusted refs and require operator review before dispatch.
No hosted CI run is described as a passed recording gate in its absence.

The evidence root must contain `pdf/` and `pptx/`, each with `holdout.json`,
`holdout-gates.json` and `service-policy-trace.json`. Run locally with:

```sh
python scripts/check_replay_release.py --evidence-root tests/fixtures/replay/private/release-v1
```

The command fails if either format is missing, mislabelled, expired, synthetic, incomplete,
outside gates or lacks real policy evidence. It prints only aggregate failures. Preparation
instructions are in [evaluation recordings](evaluation-recordings.md). Independent calibration,
permissioned holdout measurements and real Office acceptance remain outstanding until the
operator supplies and runs those recordings; no recording results are bundled here.


## Authorized scripted verification scope

The requester has authorized continuing without human recordings or independent
measurements. Generated-input live-player verification is the delivery acceptance
scope; the separate consented holdout release gate remains available for future
benchmarking and is not a required check for this authorized delivery. Do not relabel
scripted traces as independent holdout evidence or relax calibrated model admission.

`frontend/scripts/smoke_transition_policy.py` records actual PDF and Office policy
completion events, local completion/undo timings and file-backed test-state writes.
The speech input and eligibility/session transport are explicit test substitutes.
The measured aggregate report is in `docs/verification/scripted-policy-2026-11-15.json`.
The run found and verified a native preview/undo scheduling fix. Real Office manual
navigation/annotation/reconnect also passed in wide and standard generated decks.
No live provider/model accuracy, production backend admission or Office animation
duration result is claimed by those scripted checks; backend contracts remain covered
by current-head PostgreSQL CI. Private report/event artifacts stay in `replay-artifacts/`.
