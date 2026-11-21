# Preflight publication and live action gating (#53)

Generated slide actions reach a live presentation only after a trusted preflight
has executed them in Buddy's real player and an independent visual check has
confirmed the target. This guide covers the owner-facing report, the gate on
live sessions, configuration and operation. The isolated runner itself is
described in [isolated preflight](preflight-runner.md); the vision verdict in
[visual QA](visual-qa.md); bounded corrections in #51.

## What is usable live

An action is served to a live session (candidates, agent tools and accepted
session actions) only when all of the following hold:

- Its latest validation is `validated` **and** `semantic_verified` (visual QA
  passed). Execution-only validation is never enough, for PDF or PowerPoint.
- The validation is bound to the exact analysis revision, action digest,
  source revision/hash and renderer version (`check_validation_evidence`).
- The validation's runtime matches the session's runtime context: OS platform
  (`win32`/`darwin`), renderer (`pdfium-5.14.0-144dpi` or `desktop-powerpoint-2`)
  and display profile (`wide-1x`/`wide-2x`).

Everything else (candidate, pending, needs-review, rejected, unavailable, stale
or other-runtime results) is returned with `usable: false`. Present keeps the
full slide, manual navigation and annotations available; an unavailable focus
action never blocks presenting. A deck whose analysis is `partial` or
`awaiting_validation` can still be presented in full-slide/manual mode.

The desktop sends its runtime when it starts a session
(`POST /api/v1/sessions` with `runtime`). Changing runtime on an existing
session resets its focus to the full slide and clears the last accepted action.
A client that sends no runtime gets no focus actions.

## Corrected actions

If visual QA rejects an action and bounded correction is enabled, the corrected
geometry is revalidated. A correction may only change the region; type,
purpose, slide, deck and render ID must match the original. The stored analysis
digest is unchanged: the verified `executed_action` in the validation evidence
supplies the geometry that live sessions execute.

## Owner validation report

**Library → Review** shows, for each action: status (Ready, Visual QA pending,
Pending checks, Unavailable), intended target region, correction state,
verified runtime/Office version, failure reasons and the private before/after
viewport evidence (**Action details & evidence**). **Run visual QA** and
**Run visual QA on all actions** queue trusted preflight requests and show the
live phase (`queued`, `running`, `complete`, `failed`, `stale`), attempt
(up to 3) and validated/rejected/unavailable/failed counts. Reports and
evidence are private to the deck owner and are deleted with the deck.

API (owner-authenticated, `Cache-Control: no-store`):

| Endpoint | Behavior |
| --- | --- |
| `POST /api/v1/analyses/{analysis_id}/preflights` | Queue a check for one saved `instance_id`. Requires one `Idempotency-Key`. Clients choose profile, viewport, platform and whether to correct; they can never submit a verdict. 409 if the analysis is no longer current; 429 when the owner's queue is full. |
| `GET /api/v1/preflights/{id}` | Phase, attempt, state, reasons, `corrected` and evidence references. |
| `GET /api/v1/preflights/{id}/evidence/{artifact}` | The JSON report or a before/after PNG, hash-verified, as an attachment. |

Other owners and deleted decks get 404.

## Invalidation and scheduling

Results are pinned to the analysis revision, action digest, source hash,
renderer and profile. A new source upload, a new analysis/graph revision or an
owner correction produces a new analysis version, so earlier results stop
matching and the action is unusable until it is checked again. On a trusted
host the preflight worker automatically queues checks (up to 8 per pass) for
actions in the latest ready analysis that have no check yet for this host's
platform and profile. Completed failures are not retried automatically; the
owner retries from Review. A check whose analysis changes while it runs is
published as `stale` and never validates the new revision.

Phase and attempt are persisted, so progress survives restarts. A running check
whose lease expires (about 8 minutes) is marked `failed` with
`preflight_interrupted` and its partial evidence is removed.

## Configuration (trusted host only)

Preflight needs an interactive Windows or macOS desktop (PowerPoint requires
Windows with desktop Office). The Linux staging/production API keeps it
disabled; requests there complete as `unavailable` with
`trusted_preflight_runtime_unavailable`, and no action becomes usable.

| Variable | Requirement/default |
| --- | --- |
| `BUDDY_PREFLIGHT_ENABLED` | `true` or `false` (default). |
| `BUDDY_PREFLIGHT_PYTHON` | Required when enabled: absolute path to the Python that has `buddy_desktop` installed. The child gets no network, AWS, database or Buddy credentials. |
| `BUDDY_PREFLIGHT_PROFILE` | `wide-1x` (default) or `wide-2x`; must match the presenter's display scale. |
| `BUDDY_PREFLIGHT_WIDTH` / `BUDDY_PREFLIGHT_HEIGHT` | Viewport, default `1280`×`720`; 320–4096. |
| `BUDDY_AWS_REGION` | Bedrock region for the visual judge, default `eu-central-1`. AWS credentials come from the host's normal AWS configuration. |

Apply migration `0024_preflight_publication` before enabling. It only adds
nullable/defaulted columns, so the previous release keeps working against it.

## Testing

Automated: `python scripts/test.py api` and `python scripts/test.py desktop`.
Set `BUDDY_TEST_DATABASE_URL` for the database-backed preflight, gating and
session tests. They use a scripted judge and never call Bedrock.

Manual end to end on Windows: run the local API with preflight enabled
(see [development](development.md)), upload a PDF and a PPTX, wait for
processing, then in Review run visual QA on all actions. Confirm validated
actions show **Ready** with before/after evidence, rejected ones stay
unavailable with a reason, and Present offers only Ready actions while manual
navigation still works.
