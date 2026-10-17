# Presentation library (#6)

The Library workspace lists the signed-in account's persisted decks. Sign in or resume
on Home, then open Library. Changing workspaces and Refresh retrieve current backend
data; search uses literal, case-insensitive title substrings. Sorting supports newest,
oldest, title A to Z and title Z to A. Results are paged in groups of 20. Search/sort
changes return to page one. Account verification preserves the current page for the
same account. An out-of-range page after data changes returns to the last available page.

Controls have keyboard focus, accessible names and tooltips. Deck titles remain plain
text; long titles are elided in the table with their full value in the tooltip and the
selected-deck details. Details include processing guidance and actual job attempts.
Queued/running pages refresh every 15 seconds only while Library is active. Search
requests are debounced and serialized; late replies cannot overwrite newer filters,
account changes or a closed window. There are no automatic network retries.

## API contract

`GET /api/v1/decks` requires the current Cognito access token in the Authorization
Bearer header. It uses the same live verification and persisted account resolution as
other private resources. The server always filters by that verified owner, including
the total and every joined job/analysis/asset record. It does not accept an owner UUID
as authorization. Results contain only deck ID, title, creation time, derived status
and job attempts; no storage keys, summary/action bodies, credentials or private provider errors.
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
version cannot mask a newer candidate/partial revision. Complete render proof must bind
every ordered manifest slide to its render revision and raster checksum. Current asset
metadata lacks those fields, so the library conservatively reports `incomplete` for
succeeded jobs until the renderer supplies that proof. An arbitrary asset row, matching
filename, or matching row count is insufficient. No fabricated
progress percentage or arbitrary worker exception is displayed. Until #8 provides job
progress/retry contracts, users see the actual lifecycle and attempt count and are
directed to support after failures. Refresh retries listing, not processing.

No upload, processing worker, preview download, rename, delete or presentation action
is added by #6. Those belong to #7-#12, #13, #32 and #37. No migration is needed;
this view reads the existing durable tables. Without decks produced by those workflows,
the genuine empty library is shown. Local tests seed disposable PostgreSQL records;
the application never injects sample decks or reads a backend database directly.

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
