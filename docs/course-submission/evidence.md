# Evidence ledger and failure matrix

Captured for [#229](README.md) on 28 November 2026. Each row states its scope;
an implementation, passing mock or Project Done card is not a live measurement.

## Results and provenance

| Evidence | Result | What it establishes and does not establish |
| --- | --- | --- |
| Fresh [development-v6](results/development-v6-report.json) and [holdout-v6](results/holdout-v6-report.json) replay | Each: 21 frames, target agreement 1.0, no-action recall/precision 1.0, outside-menu count 0, contract gates pass | Authored synthetic proposals through the production legacy Cohere adapter. No Jev call, microphone, live retrieval, actual transition or independent human holdout. Corpus/gate/report SHA-256 are in the [manifest](results/manifest.json). |
| Fresh replay/security/observability regressions | 34 passed, no skips | `test_replay.py`, `test_security_privacy.py`, `test_observability.py`; deterministic external boundaries. No persisted database or deployed alarm claim. Exact commands/environment in manifest. |
| [Jev direct-menu evaluation](../jev-direct-current-prototype.md#measured-results) | Source reports 87.5% correct / 1,020 synthetic-text sentences; 0 provider errors | Historical provider selection, not current whole-application accuracy. Source paired-summary denominators differ (220/210); raw records and a matching revision are required before a clean paired-effect claim. Its native 51-case / three-repeat summary reports 98.0% selection and PDF 42/43, PPTX 37/43 chosen actions executed. Repeats are not independent examples. Aggregate source is committed; detailed run artifacts remain local. |
| [Current-gate retrospective](../presentation-agent.md#decision-provider-jev-alone-243-252) | Captured responses project 140/206 intended actions and 7/39 false actions with 0.5/0.4 gates | One 10-page sample PDF, synthetic transcript events, proposal-only. This is not fresh Jev execution or a general success rate. Revised handoff still needs fresh replay. Do not tune on a held-out evaluation. |
| [Scripted player run, 15 November](../verification/scripted-policy-2026-11-15.json) | One generated-input sample per format; navigation/undo and duplicate suppression passed | Actual local players, substituted speech/eligibility/session transport. Not recognition or independent calibration. |
| [Voice readiness, 17 November](../verification/voice-readiness-2026-11-17.json) | PDF and Office player completion/undo recorded; requester accepted live testing scope | The agent did not independently observe the accepted human live run. Requester acceptance supplies no new per-case accuracy/latency data. |
| [Saved actions, 28 November](../verification/powerpoint-actions-153.md), [aggregate](../verification/powerpoint-actions-153.json) | PDF zoom/highlight and all 15 Office annotation types applied/reset; Office zoom blocked | Real PDF/Office executors, Windows 11, Office 16.0.20625; eligibility HTTP and session transport substituted. No independent semantic verification or authenticated publication measured. Source module hashes are in the aggregate. |
| [#26 threat model](../course-threat-model.md), [#39 monitoring](../operations-monitoring.md), [#29 timings](../presentation-latency.md) | Implemented controls, metrics and reporting commands | Designs and automated checks are available on the baseline. Monitoring guide still leaves staging delivery/recovery acceptance pending; do not infer delivery from resource definitions. [#44](https://github.com/Buddy-presenter/Buddy/issues/44) remains Waiting for review at this snapshot. |

## Latency and cost

| Measurement | Observed value and sample scope | Interpretation |
| --- | --- | --- |
| Fresh offline proposal replay | P95 139 ms, 20 timing observations per partition; cost field USD 0.00224 per partition | **Both are authored synthetic fixture values**, not wall-clock inference latency or actual spending. Rates explicitly say synthetic accounting units. The run makes no paid calls. |
| Historical scripted navigation/undo, 15 November | PDF 239.08 / 290.96 ms; PPTX 278.58 / 978.93 ms; n=1 each | Local completion observations with substituted boundaries. No meaningful distribution or p95. Office animation timing is unknown. |
| Historical voice-readiness navigation/undo, 17 November | PDF 15.01 / 45.84 ms; PPTX 386.63 / 1254.19 ms; n=1 each | A different baseline/run; not a before/after optimization comparison or speech-to-visible benchmark. |
| Historical direct-menu provider work | Approximately 244 ms mean versus approximately 480 ms with the earlier shortlist | Provider/request experiment described by the original guide. Not recognition, warm-app rendering or end-to-end p50/p95; paired denominators need reconciliation. |
| Historical slide-image decision estimate | Source states roughly USD 0.0002/decision and 25–35 ms p50 increase | Reported estimate in the agent guide; no dated billing artifact/rates are bundled. Not verified current cost or cost per successful task. |
| Current repeated speech-to-visible p50/p95; complete task cost | **Not measured in this package** | Collect #29 desktop/backend timings by decision ID, attempts/failures, sample counts, billed usage and dated rates. Include recognition, embeddings/search/analysis when used; do not replace missing usage with zero. |

## Failure matrix

States below are `observed`, `regression-covered`, `unsupported`, or `not-run`.
The latter three do not mean a live failure experiment passed.

| Trigger / boundary | State and evidence | Expected behavior / remaining limitation |
| --- | --- | --- |
| Windows refuses foreground during saved Office zoom | Observed blocked, 28 November saved-action aggregate | Reject zoom with a focus recovery hint; retain reset/manual controls. Never bypass the foreground guard. |
| Native execution misses in older provider comparison | Observed historical: PDF 42/43, PPTX 37/43 chosen actions executed | Keep misses in results; detailed causes require the original local records. No failure cause is inferred from the aggregate. |
| Wrong or missed contextual choice | Observed historical in direct-menu guide; fresh current evaluation not-run | A valid menu target can still be wrong. Preserve manual override/undo and report false actions separately. |
| Negation, quoted commands, ambiguity, injection and invented output | Regression-covered by frozen replay / agent tests | Reject invalid choice/evidence or abstain. Synthetic tests do not establish real-provider injection immunity. |
| Oversized/malformed/error provider response and SQL failure | Regression-covered by fresh `test_security_privacy.py` | Bound body consumption, fail safely, omit SQL parameters/content and error bodies from logs. |
| Manual slide change, correction, sign-out, ended session, stale source | Regression-covered in agent, saved-action and transition tests referenced by the feature guides | Invalidate pending result and require fresh eligible context. Historical/native evidence has its stated substitutions. |
| Cross-account access, expired/revoked token | Existing security regression paths; persisted rerun not-run here | Deny access; run real PostgreSQL cases/CI before claiming persisted enforcement for a new release. |
| Office unavailable, PowerPoint on macOS, Linux worker Office validation | Unsupported for native Windows/Office acceptance | Report unavailable/unsupported; do not substitute a PDF export or CI Office fake. |
| Staging provider outage, alarm notification and recovery | Not-run here; monitoring guide retains live acceptance gap | Correlate safe IDs/outcomes, keep no-action behavior, demonstrate notification and recovery in an approved environment. |
| Continuous human speech, accents/noise, full live service chain | Not-run here | Follow both-format voice runbook with willing speaker/operator; do not equate Windows SAPI audio or requester acceptance with measured accuracy. |

## Data and deck provenance

- Frozen replay v6 partitions are Buddy-authored synthetic JSON, including expected
  targets, provider replies, timings and usage. The file named holdout is a second
  contract-smoke partition with minor wording differences. It is not a blinded benchmark.
- The [manual action fixtures](../../frontend/tests/fixtures/slide_actions/README.md)
  are original Buddy-authored text/shapes and real PowerPoint-exported PNGs, approved
  for redistribution with this repository. Their manifests pin raster digests. Use them
  for the credential-free demonstration.
- Native smoke scripts generate their own PDF/PPTX and write local ignored artifacts.
  The provider comparison uses authored synthetic sources/analyses; optional SAPI speech
  is machine-generated. None is a human microphone recording.
- [Extraction fixture provenance](../../tests/fixtures/slides/README.md) lists four
  requester-authorized Drive samples and their pinned local hashes. They are **not
  licensed here for redistribution**. They are optional, excluded from the demo bundle
  and never required by CI. The checked-in controlled geometry deck is Buddy-authored.
- Human corpora, permission records, private decks, diagnostics, audio and transcripts
  remain outside public artifacts. Follow [recording retention](../evaluation-recordings.md)
  and the replay permission/deletion requirements before any future capture.

## Finalization checklist

1. Resolve #44 review and link its final versioned failure matrix/results; collect rather
   than reimplement its suite. Recheck Project status and the actual merged main revision.
2. Obtain reviewed repeated #29 results for PDF and native PPTX separately: hardware,
   network/provider configuration, denominator including failures, p50/p95 and billing.
   Otherwise retain the explicit unmeasured labels in the report and poster.
3. Link #39's approved staging notification/recovery evidence when available. Do not
   change live cloud configuration merely to finish a report.
4. Pin the final code/package revision and rerun the documented offline gates and relevant
   CI. Retain old reports with their baseline and digests; use new directories for new runs.
5. Have a teammate follow the reproduction guide from a clean checkout. Record OS/runtime,
   commands, observed controls and any failures; this independent walkthrough is pending.
6. Cross-check report, poster and demo claims against this ledger. Keep native zoom blocked
   and human/production metrics unmeasured unless new evidence specifically resolves them.
