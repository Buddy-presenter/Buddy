# Durable PDF metadata workers

Issue #8 adds a backend-only PDF metadata stage. It does not run an LLM, extract PDF
text/positions/shapes, render previews, perform OCR, validate generated actions, or process
PPTX. Provider analysis remains #12. Existing PPTX storage/extraction remains compatible.

## Scheduling and publication

An accepted PDF upload creates its revision-bound job and `job_outbox` row in the same
PostgreSQL transaction as the deck. The unique `(deck_id, source_revision)` constraint and
transaction-scoped admission lock make scheduling idempotent across replicas. The API
dispatcher also reconciles accepted pre-upgrade PDFs in bounded batches. Scheduling and
retry enforce the owner quota (four active jobs by default); a full quota returns a safe
429. A failed upload transaction cannot leave an orphan job or accepted source.

The API publishes identifier-only `buddy.job.v1` messages through SQS. Publish failure
leaves the outbox durable; a send followed by a lost commit can duplicate delivery safely.
Every unclaimed intent is republished after 30 seconds, including wakeups lost to SQS
retention/DLQ. Only the API role sends; workers receive/delete and reconcile DB leases.
Multiple dispatchers lock job rows with SKIP LOCKED. No polling thread holds source bytes.

## States and limits

New jobs transition `queued -> running -> metadata_ready`. Transient failures enter
`retry_wait`, with 2/4/8-second backoff and at most three lifetime execution attempts.
Malformed/unsupported PDFs and exhausted jobs enter `dead_letter`. This durable terminal
state is the business failure record; malformed SQS envelopes are left unacknowledged for
the queue's physical three-receive DLQ policy. Healthy duplicate/obsolete messages are
acknowledged; capacity misses retain their wakeup. No message contains source keys,
credentials, presentation contents, owner identity or raw failure diagnostics.

The global DB admission lock bounds claimed work to two jobs by default, across worker
replicas. Each worker process executes one stage at a time. Its 45-second lease is renewed
every 10 seconds, but never beyond the 120-second attempt deadline. The complete S3
download/parser process has a 105-second hard deadline; the existing parser additionally
enforces 30 seconds, 512 MiB, 64 MiB source, 500 pages and 16 MiB result limits. The outer
stage has a 1 GiB address-space ceiling on Linux or Windows process-tree containment;
Fargate independently bounds worker CPU/memory to 1 vCPU/2 GiB. SIGTERM drains bounded
work, releases the lease and exits; abrupt termination recovers through lease expiry.

Each claim has a random lease token. Cancel/retry/reconciliation invalidates that token
and increments the message generation. Result persistence locks and rechecks the job,
lease/deadline, source revision/format/SHA and owner in the same transaction that saves the
immutable extraction and completion status. A partial save rolls back both. Late results
cannot resurrect cancelled or superseded work. S3 reads use the accepted VersionId and
verify size, media type and SHA before inspection. Originals are never deleted by jobs.

Container scratch must not be shared between worker replicas. An exclusive OS lock
protects its worker-owned `buddy-processing` directory; startup removes only abandoned
`work-*` directories after obtaining that lock. Normal stage directories are removed on
exit. Hard-killed stages leave files only inside that owned workspace for restart cleanup;
Fargate also discards scratch with the task. Never sweep global/system temporary files.

## API and typed output

All routes require verified account authentication and return `Cache-Control: no-store`:

- `GET /api/v1/decks/{deck_id}/job`: discover the owner's most recent job.
- `GET /api/v1/jobs/{job_id}`: persisted stage, attempts, progress, safe error, lease and
  extraction identifier. Another owner's job is indistinguishable from a missing job.
- `POST /api/v1/jobs/{job_id}/cancel`: idempotently cancel active work.
- `POST /api/v1/jobs/{job_id}/retry`: requeue cancelled or failed work if its lifetime
  attempt budget and owner quota permit. Repeated retries of active work return 409.

`PDFSource`, `Message`, and `MetadataOutput` are versioned typed boundaries. The only
implemented stage is `pdf_metadata.v1`; its output wraps the existing extraction v1
document, with ordered stable page IDs, EMU dimensions and PDF limitations/warnings.
`analysis_ready` is always false here. A later stage must add an explicit contract,
implementation and tests before it may claim success; metadata does not imply that a
provider/validation stage ran. No analysis record is created by this worker.

Migration 0009 expands the existing status CHECK without rewriting legacy records.
Jobs with NULL `source_revision` are legacy and are not claimed, reconciled or controlled
by the new worker. Compatibility library mapping:

| Stored status | Library status |
| --- | --- |
| pending / queued / retry_wait | queued |
| running | processing |
| failed / dead_letter | failed |
| succeeded | existing analysis/render-proof mapping, otherwise incomplete |
| metadata_ready / cancelled | incomplete |

PDF presentation remains available directly from its pinned original. Metadata-ready is
not analysis-ready or review-ready. Old four-state records and new-state mapping have
regressions; migration comparison must produce no schema drift. The tested legacy
compatibility bridge now explicitly accepts 0009 as well as 0003/0008. Existing deployed
bridge images do not gain that support automatically: rebuild/review/reverify the exact
bridge digest before any rollout that needs it. Do not bypass release compatibility gates.

## Running and deployment

Use `python -m buddy_api.processing` or the installed `buddy-worker` command. Required:
normal backend database/private storage configuration plus `BUDDY_PROCESSING_QUEUE_URL`
and `BUDDY_AWS_REGION`. `BUDDY_WORKER_OWNER_QUOTA`, `CONCURRENCY`, `MAX_ATTEMPTS`,
`LEASE_SECONDS`, `RUNTIME_SECONDS` (each prefixed `BUDDY_WORKER_`) have validated bounds;
configuration is operator-owned, never accepted from a desktop/API request. Queue
configuration also enables the API outbox dispatcher. Without it, accepted jobs remain
durably queued, not falsely successful. Apply migrations explicitly; startup never runs DDL.

Build `infra/container/Dockerfile.worker`, push an immutable digest to the worker ECR
repository and supply the reviewed `worker_image` to the existing Terraform/release
workflow. Terraform mounts dedicated scratch, gives ECS an actual container health check
and 120-second stop timeout, and grants the separate worker task role exact-version reads
only under `sources/*`. Worker task, execution and database roles remain separate from
API/migration roles. No AWS/root credentials are embedded in images, configuration or CI.

`python -m buddy_api.processing --health` succeeds only with a fresh health timestamp
maintained by real DB readiness and SQS access checks. A dependency failure exits the
process; ECS health/restart and durable reconciliation recover it. Logs contain only
sanitized operational messages, never parser exception text or credentials.

## Verification

Run component/repository checks, migrations and `python scripts/test.py worker` against
an isolated PostgreSQL `_test` database. Build and run the container smoke documented in
[testing](testing.md). For an authorized operator-only cloud smoke:

```sh
python backend/scripts/smoke_processing.py --account <staging-account> \
  --region eu-central-1 --bucket <staging-data-bucket> --kms-key <data-key-arn> \
  --pdf tests/fixtures/slides/Buddy_PDF_Geometry.pdf
```

It requires short-lived AWS credentials and `BUDDY_TEST_DATABASE_URL`. It verifies the
account, creates isolated encrypted test queues and versioned synthetic S3 originals,
starts the real worker process, exercises expired-lease recovery/success and malformed-PDF
failure, then removes its queues, all object versions, schema and processes. It does not
consume the application's existing queue or migrate the application database. Keep its
sanitized output as evidence; never export session tokens into files or source control.

Live service acceptance additionally requires the reviewed worker image/task role against
private staging RDS: startup, SQS/S3/DB access, real PDF output, failure and interrupted-task
recovery, verified health and cleanup. Run only through reviewed deployment paths and keep
production untouched. A passing local/cloud-transport smoke alone does not establish this
last deployment gate.
