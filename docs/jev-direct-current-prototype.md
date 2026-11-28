# Jev-only slide decisions (#252)

Jev alone decides slide actions; this replaced the Cohere shortlist. It is the default
whenever `BUDDY_JEV_API_KEY` is set (`BUDDY_AGENT_CANDIDATE_MODE` defaults to
`direct_current`). A Cohere key is not needed for slide decisions. No provider credential
belongs in the desktop or its settings.

The complete menu contains every currently eligible zoom/highlight action on the visible
slide, navigation to every other saved slide, no action, and reset when the current view is
focused, plus Jev's web-search and search-panel options. Other manual PowerPoint drawing
tools are not live-agent tools. Descriptions and source-measured target geometry use the
existing production builders. The same server and desktop eligibility, authorization, source,
freshness, transition-policy and execution checks still apply.

Jev supports at most 255 choices and five are reserved for search options; serialized
criteria are additionally capped at 256 KiB. A larger complete menu returns the safe
`candidates_unavailable` fallback; it is never silently shortened and never falls back to
Cohere. Preparation checks session ownership/source and returns ready without embeddings.
Explicit navigation fast paths remain; contextual navigation goes through Jev instead of the
retrieval-based topic shortcut, which jumped to the wrong slide when several slides share an
item name.

## Measured results

Paired with the earlier Cohere shortlist + Jev on the same Jev model and thresholds:

| | Jev only | Cohere + Jev |
|---|---|---|
| Correct, same 220/210 sentences (decks of 8–120 slides) | 85.0% | 65.7% |
| Correct, all 1,020 sentences | 87.5% | not run (trial-key limit) |
| Contextual sentences | 76.5% | 48.8% |
| Wrong target / missed action | 2 / 27 of 160 | 15 / 53 of 150 |
| Mean decision latency | ~244 ms | ~480 ms (Cohere ~249 + Jev ~231) |
| Errors | 0 of 1,020 | 0 of 210 |

Accuracy held from 40 slides (83.8%) to 120 slides (84.3%). On the 51-case native fixture
corpus (PDF and PowerPoint, three repeats, including Deepgram-recognized speech) Jev only
was 98.0% correct with no false triggers; PDF executed 42/43 chosen actions and PowerPoint
37/43. The weakest areas are topic carry-over across sentences and paraphrased slide names.
Synthetic decks and text replay, not human microphone recordings, produced these numbers.

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
The opt-in `--preserve-on-error` flag keeps only that run's synthetic schema and S3
evidence after interruption, with `checkpoint.json` identifying the exact fixtures for
recovery. Successful runs still clean up normally. Without that flag, failures also
clean up immediately.

The primary corpus is 43 labelled synthetic cases on equivalent eight-slide PDF and
PowerPoint sources. Labels precede the provider predictions. Each A/B pair uses the
same transcript, source, saved analysis and eligibility evidence. Order alternates
within each pair and is shuffled with a fixed seed for each of three repetitions.
Both local variants use the same 500-request/minute operator allowance so ordinary
benchmark replay is not measuring Buddy's lower default pacing limit. Actual provider
limits still apply; this is not a throughput/load test.
The driver waits for a minimum four-second interval between paired cases to respect
provider account limits. That wait is outside measured request latency and adds no wait
to Buddy's application code. `--pair-interval 0` disables it for an explicit throughput
probe; provider failures still count as failures, never correct abstentions.
`--also-audio` adds eight representative cases recognized once through the real
session WebSocket and Deepgram EU, then supplies those identical recognized words
to both approaches in both formats. Windows SAPI generates this audio; it is not a
human microphone/noise/accent test. The original text corpus stays unchanged.
Use `--speech-only --also-audio` instead of native validation/execution flags to remeasure
just those eight ASR cases in the common speech pipeline. This uses an ordinary PDF
session, does not select/execute actions, and makes no Office-readiness claim.

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
