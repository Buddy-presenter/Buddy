# Presentation latency diagnostics (#29)

Buddy records privacy-safe timings for the speech-to-action path. Desktop events are
written to `%LOCALAPPDATA%\Buddy\logs\desktop.log` on Windows (or
`$XDG_STATE_HOME/Buddy/logs/desktop.log` on Linux, falling back to
`~/.local/state/Buddy/logs/desktop.log`). The log rotates at 1 MB and keeps two
backups. These events contain decision IDs, tool names, outcome codes, and durations;
they do not include transcripts, access tokens, or presentation text.

## Reading the stages

Desktop records use the `presentation_latency` prefix:

- `proposal_received`: time from the last transcript capture end to request submission,
  request submission to response, and total capture-to-proposal time; includes the
  backend's total `elapsed_ms` for comparison.
- `action_finished`: total capture-to-execution completion, proposal-to-completion,
  server preflight time, local executor time, source format, action tool, and outcome.
- `pdf_prefetch`: whether a selected PDF destination was rendered in the background,
  discarded as stale, unavailable, or reused from cache; includes render time and the
  bounded cache size. Prefetch only renders the already selected target and never
  changes the active slide.
- `proposal_failed`: capture-to-failure and submission-to-failure durations.

The backend writes `presentation_agent_decision` JSON events to stdout. `elapsed_ms`
is the complete API decision time; `phase_timings_ms` separates session reads, admission,
quota reservation, deterministic navigation or semantic retrieval, model selection,
and final freshness validation. The event deliberately excludes transcript content.

Compare like-for-like PDF and saved PowerPoint runs. For each source, capture the
desktop `capture_to_action_ms` values and report sample count, p50, and p95; pair those
with backend `elapsed_ms` and phase timings using `decision_id`. Also report action
outcomes and prefetch hit rate. A zero-sample p95 is not evidence: collect repeated
real presentation trials under the same network and machine conditions. Local PDF
prefetch can reduce rendering after an accepted action; PowerPoint uses the native
Office path and is measured without PDF prefetch. If an action is rejected or its
context becomes stale, Buddy keeps the existing freshness and execution guards intact.

Summarize desktop and backend log files without printing their raw contents:

```powershell
python scripts/presentation_latency_report.py `
  "$env:LOCALAPPDATA\Buddy\logs\desktop.log" `
  "$env:LOCALAPPDATA\Buddy\logs\desktop.log.1" `
  path\to\backend.log
```

The report groups completed actions by PDF/PowerPoint source and reports sample count,
p50, and p95. Failed action outcomes are counted separately and excluded from completed
action percentiles. It reports proposal/backend percentiles and each backend phase too.
