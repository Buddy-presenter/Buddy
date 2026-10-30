# Durable PDF analysis workers (#8, #12)

The durable PDF worker performs page metadata inspection, submits the accepted original
PDF unchanged to Amazon Bedrock Claude Sonnet 4.6, then resolves generated targets in a
bounded child process using PDFium text/object geometry and on-demand Tesseract OCR for
unmatched text anchors. It persists ordered summaries and only source-grounded content-action
candidates, then validates every action through the isolated #49 executor before completing
the job. Rendered OCR pages are transient, are not sent to another provider or saved as
evidence, and are removed with the child scratch directory. Default quote mode does not
send extracted text; opt-in candidate mode sends its ephemeral text catalogue alongside
the unchanged original PDF, as described below. Existing
PPTX upload/storage remains compatible; PPTX analysis and playback are deferred.

## Scheduling and publication

An accepted PDF upload creates its revision-bound job and `job_outbox` row in the same
PostgreSQL transaction as the deck. The unique `(deck_id, source_revision)` constraint and
transaction-scoped admission lock make scheduling idempotent across replicas. The API
dispatcher also reconciles accepted pre-upgrade PDFs in bounded batches. Scheduling and
retry enforce the owner quota (four active jobs by default); a full quota returns a safe
429. A failed upload transaction cannot leave an orphan job or accepted source; quota
failures retain the upload's same-key retry path after capacity becomes available.

The API publishes identifier-only `buddy.job.v1` messages through SQS. Publish failure
leaves the outbox durable and retries with bounded exponential backoff (30 seconds up to
15 minutes); a send followed by a lost commit can still produce a safe duplicate. After a
successful send, reconciliation schedules a replacement before the earlier of SQS message
retention expiry or the configured redrive horizon (with one visibility interval reserved
for recovery). An idle or absent worker therefore does not create a duplicate every 30
seconds, and a valid job cannot remain dormant until the full 24-hour retention expires
after its wake-up reaches the DLQ. A capacity miss acknowledges the current delivery and schedules one
replacement after a 60-second cooldown, avoiding repeated SQS receives and DLQing work just
because concurrency is temporarily full. Processing failures reset the outbox to their
existing bounded job retry schedule. Only the API role sends; workers receive/delete and
reconcile DB leases. Multiple dispatchers lock job rows with SKIP LOCKED. No polling thread
holds source bytes.

## Enable candidate-line targeting

The default worker mode is `quote`. To try the experimental text-ID method in the normal
desktop Library/Review flow, start the reviewed worker with:

```powershell
$env:BUDDY_WORKER_TEXT_TARGETING = 'candidate_lines'
.\.venv\Scripts\python.exe -m buddy_api.processing
```

This is a **worker** setting; setting it only in the desktop or API process has no effect.
Local workers still need the existing database, private source storage, queue, validator
and Bedrock access. The desktop never receives AWS credentials. This setting does not
replace those services or enable a credential-free live model call.

For ECS, both Terraform environment roots expose `worker_text_targeting` (default
`quote`). Set it to `candidate_lines` in the reviewed environment configuration; it is
included only in the worker task definition. Apply through the normal reviewed release
process and verify the running worker image and task environment. This flag does not
bypass staging compatibility checks: traced analyses require the compatible API and
rollback reader described in [analysis](analysis.md). Do not start a trace-publishing
worker against an incompatible reader.

After deployment, open Buddy, sign in normally, and upload the test PDF as a **new deck**
from Library. Wait for processing, then open Review and select the generated highlights.
Opening the local PDF in Present does not request a new analysis. Completed old jobs and
saved boxes are not regenerated when the flag changes; do not use Retry on a completed
analysis to test a different generator. Candidate traces report `pdf_candidate_line` and
the chosen target ID. Selection is performed by the live model; availability of a measured
line does not guarantee the model selects it.

This mode currently produces whole horizontal left-to-right text-line highlights only.
It does not generate zooms, OCR/diagram targets, rotated-page targets or multiline boxes.
Independent visual verification remains required for usability. Revert the setting to
`quote` and restart/redeploy the worker to change **future** generation. Existing candidate
analyses retain their immutable geometry and require the compatible reader to remain
available; changing the flag is not a database or reader rollback.

The [offline experiment](analysis.md#opt-in-experiment-select-measured-text-ids-instead-of-predicting-boxes)
still works without AWS credentials and proves geometry only.

## States and limits

New jobs transition `queued -> running` through `pdf_metadata.v1` and `pdf_analysis.v1`,
then finish as `analysis_ready` or explicitly `analysis_partial`. A partial status means
one or more summaries were unavailable; it never implies completion of missing content.
Transient failures enter `retry_wait`, with 2/4/8-second backoff and at most three lifetime
execution attempts.
Malformed/unsupported PDFs and exhausted jobs enter `dead_letter`. This durable terminal
state is the business failure record; malformed SQS envelopes are left unacknowledged for
the queue's physical three-receive DLQ policy. Healthy duplicate/obsolete messages are
acknowledged; capacity misses are rescheduled through the durable outbox. No message
contains source keys, credentials, presentation contents, owner identity or raw failure
diagnostics.

The global DB admission lock bounds claimed work to two jobs by default, across worker
replicas. Each worker process executes one job at a time. Its 45-second lease is renewed
every 10 seconds, but never beyond the 900-second attempt deadline. PDF inspection and
ephemeral PDFium manifest hashing run in an isolated process with a 120-second deadline,
1 GiB address-space ceiling, 30-second metadata parser limit and no persisted page images.
Provider calls time out after 90 seconds. The provider boundary accepts at most an 18 MiB,
100-page PDF, returns at most 12,000 tokens, and is attempted at most three times for a job.
The 18 MiB source limit leaves room for base64 expansion and request metadata under Bedrock's
25 MiB InvokeModel body quota.
The model context window and request/output limits bound token spend; generation can produce
multiple distinct actions per slide, with validation capped at 40 actions per deck and 15 seconds
per isolated action run. Review warns when all 40 slots are used because additional useful
actions may have been left out. Generated highlights must quote a visible target phrase and are
rejected if their rectangle exceeds 6% of page height; this catches paragraph-sized regions but
does not establish semantic correctness. Fargate independently bounds
worker CPU/memory to 1 vCPU/2 GiB. SIGTERM drains bounded work, releases the lease and exits;
abrupt termination recovers through lease expiry.

Each claim has a random lease token. Cancel/retry/reconciliation invalidates that token
and increments the message generation. Metadata persistence locks and rechecks the job,
lease/deadline, source revision/format/SHA and owner in the transaction that saves the
immutable extraction. Analysis persistence is idempotent per job; restarts reuse saved
provider output and resume only missing validations. Image-subregion zoom candidates marked
for visual QA remain non-usable, and executor validation intentionally skips them until the
independent #50/#51 workflow is available. Each action report is bound to its
analysis/action hashes and renderer/profile before final completion. Late results cannot
resurrect cancelled or superseded work. S3 reads use the accepted VersionId and verify size,
media type and SHA before inspection or provider submission. Originals are never deleted.

Container scratch must not be shared between worker replicas. An exclusive OS lock
protects its worker-owned `buddy-processing` directory; startup removes only abandoned
`work-*` directories after obtaining that lock. Normal stage directories are removed on
exit, including a parent-owned per-attempt directory removed after a timed-out child dies.
Hard-killed worker processes leave files only inside that owned workspace for restart cleanup;
Fargate also discards scratch with the task. Never sweep global/system temporary files.

## API and typed output

All routes require verified account authentication and return `Cache-Control: no-store`:

- `GET /api/v1/decks/{deck_id}/job`: discover the owner's most recent job.
- `GET /api/v1/jobs/{job_id}`: persisted stage, attempts, progress, safe error, lease and
  extraction identifier. Another owner's job is indistinguishable from a missing job.
- `POST /api/v1/jobs/{job_id}/cancel`: idempotently cancel active work.
- `POST /api/v1/jobs/{job_id}/retry`: requeue cancelled or failed work if its lifetime
  attempt budget and owner quota permit. Repeated retries of active work return 409.

`PDFSource`, `Message`, and `MetadataOutput` are versioned typed boundaries. The metadata
output wraps extraction v1 with ordered stable page IDs, EMU dimensions and PDF
limitations/warnings, plus a transient manifest whose image hashes are computed in memory.
The worker then creates an immutable `AnalysisInput`, runs the independent action validator,
and marks `analysis_ready` only after every proposed action has a terminal recorded result.
Unavailable summaries produce `analysis_partial`; unsafe or uncertain actions remain
rejected or needs-review and cannot be used.

The library's additive processing detail mirrors these stored stages as uploaded, queued,
PDF metadata inspection, analysis, validation, review ready, partial, failed or cancelled.
The worker persists render/provider/validation transitions under its lease; expired workers
cannot advance a newer attempt. A retry reuses source-bound metadata and saved analysis,
while publication and validation identities keep repeated writes from duplicating outputs.
The older coarse library status remains unchanged for old desktop clients.

Migration 0010 expands the existing status CHECK without rewriting legacy records.
Jobs with NULL `source_revision` are legacy and are not claimed, reconciled or controlled
by the new worker. Compatibility library mapping:

| Stored status | Library status |
| --- | --- |
| pending / queued / retry_wait | queued |
| running | processing |
| failed / dead_letter | failed |
| succeeded | existing analysis/render-proof mapping, otherwise incomplete |
| metadata_ready / analysis_partial / cancelled | incomplete |
| analysis_ready | ready only if separate complete render evidence exists; currently incomplete |

New clients also consume `processing.state`: `metadata_ready` is shown as failed/recoverable
and may be retried if the source is still current and the lifetime attempt budget allows it;
`analysis_partial` remains reviewable, and only a latest published analysis for the current
source revision is `review_ready`. The legacy status table above is retained for older apps.

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
configuration also enables the API outbox dispatcher. Terraform sets
`BUDDY_WORKER_QUEUE_MESSAGE_RETENTION_SECONDS` from the SQS queue's retention attribute so
the API and worker reconcile against the deployed queue policy. Without queue configuration,
accepted jobs remain durably queued, not falsely successful. Apply migrations explicitly;
startup never runs DDL.

Build `infra/container/Dockerfile.worker`, push an immutable digest to the worker ECR
repository and supply the reviewed `worker_image` to the existing Terraform/release
workflow. Terraform mounts dedicated scratch, gives ECS an actual container health check
and 120-second stop timeout, and grants the separate worker task role exact-version reads
only under `sources/*`. Worker task, execution and database roles remain separate from
API/migration roles. The worker task role can invoke only the EU Claude Sonnet 4.6 inference
profile, its routed foundation-model resources, and the account default Bedrock project; it
receives no long-lived provider key. The AWS account must be entitled to the model before
rollout; a provider access-denied
response is recorded as a sanitized permanent failure instead of retrying a request that
cannot succeed. Bedrock inference is billed to the AWS account; no OpenAI API key or
separate OpenAI billing account is used. The EU profile can route processing to supported
regions within the EU geography. The adapter uses Bedrock's native InvokeModel API, which
does not expose a response-retention option. This does not create a residency
guarantee or privacy-consent/policy system. No AWS/root credentials are embedded
in images, configuration or CI.

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

### Disposable pre-merge ECS acceptance

With explicit account-owner authorization for temporary resources and AWS charges,
`backend/scripts/staging_worker_acceptance.py` can test a candidate before promotion.
It is an operator-only exception, not a change to the main-only deployment workflow.
It never updates an ECS service or migrates the application's `buddy` database.

Build the candidate from a clean archive of its exact commit with
`infra/container/Dockerfile.worker`. Supply short-lived operator credentials in memory,
then run from the repository root on the Windows/WSL Docker operator workstation:

```sh
python backend/scripts/staging_worker_acceptance.py \
  --commit <full-candidate-commit> --image <built-local-worker-image> \
  --state build/worker-acceptance.json
```

This runner is deliberately restricted to the existing Buddy staging account/region.
It reuses its RDS instance, network, KMS key, bucket and worker ECR repository. It creates
one separate, uniquely named `_test` database and restricted SQL role, encrypted test
queues, synthetic source prefix, runtime secret, task roles, log group and one-off tasks.
No root/operator credentials enter a task or image. The admin helper alone reads the
RDS master secret inside ECS; the worker cannot access it. The worker task role mirrors
the Terraform permissions, further restricting source reads to the synthetic prefix.

The installed candidate exercises real PDF processing, malformed input, duplicate
delivery, running cancellation/retry, output persistence, actual ECS hard interruption
and replacement recovery, and ECS health. The fault-drill task intentionally uses a
two-second stop timeout while the controller holds the database completion fence;
the replacement uses the normal 120 seconds. Temporary workers have bounded lifetimes.
This tests replacement recovery, not ECS service auto-replacement or production rollout.

The sanitized state journal records identifiers and results, never passwords or session
tokens. Keep it outside version control. Cleanup stops tasks, drops only the marked
test database/role and removes test objects including versions, queues, IAM roles,
secret, logs, task definitions and image tag. If interrupted, retry cleanup with the
same journal and fresh operator credentials:

```sh
python backend/scripts/staging_worker_acceptance.py \
  --cleanup --state build/worker-acceptance.json
```

If database cleanup fails, the runner retains its admin-access path for retry and
reports incomplete cleanup. Never discard the journal until cleanup is confirmed.
Budget for small incremental AWS charges; billing/credit reporting may lag the run.
