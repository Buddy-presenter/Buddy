# Stored presentation analysis v1 (#11)

The durable PDF worker submits the checksum-verified original PDF to Amazon Bedrock's
native PDF-capable InvokeModel API using Claude Sonnet 4.6. It persists exactly three
outputs: one presentation summary, one ordered content summary per page, and zero or more
parameterized content actions. Actions are limited to `zoom_region` and
`highlight_region`; navigation and reset controls come from code. Existing PPTX upload
and storage remain compatible, while PPTX analysis and playback are deferred.

## Records and version isolation

| Table | Purpose |
| --- | --- |
| `analysis_versions` | Existing stable ID, owner/deck/version/job/extraction, summary text/state/reason, input-version provenance/hash and publication state. |
| `analysis_slides` | Every slide once in extraction/manifest order, with summary text/state/reason. |
| `slide_actions` | Stable instance ID, position, catalog version/type, purpose, JSONB parameters and immutable action hash. |
| `action_validations` | Append-only candidate/validated/rejected/needs-review history and versioned JSONB evidence. |
| `legacy_analysis_payloads` | Compatibility archive of old untyped payloads (including legacy writes after rollback); never current results or returned to clients. |

Composite foreign keys preserve owner/deck/analysis consistency. Repository callers must
derive owner scope from verified identity or an authorized backend job. The source
revision/hash, extractor/schema version, slide IDs/order and aspect ratios must match an
existing owned extraction. The manifest pins the render, dimensions and image digests.
PDFium computes these page-raster hashes in memory to bind coordinate provenance; Buddy
does not save those pages as previews or send rasterized pages to the provider. The
provider receives the original PDF as a native Claude PDF document block.
Renderer, validator and display-profile identifiers describe the intended validation
environment and must change when its behavior changes.

`Analyses.create(owner_id=..., deck_id=..., version=..., document=AnalysisInput(...))`
is a trusted internal boundary, not a public write endpoint. It revalidates even unchecked
Pydantic constructions/copies. Creation locks the deck, accepts identical retries of the
same version and rejects conflicting content. Reruns create a new version/ID. SQL triggers
reject changes to existing content and validation records; only derived publication can
change on the parent. Deletion is also blocked until a separately reviewed retention
workflow explicitly handles these immutable records. Sessions keep their original analysis foreign key.

Limits: 500 slides, 100 actions per slide, 1,000 actions total, 8 MiB serialized analysis,
16,000 characters per summary. Empty action lists are valid. Provider generation and isolated
execution use a stricter 40-action deck budget so one analysis attempt stays bounded by the
worker's runtime. A slide may receive multiple distinct actions, including both zoom and
highlight suggestions. Using all 40 generated-action slots is surfaced in Review as a capacity
notice. It reports the slot count only and does not imply that actions were omitted or that the
analysis is semantically complete.

## Summaries, publication and readiness

Each summary has `status`, `text` and `reason`. Available requires nonblank text and no
failure reason. Pending/unavailable have no text; unavailable requires a bounded reason.
Never invent content for unreadable slides.

| Publication | Meaning |
| --- | --- |
| `legacy` | Untyped historical data requiring reprocessing; no usable analysis exposed. |
| `partial` | At least one presentation/slide summary is pending or unavailable. |
| `awaiting_validation` | All summaries available, but at least one action is a candidate. |
| `published` | All summaries available, with terminal validation decisions for all actions, or no actions. |

Published means ready for review, not that every action succeeded. Rejected and
needs-review actions remain inspectable with reasons, but are never usable. The library
selects the highest analysis version for the deck, then requires that revision to be
published and belong to the latest completed analysis job. It also requires complete
render evidence matching every ordered manifest slide, render revision and raster checksum.
Current RenderedAsset rows cannot express that evidence, so successful decks remain
`incomplete` until a renderer persists the proof. Asset presence/count or old published
versions cannot imply completion. A successful analysis job can therefore appear as
`incomplete` in the library until the separate renderer-persistence work exists.
Historical analyses remain readable by ID.

## Catalog and generation contract

`buddy_api.analysis.catalog.CATALOG` classifies the existing v1.1 catalog:

| Category | Types | Producer |
| --- | --- | --- |
| `slide_content` | `zoom_region`, `highlight_region` | Model proposes region/purpose; stored per slide. |
| `general_control` | `next_slide`, `previous_slide`, `go_to_slide`, `reset_view` | Code; never generated/stored per slide. |

`generation_schema()` returns the exact content-only subset of the completed catalog.
Backend-packaged standalone schemas are byte-identical to the executor's schemas;
integration tests compare them and parse saved instances through the real contract parser.
Backend runtime never imports frontend Python or Qt. Catalog changes must update both
standalone copies deliberately and pass the parity test. Generation can return several
distinct actions for a slide within the deck budget. Before an action is stored, issue #133
resolves exact text targets to PDFium glyph boxes (or confidence-bounded OCR word boxes for
scanned pages) and snaps visual zoom targets to PDF object geometry. When a proposed zoom is
inside a flattened image, Buddy preserves the crop as a non-usable candidate for the independent
visual QA/correction path; the execution validator is skipped and cannot promote it. This
server-owned handoff is persisted with the immutable action record, without changing the public
action catalog. The original PDF remains the provider input. Coordinates returned by the model
are only hints; ambiguous or unresolved targets are omitted. The isolated validator applies a 6%-of-page-long-side maximum to the
short axis of model-generated line highlights, so narrow vertical text is handled like a
horizontal line. Broader regions are stored as rejected suggestions with evidence. Source
matching improves geometric grounding, but it still does not prove that the model chose the
right semantic point; validation remains explicitly non-semantic and requires human review.

`general_controls(ordered_slide_ids, current_slide_id)` enables next except on the last
slide, previous except on the first, supplies ordered valid jump targets, and always
enables reset. The analysis worker does not generate or persist these controls. Existing
executor boundary no-ops remain unchanged.

Stored instances retain catalog `version`, `instance_id`, `purpose`, deck/render/slide
references, `type` and filled `params`. Unknown/extra fields, general controls, duplicate
IDs, mismatched references, nonfinite/boolean geometry, out-of-bounds regions and excessive
zoom reject. Extraction uses EMU; action rectangles use canonical source pixels. Their
aspect ratios must agree; values are not silently interpreted in the other plane.

## Validation evidence and typed reads

New source-grounded analyses retain an optional `inputs.grounding` list in the existing
immutable provenance JSON. Each record binds an action ID and hash to the exact quoted
text (when applicable), the original model rectangle, final source rectangle, method
(`pdf_glyphs`, `pdf_words`, `ocr_words`, `pdf_visual_geometry`, or `visual_hint`) and
grounding version. The enclosing analysis pins the source hash/revision and renderer.
Changed hashes or final geometry reject; unresolved visual hints must remain QA-pending.
The provider cannot submit this server-owned metadata. The normal catalog is unchanged.

This metadata is private document content, available only through the existing owned
analysis read. It must not enter logs. Empty metadata is omitted from serialization so
historical immutable analysis hashes remain valid. Older analyses have no trace, which
must not be interpreted as evidence that source grounding ran. No database migration is
needed: the existing provenance JSON stores the new versioned information.

Compatibility direction matters: new readers accept historical records, but older strict
readers do not understand non-empty grounding metadata or its changed analysis digest.
Before deploying a worker that publishes traces, prove the rollback API can read a new
traced analysis, or provide a reviewed compatible rollback reader. Passing an empty-trace
fixture is insufficient evidence for that rollout. Do not rewrite historical digests.

Text matching requires whole words, including normalized Unicode matching, so a target
such as `net results` cannot be grounded to the suffix of `internet results`. Grounding
still locates the supplied quote; it does not prove the purpose agrees with that quote.
Candidate-based target selection, multi-line highlight contracts and independent visual
QA/correction remain separate outstanding work in #135/#50/#51.

`ValidationInput` (`buddy.validation.v1`) binds a unique validation ID to analysis ID/hash,
action instance ID/hash and validator/renderer/display-profile versions. Evidence carries
opaque artifact UUIDs, kind and SHA-256, never storage keys. The worker calls the existing
isolated #49 validation service, which stores owner-scoped report/screenshots and returns
the references bound to the exact source, action and profile. A reference alone is not
proof of semantic correctness; the validator explicitly records that semantic grounding
is not verified. Execution validation therefore remains non-usable until an independent
visual review is recorded. A visual review may set `validation_kind=visual` and
`semantic_verified=true` only after inspecting the real audience viewport; only that
validated combination is usable. Existing execution-only records default safely to
non-semantic verification.

The PDF [action validator](action-validation.md) produces owner-scoped standalone
execution reports and private evidence. A standalone API request does not automatically
append analysis-action validation history or publish an analysis. The trusted worker
explicitly binds each matching report's source/action/renderer/profile to this contract.

Only trusted `record_validation(owner_id=..., result=...)` appends results. Validated needs
evidence and no failure reasons; rejected/needs-review needs reasons. Validation IDs retry
idempotently only with identical content. Stale hashes/version/profile reject. History
is bounded to 100 attempts after the candidate; create a new revision for more attempts.
The latest result determines usability. Changed source/render/catalog/profile requires
a new analysis revision with fresh evidence.

`GET /api/v1/analyses/{id}` returns a typed request-ID envelope containing metadata,
publication, original typed inputs and ordered slide results. Each action result has
state, evidence and `usable`. Consumers must check `usable` and matching version/profile,
never infer eligibility from action presence in original inputs. No HTTP write endpoint
lets a client assert validation. `/resources/analyses/{id}` is now metadata only.
Reads use a consistent snapshot and no-store headers. Missing identity is 401, wrong
owner 403, missing record 404 and unavailable/invalid stored contracts a redacted 503.

[Analysis](fixtures/analysis-v1.json) and [validation](fixtures/validation-v1.json) fixtures
are synthetic contract examples, not generated/visually validated production evidence.
Published machine-readable schemas are `analysis-v1.schema.json`,
`validation-v1.schema.json` and `generated-content-action-v1.1.schema.json` in this directory.

## Migration and checks

### Diagnose a misplaced text rectangle

Before changing generation or coordinates, compare the saved action with current source
grounding. From the repository root, run the local diagnostic with the exact source PDF,
one-based page number, quoted target, and optional saved source-pixel rectangle:

```powershell
.\.venv\Scripts\python.exe backend/scripts/diagnose_pdf_region.py deck.pdf `
  --page 2 --target "Exact visible phrase" `
  --saved-region 100 200 400 30 --source-sha256 <saved-source-sha256> `
  --output build/private-region-check
```

The directory must be new. It contains a private JSON report and a full-resolution page
overlay: orange is the saved region and green is current grounding. The report includes
source identity, renderer version, canonical dimensions and intersection-over-union.
A mismatched supplied source digest refuses comparison. Without a digest, source identity
is explicitly unverified. Missing or ambiguous targets have no grounded rectangle.
The command reuses the production bounded grounding subprocess, makes no provider calls,
and changes no persisted analysis. Keep these artifacts out of Git and shared logs.

Compare the saved rectangle, this overlay and the Review viewport. Correct source geometry
with an incorrect stored box points toward old generation/grounding or different deployed
worker code. Correct stored geometry with an incorrect overlay in Review points toward the
client mapping/selection path. A supplied quote is an investigator's target: it does not
prove that the original model used that quote, nor does a matching box prove semantic intent.

The source-grounding version in code is not evidence that staging runs it. Confirm the
worker image/commit and analysis timestamp before making a fresh provider request.

Apply the analysis migration and the following worker-state migration explicitly with the
migration identity. They archive each old
`graph` payload, marks its parent legacy/unavailable, retains all IDs/version/session
references and drops the active column. Stop old backend binaries before migration and
deploy compatible binaries before serving again. The transactional migration is
forward-only: restore a verified backup or apply a reviewed forward repair. Runtime
never applies DDL. Deployment owners must provision necessary new-table grants; no live
AWS infrastructure or database is changed by this issue's local verification.

Run API, disposable PostgreSQL and integration tests using `scripts/test.py`, followed by
repository quality checks, OpenAPI compatibility and wheel builds. Tests cover restart,
legacy migration/session preservation, immutable versions, retries/conflicts, authorization,
stale evidence, publication and compatibility with the completed catalog.
