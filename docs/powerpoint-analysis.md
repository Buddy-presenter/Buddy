# PowerPoint analysis and execution (#128)

PPTX uploads use the existing private source storage, outbox, SQS dispatcher and durable
worker. The new `pptx_metadata.v1` / `pptx_analysis.v1` stages inspect the immutable source,
generate the three saved outputs, validate action execution, and publish owner-only review
results. Source SHA-256, revision, extracted slide IDs, native Office slide IDs and ordered
slide dimensions stay bound through the complete path. No PDF conversion is involved.

## What the model receives

The PowerPoint provider receives the existing bounded OOXML extraction: slide text,
available speaker notes, object types, transformed bounds and explicit fidelity warnings.
This representation works without running Office in the analysis container and gives the
generator concrete object identities. It is deliberately distinct from the PDF provider,
which continues to receive the original PDF. The PowerPoint extraction is tied to the
accepted original's hash and revision; a mismatched original, format or manifest is rejected.
There is no alternate representation or silent fallback.

The provider cannot inspect rendered images, animation states, or arbitrary embedded
visuals through this representation. Summaries must identify unavailable image/chart/other
visual content explicitly, include supported text and notes, and give an unavailable reason
when insufficient content exists. The UI explains this limitation and can show actual
PowerPoint-exported slides after the presenter explicitly opens the saved source.

Inspector subprocess results are always UTF-8 bytes, including on Windows consoles with
legacy code pages. Accented and non-Latin slide text must survive this boundary unchanged.
Library inspection/validation labels use the deck's source format; routine status polling
keeps rows selectable and preserves the selected deck when results reorder.

## Separate catalogs and read contracts

`powerpoint-2` permits highlight, rectangle, oval, underline, arrow, callout, line,
filled rectangle, filled oval, text box, rounded rectangle, diamond, triangle, hexagon
and star actions. Generation considers the whole catalog and selects a useful, varied
subset rather than forcing decoration. Every generated action selects an extracted object ID;
the server derives the rectangle from that object's actual bounds in PowerPoint slide
points (12,700 EMU per point). Unsupported, duplicate, clipped/degenerate or unknown targets
fail explicitly. Calls cannot supply arbitrary tools or executable code. Callouts and text boxes
accept bounded text. Navigation/reset remain code-owned; manual pointer/freehand/erase/undo
controls remain presenter-owned. PowerPoint region zoom remains #111.

The separate schemas are packaged in `analysis/powerpoint_contracts/` on the server and
`powerpoint_contracts/` in the desktop wheel, with a parity test. PowerPoint's analysis
schema is `buddy.powerpoint.analysis.v2`; its source manifest contains slide points and
native slide IDs, without pretending that a PDF raster or render cache exists. Source
format is explicit in the catalog, source, analysis, action and execution report. New
database format columns and composite foreign keys prevent cross-format records.

PowerPoint reads use `GET /api/v1/analyses/powerpoint/by-deck/{deck_id}` and original bytes
use `GET /api/v1/decks/{deck_id}/powerpoint-source`. Existing PDF analysis/source endpoints
retain their PDF contracts and reject PowerPoint access. Authentication, ownership,
private evidence, bounded reads and `Cache-Control: no-store` apply to both formats.
There is no HTTP endpoint through which a desktop client can assert successful validation.
Generated text annotations find clear space beside the target using the actual Office
object/text bounds. Text wraps and fits inside a bounded box; callouts have an orange
frame and a leader pointing at their target. No clear space means an unavailable action,
not permission to cover existing text. Manual text placement remains presenter-controlled.
Technical details are collapsed per action. View proof opens the actual before/after
images or execution report, bound to the execution record that produced those artifacts.
Review does not download a PPTX while its analysis is unavailable. Original-PPTX transfers
use a 120-second client socket timeout, separately from short metadata requests. Source
authorization captures immutable pointers before releasing the database connection, so
slow storage reads do not occupy the API connection pool.

## Real Office validation boundary

The backend invokes the separately installed desktop package's
`buddy_desktop.powerpoint_validation_worker` using the existing isolated process runner.
It receives only the pinned temporary source and a bounded JSON request; database,
provider and AWS credentials are not inherited. Before invoking Office, the backend's
bounded parser verifies slide identity/dimensions and refuses external links and embedded
active material. Office automation is disabled while opening the read-only source.

On a **dedicated interactive Windows worker with desktop PowerPoint**, the process opens
its own source, runs the real #116 typed executor, verifies an added annotation and changed
Office rendering, erases it, and confirms original shape count. Private before/after images
and a report are hash-bound to the action/source. The report records the actual Office
version/build and declares that execution evidence is not semantic verification.

The process owns one COM apartment for its complete lifetime. It refuses to borrow an
existing PowerPoint session, closes its own source without saving, and records the exact
new Office process for bounded timeout cleanup. Use an isolated Windows user/session for
this worker, not a presenter's simultaneously active Office session. Parser/validator
timeouts, memory/scratch limits, cancellation, private evidence reservations and cleanup
remain enforced by the existing runner/service.

**Deployment limitation:** the existing AWS worker image is Linux and cannot run desktop
PowerPoint. It records `needs-review` with `powerpoint_runtime_requires_windows_office`;
it never promotes those actions to usable. A reviewed dedicated Windows/Office worker
deployment and its queue/identity/network configuration are required for staging to produce
usable PowerPoint actions. Do not add a competing Windows consumer to the same live queue
and assume PPTX jobs will reach it. Plan explicit routing or move the worker's consumption
to the reviewed Windows runtime before accepting live PowerPoint execution. This change
does not provision a Windows VM or claim that an Office runtime is deployed in AWS.

## Presenter workflow and privacy

1. Upload a `.pptx` in Library. Its processing states follow inspecting, analyzing,
   validating, review-ready/partial or failed. Existing cancel/retry semantics remain.
2. Open **Review** to read saved summaries, action purposes, statuses and evidence.
3. On Windows, **Open saved PowerPoint** opens the exact downloaded, verified source
   read-only and shows a real static Office preview. Review navigation follows the saved
   native slide IDs and dimensions. Sources containing external/active material cannot be
   opened through this review integration.
4. **Run selected action in PowerPoint** is enabled only for a validated action after
   opening that source. The raw action hash, source hash, native slide, geometry, window and
   recorded Office build are checked again. A different Office build requires revalidation.
   No action runs automatically when reading or selecting a suggestion.

Private sources, previews and results clear on sign-out, account/backend/deck change and
shutdown. A separate cleanup runner closes only the downloaded private presentation and
removes its temporary source after current work drains; unrelated PowerPoint shows remain
outside that session. Local manual PowerPoint controls and the PDF player are preserved.

## Limits, migration and verification

- PPTX parser: existing 64 MiB source, 256 MiB archive expansion and XML/object/time/memory
  limits. Analysis: at most 100 slides and 120,000 UTF-8 bytes of extracted context; no
  silent truncation. Provider: one 90-second invocation per generation attempt, 8,000 output
  tokens, 2 MiB response, 40 generated actions per deck. Existing lifetime attempts,
  per-owner concurrency/quota and whole-job deadlines also apply. These bound requests and
  token consumption; account budgets remain alerts rather than a hard monetary cap.
- Apply forward migrations `0012_powerpoint_analysis` and `0013_powerpoint_tools` with the
  migration identity before deploying compatible binaries. Existing PDF action/input hashes are preserved. Historical
  untyped analyses remain legacy; legacy writes get their deck's format through a narrow
  legacy-only trigger. Typed cross-format data is never silently reinterpreted.
- Run `python scripts/quality.py all check`, the frontend/backend/database/integration tests,
  OpenAPI compatibility, both package builds and the desktop smoke.
- Opt-in real Office acceptance: `python -m tests.integration.smoke_powerpoint_analysis`.
  It uses synthetic permissioned 16:9 and 4:3 sources and exercises every generated action
  through the actual parser/process/executor, recording evidence under `build/issue128/`.
  No personal presentations are used or saved. Provider transport tests use deterministic
  responses only at the external model boundary; a live Bedrock run still needs a current
  AWS session and the approved model permission.

## Acceptance evidence

2026-10-28: the expanded v2 catalog passed all **30 real Office cases**: 15 tools on
16:9 and 4:3 slides. Callouts/text boxes used clear-space placement and fitted text;
rounded rectangles, diamonds, triangles, hexagons and stars produced real Office shapes.
The catalog and executor/profile versions were advanced together; regenerate any older
unreleased test analyses to use these actions. PDF catalogs are unchanged.

The expanded workflow also passed with real staging S3/Bedrock and isolated local
HTTP/PostgreSQL/Windows Office/Qt services. Six generated actions (oval, underline,
callout, rounded rectangle, arrow and text box) executed through Review. The actual
evidence button opened a saved execution image. Sign-out closed the private source;
all 19 synthetic test S3 objects/versions and the isolated schema were removed.

Earlier baseline verification (2026-10-26):

The opt-in Office matrix passed all 12 cases: the six catalog actions on 960x540
(16:9) and 720x540 (4:3) slides, using desktop PowerPoint 16.0 build 20527. Every case
recorded a real before/after render, source/action hashes, successful annotation execution,
and restoration of the original shape count. Sources were synthetic permissioned decks.

The complete workflow also passed using real staging S3 and Bedrock, with an isolated
local API, PostgreSQL schema, Windows Office validator and Qt desktop. The two-slide
source was uploaded through the real desktop service and HTTP API. The durable processor
saved its deck summary, two ordered slide summaries, and four usable actions. In the real
Review UI, underline, callout, highlight and rectangle each executed in PowerPoint.
The summaries correctly reported the source's 12% revenue growth, proposed pilot expansion,
and speaker notes, and explicitly identified unavailable background artwork. Sign-out
closed the private review source. All 13 test S3 objects/versions and the isolated database
schema were removed afterward. No personal presentation or shared queue message was used.
An additional real Office recovery check ended the external slideshow, reopened the same
verified private source through Review, and executed a saved validated action successfully.

Reproduce that acceptance with a current approved AWS profile and an isolated local
database using `python -m tests.integration.smoke_powerpoint_workflow --live-aws
--bucket <approved-staging-bucket> --region <region>`. This explicitly invokes a paid
provider; ordinary tests use deterministic responses only at external boundaries.
Keep receipts, render comparisons and screenshots together under ignored `build/issue128/`.

This verifies live storage/provider integration and real local Office execution; it does
not establish a deployment of this branch to the shared staging API/worker. The Linux
worker limitation above remains an explicit rollout blocker for usable staging PPTX
actions until the dedicated Windows runtime and routing have been deployed and reviewed.
