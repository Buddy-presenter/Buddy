# Presentation latency diagnostics (#29)

## First latency experiment: short explicit commands

Follow-up routing checks found two additional avoidable deferrals: the local parser
rejected the curly apostrophe in "Let’s" before expanding the contraction, and the
interim model-attempt/unchanged-length limits ran before local resolution. Contractions
are now expanded before the quotation guard, and those model limits apply only after
local resolution has had a chance to produce a command. Quoted, conditional, negated,
unstable and stale input remain subject to their existing guards. These code paths
can explain missed fast paths, but private transcripts are not logged, so they are
not asserted to explain every recorded remote navigation request.

The API now sends its bounded phase durations in `Server-Timing` response headers.
Updated desktops record these as `backend_phase` events, joined to the decision in the
local viewer. Older servers simply omit them; upgrading only the desktop does not expose
backend phases. Authentication and ingress happen outside the route's phase timings.
The desktop request-minus-server duration therefore cannot be labelled pure network time.
The current urllib transport opens a fresh HTTP connection per decision; connection
reuse is a separate follow-up experiment. Policy rejection diagnostics (duplicate,
busy, stale, etc.) are also displayed by the viewer.

The desktop's 12-character minimum for interim model requests previously ran before
local navigation resolution. This delayed complete stable commands such as "next slide"
until more words or a final transcript arrived. Local resolution now runs before that
minimum-length check. The remote model still requires 12 characters for interim input;
stable-prefix, freshness, incomplete-number, duplicate-revision and execution guards remain.

Regression tests reproduce the blocked interim proposal before the change and verify
immediate local proposals afterward. They do not establish a real microphone speedup.
At the start of this experiment the local viewer contained zero diagnostic samples,
so no measured baseline or latency improvement is claimed.

For live comparison, use the same saved deck, machine, microphone, network and backend.
Restart the desktop from this checkout to load the change, enable live suggestions and
automatic transitions, then record ten next/previous commands using fresh speech for
each. Keep PDF and PowerPoint runs separate. Compare local-navigation capture-to-proposal
and completed-action capture-to-action p50/p95 against an equivalent unmodified run.
Count missed, incorrect and duplicate actions as well as successful moves. Test negation,
incomplete slide-number commands, manual overrides and Undo too. If local navigation is
fast but topic commands are slow, inspect retrieval and model selection phases next.

## Local observability viewer (no account required)

From the repository root, run:

```powershell
python scripts/buddy_log_viewer.py
```

Open http://127.0.0.1:8765. The browser opens automatically unless `--no-browser`
is supplied. The viewer watches the desktop rotating log and refreshes every two
seconds. Select a decision to see its desktop and backend stages, event fields,
outcome and correlation IDs. Search and filters help isolate failures and unlinked
diagnostics. Summary cards show completed-action sample count, median and p95.
Unrelated speech/worker events remain separate when no shared identifier exists.

To watch API/worker files too, supply them as positional arguments (missing files
are retried on refresh):

```powershell
python scripts/buddy_log_viewer.py path\to\api.log path\to\worker.log
```

For a local API, redirect its stdout/stderr to a file in a separate terminal, then
watch that file. The viewer does not start or reconfigure your API:

```powershell
python -m buddy_api *> ..\tmp\buddy-api.log
```

Use **Import logs** for snapshots of JSONL/plain logs or a CloudWatch CLI JSON export.
Imports stay in memory until viewer shutdown, and importing the same filename replaces
that snapshot. Watched files retain the latest 2 MiB each; imports retain up to 10,000
supported events per file. Duplicate diagnostic records are collapsed. Backend decisions
and desktop executions join by `decision_id`; API records can also join by `request_id`.
Bars compare recorded durations, not a reconstructed historical timeline. Client intervals
may include server stages. Desktop timestamps from different sources need not be synchronized.

Optional live CloudWatch polling requires boto3 (included in the backend API extra) and
an existing AWS login with `logs:FilterLogEvents` access to the selected groups:

```powershell
python scripts/buddy_log_viewer.py --aws-profile YOUR_PROFILE --aws-region eu-central-1 `
  --cloudwatch /buddy/staging/api --cloudwatch /buddy/staging/worker
```

Only explicitly selected groups are read, starting with the preceding hour and polling
every ten seconds. Up to 10,000 cloud diagnostics are retained. No credentials are entered
into the web UI. An expired/unavailable login is shown in source status. Existing records
stay visible. Cloud access has not been established merely by starting the local viewer.

The server binds only to loopback, checks Host/Origin and never serves arbitrary file paths.
Only allowlisted diagnostic fields are returned to the browser; raw log lines, document
content, transcripts and credentials are omitted. Unsupported/general application messages
are not displayed. Stop the viewer with Ctrl+C in its terminal.

## Optional Langfuse dashboard

The API supports opt-in Langfuse Cloud tracing (Python SDK 5.2.0). Install the optional
backend extra using the virtual environment running your API:

```powershell
python -m pip install -e ".\backend[api,tracing]"
$env:BUDDY_LANGFUSE_ENABLED = 'true'
$env:LANGFUSE_BASE_URL = 'https://cloud.langfuse.com'
# Load LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY securely into this API process.
$env:BUDDY_ENVIRONMENT = 'development'
python -m buddy_api
```

Create a project at https://cloud.langfuse.com and obtain its project API keys in the
project settings. Do not commit keys or place the secret in desktop settings. The
base URL must match the region of that project. SDK configuration is read at API startup;
restart the API after changes. Self-hosted installations need a server compatible with
Python SDK v5. See the [SDK compatibility guide](https://langfuse.com/docs/observability/sdk/overview).

Each backend decision produces a `presentation.decision` trace, with child observations
for the phases actually visited: session, admission, quota reservation, retrieval,
selection (a Cohere generation), navigation and validation. Retrieval includes query
embedding and candidate ranking; those are not yet separate spans. Trace IDs equal the
decision UUID with hyphens removed, allowing direct correlation with existing logs.
Output contains only the selected tool, bounded reason and decision source. There are
no captured prompts, transcript text, summaries, document bytes, tokens or raw exceptions.
Token usage/cost is not yet collected. Export runs through the SDK's background exporter;
requests do not flush or wait for ingestion. Shutdown drains the exporter off the API
event loop. Missing SDK/configuration or tracing failures leave presentation behavior intact.

Desktop execution remains local and does not require a Langfuse secret. After a test run,
use an operator terminal with the tracing extra, enable flag and project credentials to
append allowlisted execution timing observations:

```powershell
python -m buddy_api.trace_execution "$env:LOCALAPPDATA\Buddy\logs\desktop.log"
```

The importer reads only `action_finished` records and attaches `desktop.execution_timing`
to the matching trace ID. Metadata contains PDF/PPTX format, tool, completion flag and
capture-to-action, proposal-to-action, preflight and executor durations. These imported
durations are metadata, not a reconstructed historical waterfall: the observation itself
is timestamped at import. Local navigation has no backend decision span, so an execution
import can create its own trace. Deduplication covers the files in one invocation only;
reimporting the same log in another invocation can add duplicate observations. Import
only the relevant test log once. SDK queueing does not prove delivery; verify the trace
in the dashboard. Existing logs and percentile reports remain available when tracing is off.

Running the desktop against staging will only produce backend dashboard traces after
the matching backend code, optional dependency and secret configuration are deployed.
This change does not deploy services or provision a Langfuse project.

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
- `local_navigation`: explicit next/previous/numbered commands resolved from the locally
  loaded presentation, including capture-to-proposal time. These commands bypass Cohere
  and the network request used for contextual suggestions.
- `preflight_skipped`: the proposal already represents validated slide navigation, so the
  additional transition-check request was skipped. Its `action_finished preflight_ms`
  value is zero; freshness, source, and execution guards still run locally.
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
