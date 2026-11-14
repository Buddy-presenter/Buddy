# Presentation library (#6)

See [desktop upload progress and recovery](desktop-upload-flow.md) for upload phases,
cancellation semantics and manual testing of the PDF/PPTX controls.

The Library workspace lists the signed-in account's persisted decks. Sign in or resume
on Home, then open Library. Changing workspaces and Refresh retrieve current backend
data; search uses literal, case-insensitive title substrings. Sorting supports newest,
oldest, title A to Z and title Z to A. Results are paged in groups of 20. Search/sort
changes return to page one. Account verification preserves the current page for the
same account. An out-of-range page after data changes returns to the last available page.

Controls have keyboard focus, accessible names and tooltips. Deck titles remain plain
text; long titles are elided in the table with their full value in the tooltip and the
selected-deck details. Details include processing guidance and actual job attempts.
Active PDF jobs refresh every 15 seconds only while Library is active. Search
requests are debounced and serialized; late replies cannot overwrite newer filters,
account changes or a closed window. There are no automatic network retries.

## API contract

`GET /api/v1/decks` requires the current Cognito access token in the Authorization
Bearer header. It uses the same live verification and persisted account resolution as
other private resources. The server always filters by that verified owner, including
the total and every joined job/analysis/asset record. It does not accept an owner UUID
as authorization. Results contain only deck ID, title, creation time, the backward-compatible
coarse status, job attempts and a typed processing summary; no storage keys, summary/action
bodies, credentials or private provider errors.
Successful responses have Cache-Control: no-store and the normal request-ID envelope.

Query parameters: `search` (up to 256 characters), `sort` (`newest`, `oldest`,
`title_asc`, `title_desc`), `page` (1 through 100000), `page_size` (1 through 50;
default 20). SQL values are bound; `%`, `_` and backslash in searches are literal.
Every sort has an ID tie-breaker. Count and page rows share a repeatable-read database
snapshot. Pagination is offset-based: separate requests reflect current data, not a
frozen historical collection. Missing/invalid credentials return 401, malformed queries
422, and provider/database failures 503 using the existing sanitized error contract.
Throttling remains at the existing trusted deployment edge.

## Status semantics and feature boundaries

Status uses the latest persisted processing job by creation time and ID, not an old
successful attempt. All records are from the account's own deck:

| Stored condition | Library status |
| --- | --- |
| No job | Awaiting processing (`uploaded`) |
| Pending job | Queued (`queued`) |
| Running job | Processing (`processing`) |
| Failed job | Failed (`failed`) |
| Successful latest job with the current published analysis and complete matching render proof | Ready (`ready`) |
| Successful job missing current publication or complete render proof | Assets unavailable (`incomplete`) |

The current analysis is the highest persisted version for the deck; an older published
version cannot mask a newer candidate/partial revision. The additive `processing` object
reports `uploaded`, `queued`, `inspecting_pdf_metadata`, `analyzing`, `validating`,
`review_ready`, `partial`, `failed` or `cancelled`, plus the durable stage, real worker
progress, attempts and safe error code. `review_ready` requires the latest published
analysis to belong to the latest job for the deck and the same PDF source revision.
Publication verifies the ordered manifest, source hash/revision, complete typed summaries
and terminal action outcomes. It does not require an S3 raster row: review renders the
pinned source PDF locally with the canonical renderer. A candidate or one render asset
cannot establish readiness. The legacy coarse `status` field retains its conservative
render-proof mapping for older desktop clients.

The selected deck exposes Cancel processing only for an active, current-source job and
Retry processing only for a recoverable current-source job below the configured lifetime
attempt limit. Both call the authenticated owner-scoped job API; server quota and attempt
checks remain authoritative. A lost response is safe to recover by refreshing. The worker
persists metadata, render, provider and validation stage transitions under its active lease;
expired leases cannot overwrite newer state. Retry reuses the saved source and extraction,
and analysis/validation writes remain tied to the job and source revision. Manual PDF
presentation remains available as soon as the immutable PDF is saved, regardless of
analysis state. The review page stays read-only and explains partial or unavailable data.

No migration is needed: additive library fields read existing durable records and stage
values fit the existing bounded job column. Local tests seed disposable PostgreSQL records;
the application never injects sample decks or reads a backend database directly.

## Deck details, rename and deletion

Select a deck and choose the details arrow beside the selection actions. The details
view fetches its type, added date, latest processing stage/progress/attempts, safe error
code and analysis status from the authenticated API. Refresh details reads a new snapshot;
Back to Library reloads the list. A published analysis status indicates saved results,
not independent verification that every suggested target is visually correct. Stale
analysis is identified explicitly, including results tied to an earlier source or job.

Edit Name and choose Save name (or press Enter) to persist a new title. Names are trimmed,
limited to 256 characters, and cannot contain control characters. The saved title appears
in the library and survives subsequent loads. Processing, source bytes, active sessions
and analysis versions are not changed by a rename.

Delete presentation uses the existing permanent-deletion confirmation and asynchronous
cleanup. Cancel is the default. Confirming immediately revokes deck access and schedules
the existing retryable source/derived-data cleanup. See [private uploads](uploads.md) for
deletion and retention behavior. Account deletion remains a separate feature.

`GET /api/v1/decks/{id}` and `PATCH /api/v1/decks/{id}` (body `{"title":"New name"}`)
return a `DeckDetailsResponse` with the normal request ID and no-store caching. Both resolve
the verified owner, exclude deletion tombstones, and return 404 for foreign, missing or
deleted decks. Rename rejects unknown fields and invalid titles with 422. Current source,
latest job, latest analysis and slide count are read in a repeatable-read transaction;
storage keys and private analysis/provider content are never included. No migration is needed.

The UI uses the library's existing serialized background runner. Controls expose accessible
names, keyboard name saving, loading/error states and an explicit refresh. Account/backend
changes and sign-out clear details and discard late responses. The test suite includes
real local HTTP reads/renames, native Qt interactions, deletion cancellation, minimum-width
layout and sign-out races; PostgreSQL CI verifies owner isolation, persistence, current/stale
analysis and tombstone protection.

## Verification

Install the documented API/dev extras, then run:

```sh
python -m pytest frontend/tests/test_library.py
python scripts/test.py database
python scripts/quality.py all check
python -m mypy
python -m buddy_api.schema --check --compare-ref origin/main
```

`backend/tests/api/test_library.py` requires BUDDY_TEST_DATABASE_URL for real migrated PostgreSQL
owner isolation, literal searches, sorting, pagination, latest-job transitions, restart
reads and failure handling. Qt/HTTP tests cover bounded transport, sanitized errors,
loading/empty/failed states, keyboard controls, filtering races, account changes,
expiry, polling and window shutdown. Live Cognito/native-vault deployment acceptance
remains subject to the account guide; no real account or cloud credentials are used here.
