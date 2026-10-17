# Private PPTX uploads (#7)

Uploads belong to the authenticated API, not the desktop UI. Issue #37 adds the native
upload controls. A completed upload appears in the existing library as **Awaiting
processing** (`uploaded`); no processing job, extraction record, analysis or fake worker
is created by this operation. Issue #8 owns scheduling.

The server validates the original presentation without rewriting, redacting or cleaning
it, computes/verifies SHA-256, and stores the exact received bytes in the private,
versioned S3 data bucket using the workload identity and explicit KMS encryption.
The accepted deck pins a server-generated source revision, digest, size, slide count,
private object key and S3 version. Public responses contain no bucket, key, version ID,
owner override or signed object URL. Existing ownership checks protect upload status,
content transfer and deck reads; verified-email policy remains issue #74's boundary.

## Supported files and limits

Only `.pptx` with content type
`application/vnd.openxmlformats-officedocument.presentationml.presentation` is accepted.
Extension/MIME alone are insufficient: the real contained OOXML parser checks package
structure, relationships, duplicate/traversal/symlink/encrypted ZIP entries, XML security,
slide count and resource budgets. A bounded additional pass verifies media CRCs.
Strict OOXML, legacy/encrypted PowerPoint, corrupt archives, macro parts/content types,
embedded files (including Excel workbooks), OLE and ActiveX are rejected. Ordinary text,
images and supported PresentationML remain unchanged. Charts containing an embedded
workbook fall under this restriction; this was an explicit first-version product choice.
External relationships are never fetched. This is structural/active-content validation,
not a general antivirus scanner or a promise of rendered visual fidelity.

| Setting | Default | Hard bounds |
| --- | --- | --- |
| `BUDDY_UPLOAD_MAX_BYTES` | 64 MiB | 1 byte–64 MiB |
| `BUDDY_UPLOAD_EXPANDED_BYTES` | 256 MiB | 1 byte–256 MiB |
| `BUDDY_UPLOAD_MAX_SLIDES` | 500 | 1–500, at least one slide required |
| `BUDDY_UPLOAD_QUOTA_BYTES` | 1 GiB per owner | 1 byte–1 TiB |
| `BUDDY_UPLOAD_OWNER_CONCURRENCY` | 2 | 1–8 |
| `BUDDY_UPLOAD_GLOBAL_CONCURRENCY` | 8 | 1–64 across API replicas |
| `BUDDY_UPLOAD_SECONDS` | 120 seconds | 5–300 |
| `BUDDY_UPLOAD_RESERVATION_SECONDS` | 600 seconds | 600–3600 |

The existing parser also bounds ZIP entries (10000), XML part size (8 MiB), XML depth/nodes,
objects, text and serialized output, with a 30-second/512-MiB parser process budget.
One parser runs at a time per API process to respect the staging API's 1-GiB memory size.
The transfer deadline includes waiting for that parser. Cancellation drains any in-flight
bounded parser/S3 call before removing its temporary source or starting byte cleanup;
this drain may extend the response deadline by that call's remaining duration. S3 calls
have 3-second connect/15-second read timeouts and no automatic retries.

Account quota includes accepted source bytes and pending/receiving/cleanup reservations.
Reservations consume upload slots before request bodies are read. A PostgreSQL transaction
advisory lock serializes admission and reservation-to-deck transfers across replicas.
The source is streamed into a private temporary directory with a byte counter and digest;
no multipart parser or whole-file request-body buffering is used. Metadata JSON is capped
at 8 KiB. Temporary source copies are removed on success, rejection and interruption.

## HTTP contract and retries

All operations require the existing `Authorization: Bearer ...` header over HTTPS.

1. `POST /api/v1/uploads` with one UUID `Idempotency-Key` header and strict JSON fields
   `filename`, `title`, `size_bytes`, lowercase `sha256`, and `content_type` reserves a
   durable upload. The returned `data.id` is an opaque upload UUID.
2. `PUT /api/v1/uploads/{id}/content` streams the original file with the PPTX content type.
   The actual byte count and digest must match the reservation. After validation, one
   conditional S3 PUT (`If-None-Match: *`) records an immutable source version. Deck creation
   and upload completion commit together. `data.deck.status` is `uploaded`.
3. `GET /api/v1/uploads/{id}` returns `pending`, `receiving`, `cleanup`, `failed` or `complete`,
   plus `expires_at`, `retryable`, a safe `error_code`, and the deck only after completion.

Keep the same idempotency key and metadata after a dropped response. A completed retry
returns the same deck and performs no S3 write. Concurrent transfers for the same pending
upload cannot both claim it. Changed metadata with the same key returns 409. A deliberate
new upload uses a new key and creates a separate deck even for identical bytes.

If status is `receiving` or `cleanup`, poll status and respect `Retry-After`; do not invent
a replacement key to recover an uncertain request. Once a transiently failed upload is
cleaned, repeat the original POST with the same key/metadata to rearm that same upload.
Then resend the complete original file. This version does not support byte-range resume.
A deterministic malformed/unsupported-file rejection is not retryable with identical
bytes; select a supported file and start a new upload. Uploads are not partially visible
as library decks. Errors use the existing safe request-ID envelope and HTTP 401/403/404,
408, 409, 413, 415, 422, 429 or 503; storage paths and external exception text stay private.

## Recovery and deployment

Apply additive migration `0004_uploads` explicitly with the migration identity. Existing
deck rows remain readable with nullable source metadata; historical migrations are unchanged.
Set `BUDDY_UPLOADS_ENABLED=true`, `BUDDY_DATA_BUCKET`, `BUDDY_AWS_REGION` and
`BUDDY_DATA_KMS_KEY_ARN`. Uploads default to disabled; other API features keep working.
Enabled storage registers a bounded read-only bucket readiness probe. Use workload identity,
not AWS keys in desktop settings. The source bucket must have versioning, blocked public
access, bucket-owner-enforced ownership, TLS-only policy and KMS configuration as provisioned.

The Terraform changes add API-only version-list/read/delete rights under `sources/*`,
the public storage configuration and a dedicated writable scratch mount. The image declares
that volume with owner 10001 and mode 0700; the root filesystem stays read-only. Apply the
reviewed infrastructure and use its new task-definition templates for promotion before
enabling the new API image. Merely changing the image on an old task does not add its mount
or environment. No new service, public bucket, queue consumer or production deployment is
introduced. Read [staging acceptance](staging-acceptance.md) and the release guide first.

Every admitted operation is persisted before S3 can be written. Receiving leases outlive
the bounded transfer/parser/network work. Normal failures durably mark cleanup; database
failure instead leaves the existing lease for reconciliation. Every API process runs a
bounded reconciliation pass at startup and every 30 seconds for cleanup and expired
reservations, using locked rows to prevent concurrent cleanup/retry/finalization. A failed
cleanup retains its quota reservation and is retried after restart. Lost commit responses
are resolved from durable state: an accepted deck's source is never deleted as an orphan.
Stale transfer leases cannot finalize a newer retry. Exact-key cleanup removes **versions
and delete markers**, not just a visible S3 delete marker. This is failure recovery, not
a general retention/deck-deletion system (issue #35).

## Checks and manual acceptance

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/api/test_upload_validation.py -q
# Set the disposable PostgreSQL URL described in docs/database.md first:
.\.venv\Scripts\python.exe -m pytest backend/tests/api/test_uploads.py -q
.\.venv\Scripts\python.exe scripts/quality.py all check
.\.venv\Scripts\python.exe -m buddy_api.schema --check --compare-ref origin/main
```

Database tests use real PostgreSQL and the real authenticated HTTP/service/persistence
paths. Only provider verification and S3 network calls are deterministic test boundaries.
S3 SDK contract tests verify checksum/KMS/conditional-write and version-aware cleanup.
Adversarial files are generated locally and never require an external corpus or credentials.

Required staging acceptance is **not satisfied by unit tests**. Deploy the reviewed issue
head through an authorized staging route, then record the API image/source SHA and:

- Complete a real browser login and upload a small Buddy-authored PPTX; the existing
  library must show Awaiting processing. Repeat with the same key and verify one deck.
- As the API workload identity, read the exact stored S3 version and verify byte-for-byte
  equality/SHA-256. Verify anonymous object reads are denied without publishing its key.
- Sign in as a second real account; both upload status/content operations and the deck
  resource must deny the first account's IDs; its library must exclude the first deck.
- Exercise a dropped response/restart, malformed/bomb/embedded-file rejection, and an
  injected storage/commit failure in an isolated staging test task/schema. Verify cleanup
  leaves no source versions and recovery does not duplicate the accepted deck.
- Keep tokens, private filenames/content, bucket keys and raw operator logs out of public
  evidence. Publish only sanitized results and the reviewed revision. Until real S3 and
  owner isolation pass, issue #7 stays incomplete and dependent work must wait.

For the human browser-login/upload checks, close Buddy and run from PowerShell in the
repository root (this uses the same secure saved-session store as Buddy):

```powershell
.\.venv\Scripts\python.exe tests/manual_upload.py --file frontend/tests/fixtures/slide_actions/standard.pptx
```

If no saved session can resume, a browser opens for you to sign in. The helper prints
only test results and non-secret retry/upload/deck UUIDs. Record those UUIDs. Run it again
with the same `--file` and `--key <printed-retry-UUID>`; expect the **same deck UUID**.
A new key deliberately creates a new deck. This is a real upload; accepted deck deletion
remains issue #35. Use the Buddy-authored fixture or a file you intend to store privately.

To check a second account, first sign out of the first account in Buddy (including its
browser handoff), close Buddy again, and run:

```powershell
.\.venv\Scripts\python.exe tests/manual_upload.py --new-login --check-other-upload <first-upload-UUID> --check-other-deck <first-deck-UUID>
```

Sign in as the **other account**. Both reads and an empty transfer attempt must return 403;
signing into the same account correctly fails this isolation test. Operator acceptance
must additionally check S3 original-version equality/cleanup as listed above.
The helper never fetches another account's private file or prints tokens. After the test,
relaunch Buddy with the staging settings from [accounts](accounts.md) and refresh Library;
the first account should see its saved deck as Awaiting processing.
