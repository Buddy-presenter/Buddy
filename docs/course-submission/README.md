# Buddy course submission package (#229)

**Provisional evidence package, 28 November 2026.** Runtime baseline:
`61a326e839fdfe701ad265b3f5a95c23e8aac550`. This package collects existing work;
it does not implement security, monitoring, latency optimization or an evaluation harness.

Read these in order:

1. [Technical report](report.md): problem, agentic workflow, architecture and boundaries.
2. [Evidence and failure matrix](evidence.md): results, provenance, limitations and gaps.
3. [Reproduce and run](reproduce.md): credential-free evaluation and local demo, followed
   by separately opt-in service/native checks.
4. [Demo and poster plan](demo-poster.md): a timed demonstration and evidence-led poster.
5. [Evidence manifest](results/manifest.json): exact baseline, input/report digests,
   environment and verification commands. The adjacent JSON reports are aggregate-only.

The fresh offline runs pass both 21-frame contract partitions. This is a reproducible
code-contract result, **not a current Jev accuracy, microphone latency or deployment
acceptance result**. Historical real-provider and native-player results are identified
separately, with their original source and limits. Missing measurements remain missing.

## Dependency snapshot

The [organization Project](https://github.com/orgs/Buddy-presenter/projects/2) was read
on 28 November 2026. Status differs from issue open/closed state:

| Evidence owner | Sprint / Wave | Project status at capture | Package treatment |
| --- | --- | --- | --- |
| [#26 security](https://github.com/Buddy-presenter/Buddy/issues/26) | 5 / 1 | Done; issue closed | Reference the merged threat model and regressions. |
| [#39 observability](https://github.com/Buddy-presenter/Buddy/issues/39) | 5 / 1 | Done; issue open | Describe implemented metrics; preserve the guide's pending live alarm-delivery acceptance. |
| [#44 evaluation](https://github.com/Buddy-presenter/Buddy/issues/44) | 5 / 1 | Waiting for review; issue open | Use only evidence already on the baseline; final shared evaluation remains pending review. |
| [#29 latency](https://github.com/Buddy-presenter/Buddy/issues/29) | 5 / 2 | Done; issue open | Reference timing tools and historical results; no repeated end-to-end p50/p95 is supplied here. |
| [#229 submission](https://github.com/Buddy-presenter/Buddy/issues/229) | 5 / 3 | Backlog before this work | Requester authorized provisional packaging before all evidence is final. |

These are dated observations, not live status indicators. No prerequisite ownership or
status is changed by this package. Review the finalization checklist in the
[evidence ledger](evidence.md#finalization-checklist) before calling this a final submission.
