# Jev without Cohere retrieval prototype (#252)

This is an opt-in experiment, not a rollout. The default remains semantic retrieval.
Run the prototype on a separate local API; do not enable it on shared staging or merge it
without reviewing the paired evaluation results.

Set `BUDDY_AGENT_ENABLED=true`, `BUDDY_DECISION_PROVIDER=jev`,
`BUDDY_AGENT_CANDIDATE_MODE=direct_current`, and the server-side `BUDDY_JEV_API_KEY`.
A Cohere key is not required for this decision path. Normal deck analysis providers
remain separate. No provider credential belongs in the desktop or its settings.

The complete menu contains every currently eligible zoom/highlight action on the visible
slide, navigation to every saved slide, no action, and reset when the current view is
focused. Other manual PowerPoint drawing tools are not new live-agent tools in this
experiment. Descriptions and source-measured target geometry use the existing production
builders. The same server and desktop eligibility, authorization, source, freshness,
transition-policy and execution checks still apply.

Jev supports at most 255 choices. Serialized criteria are additionally capped at 256 KiB.
A larger complete menu returns the existing safe
`candidates_unavailable` fallback; it is never silently shortened and never falls back to
Cohere. Selecting the Cohere decider in this mode also fails closed. Index preparation
checks session ownership/source and returns ready without document or query embeddings.

For the primary comparison, keep `jev-1.14.0`, its instructions, the 0.6 choice threshold,
and the 0.2 presenting-now threshold identical in both variants. The prototype changes
candidate construction only. Explicit navigation fast paths remain available in both.
The prototype sends contextual navigation through Jev instead of the retrieval-based
topic fast path. Report such routing differences separately.

Evaluation artifacts and measured results will be documented here after the paired
live-provider and native-player checks. A transcript-only replay must never be described
as a microphone test, and successful proposal selection must not count as successful
player execution.

## Reproduce the paired evaluation

Use a separate Windows checkout with Microsoft PowerPoint, an interactive unlocked
desktop, and the normal shared/backend/frontend development dependencies installed.
Install the optional fixture generator with
`python -m pip install -r tests/integration/requirements-jev-comparison.txt`.
Provide `BUDDY_TEST_DATABASE_URL` for a local disposable PostgreSQL database whose name
ends in `_test`. The runner creates and drops a random schema; it does not modify
the staging database. Sign in with `aws login --profile buddy-staging` first.

Save and close PowerPoint and leave the desktop idle during native captures. Run:

```powershell
python -m tests.integration.compare_jev_candidates --live --native-validation --execute --also-audio --repeats 3 --output build/jev-comparison
```

The AWS profile defaults to `buddy-staging-tools`. Secrets Manager provides Jev, Cohere
and Deepgram credentials to the Python process; the runner never writes those keys.
AWS S3 stores only synthetic source/evidence objects beneath a new owner's prefix;
the runner removes those exact prefixes and writes `cleanup.json`. It also invokes
Bedrock for actual native visual validation. These are paid, explicitly opt-in checks.
None of these provider calls run in normal tests or CI.

The primary corpus is 43 labelled synthetic cases on equivalent eight-slide PDF and
PowerPoint sources. Labels precede the provider predictions. Each A/B pair uses the
same transcript, source, saved analysis and eligibility evidence. Order alternates
within each pair and is shuffled with a fixed seed for each of three repetitions.
Both local variants use the same 500-request/minute operator allowance so ordinary
benchmark replay is not measuring Buddy's lower default pacing limit. Actual provider
limits still apply; this is not a throughput/load test.
`--also-audio` adds eight representative cases recognized once through the real
session WebSocket and Deepgram EU, then supplies those identical recognized words
to both approaches in both formats. Windows SAPI generates this audio; it is not a
human microphone/noise/accent test. The original text corpus stays unchanged.

The source analyses are authored fixtures, not fresh LLM-generated analyses. Each saved
focus action must pass the real isolated native runner and Bedrock judge before it can
be offered. Transient failures at the initial capture may retry at most three times;
all attempts remain in `validation-attempts.jsonl`. Retries never change a verdict or
bypass focus, source, ownership or eligibility checks. A fixture that cannot pass stops
the comparison rather than becoming an assumed executable option.

The runner exercises the real local FastAPI routes, PostgreSQL, Cohere embedding API,
Jev API, transition-check route and isolated PDF/PowerPoint players. Authentication
uses a synthetic identity at the local test boundary; Cognito sign-in and the deployed
staging API are not part of this benchmark. First-repeat text cases execute in the
native players. Later repetitions and audio cases measure selection, not repeated
player execution. Native execution timings include cold isolated player startup;
they must not be described as normal warm-app or microphone-to-visible latency.

For a separate provider-only capacity probe, run
`python -m tests.integration.jev_menu_stress --live build/jev-comparison/records.jsonl --output build/jev-menu-stress.json`.
This puts one clearly relevant choice last among 16, 32, 64, 128 and 255 options, three
times each. Its unrelated synthetic distractors are not validated executable actions
and its accuracy cannot stand in for a full large-deck benchmark.

`summary.json`, `records.jsonl`, captures and speech WAVs stay under ignored `build/`.
The public report includes aggregate metrics and synthetic case failures, never keys,
private deck content or raw provider diagnostic traces. Three repetitions of one case
are repeated measurements, not three independent accuracy examples.
