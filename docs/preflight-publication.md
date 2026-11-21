# Action checks and live action gating (#53)

A generated slide action reaches a live presentation only after it has been
**executed in Buddy's real player and visually verified** by an independent vision
model (Claude on Amazon Bedrock). Each action is executed **once**; the visual check
judges the screenshots from that single run. The vision verdict is always made by
the API server. The desktop never decides an outcome and never holds provider
credentials.

## Who checks what

| | PDF | PowerPoint |
| --- | --- | --- |
| Executes the action | Processing worker, headless PDFium player (one run per action) | The owner's Buddy, real desktop PowerPoint |
| Takes the screenshots | Same run (saved as execution evidence) | Same run, real fullscreen slideshow |
| Judges them | API server visual QA (#135) reusing those screenshots | API server, per attempt |
| Starts | Automatically after processing | Owner clicks **Check on this PC** in Review |
| Runs on staging (Linux) | Yes | Yes (the server only judges) |

PDF: processing executes each action and stores before/after screenshots. The
server visual QA (`buddy_api.validation.visual_qa`) then judges **those**
screenshots; it only executes the action itself if they are unavailable (for
example after evidence retention). The API's background worker schedules these
checks automatically for new analysis revisions.

PowerPoint: processing does not open Office. After processing, Review shows
"N PowerPoint actions need a quick check on this PC". **Check on this PC** runs each
action once in an isolated desktop PowerPoint session (`buddy_desktop.desktop_capture`,
`preflight_operator`, `preflight_worker`) and uploads each before/after screenshot
pair to the API, which calls the vision model and records the verdict. Bounded
corrections (#51) are tried automatically (up to three attempts). The final report
is accepted only if every attempt, screenshot hash and outcome matches the verdicts
the server issued.

While PowerPoint checks run, a full-window notice asks the owner not to use the
mouse or keyboard (input can move the pointer into the slide or end the show and
spoil a screenshot) and offers **Stop after this action**.

## What is usable live

An action is served to live sessions (candidates, agent tools and accepted session
actions) only when its latest validation is `validated` **and** `semantic_verified`,
bound to the exact analysis, action digest, source and renderer.

- PDF evidence holds on any presenter display: every PDF is drawn by the same pinned
  PDFium renderer in source coordinates.
- PowerPoint evidence must come from the same OS and display scale as the presenter
  (`wide-1x` at 100% Windows scaling, `wide-2x` at 200%). Present sends its runtime
  when it starts a session; changing it resets focus to the full slide.

Anything else (unchecked, rejected, needs-review, stale or other-scale results) is
`usable: false`. Present always keeps the full slide, manual navigation and
annotations available. Pending checks never make a deck "Partial results"; that
label only means a summary or analysis is missing.

## Owner report and progress

- **Library**: PDF shows a progress ring and `Checking · 3/6` while the server works,
  then `Ready · 6/6 actions`. PowerPoint shows `Needs check · 0/24` until the owner
  checks on their PC. A server that cannot check a format shows `Ready · actions off`.
  The API adds `processing.checks` (`total`, `checked`, `ready`, `available`,
  `active`, `mode`: `server` or `desktop`).
- **Review**: a progress bar and `x of y checked · z Ready`; PDF refreshes itself
  while server checks run. Each action shows Ready, Needs check on this PC, Waiting
  for PowerPoint or Unavailable, with its target, correction state, verified
  runtime/Office version, reasons and private before/after evidence
  (**Action details & evidence**). **Check again** appears only for a finished,
  failed check.

Reports and evidence are private to the deck owner and deleted with the deck.

## API

Owner-authenticated, `Cache-Control: no-store`; other owners get 404.

| Endpoint | Behavior |
| --- | --- |
| `POST /api/v1/analyses/{analysis_id}/preflights` | Queue a check for one saved `instance_id` (one `Idempotency-Key`). PDF: checked by the server. PowerPoint: waits for the owner's desktop. |
| `GET /api/v1/preflights/{id}` | Phase, attempt, state, reasons, `corrected`, evidence references. |
| `POST /api/v1/preflights/{id}/claim` | Desktop starts a PowerPoint check; returns the bound snapshot (source hash, slide map, action, profile, viewport). 10-minute lease. |
| `POST /api/v1/preflights/{id}/judge` | One attempt's before/after PNGs (base64, ≤3.75 MB each); the server returns and records the verdict. Attempts must arrive in order (max 3). A confident-enough (≥0.8) approval is required. |
| `POST /api/v1/preflights/{id}/result` | Final report and screenshots; rejected with 409 unless they match the recorded verdicts. |
| `POST /api/v1/preflights/{id}/fail` | Desktop could not capture; known reasons are kept, others become `desktop_check_failed`. |
| `GET /api/v1/preflights/{id}/evidence/{artifact}` | Report or a before/after PNG, hash-verified. |

Results are pinned to the analysis revision and action digest, so a new upload,
analysis or owner correction makes old results stop counting. A check whose
analysis changes while it runs is published as `stale`. Expired leases fail as
`preflight_interrupted`.

## Configuration

No extra infrastructure is needed. The API's existing worker region and Bedrock
permission (`api_bedrock_visual_qa`) judge PowerPoint screenshots; the server visual
QA needs the configured action validator (`BUDDY_VALIDATOR_PYTHON`) for PDFs.
`BUDDY_PREFLIGHT_*` settings remain only for an optional trusted desktop host that
runs PDF checks in the real desktop player instead of the server.

Apply migration `0024_preflight_publication` before deploying (additive; the
previous release keeps working against it).

## Testing

Automated: `python scripts/test.py api` and `python scripts/test.py desktop` (set
`BUDDY_TEST_DATABASE_URL` for the database-backed tests). They use scripted
verdicts and never call Bedrock.

Manual, on Windows with desktop PowerPoint: upload a PDF and a PPTX, wait for
processing, and confirm the PDF turns Ready on its own while the PPTX shows
"Needs check". Open the PPTX in Review, choose **Check on this PC**, leave the mouse
and keyboard alone until the notice closes, then confirm verified actions show Ready
with before/after evidence and Present offers only Ready actions.
