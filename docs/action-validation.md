# Isolated PDF action validation (#49)

The requester explicitly authorized combining issues **#10 and #49 in PR #91** as an
exception to the normal one-issue-per-PR rule. This does not change the default workflow
or issue ownership.

The validator tests a supplied catalog action against a saved PDF using the same
`SlideExecutor`, PDFium renderer and `SlideSurface` as Buddy. It does not call a model,
extract text, choose actions, operate a user's real desktop, or approve natural-language
intent. Tests provide predefined commands. A later pipeline may submit model-proposed
commands to the same callable/API.

## Interface and results

- `POST /api/v1/decks/{deck_id}/action-validations` takes `{"profile":"wide-1x",
  "action":{...catalog instance...}}`, an authenticated account and one UUID
  `Idempotency-Key` header. The server fetches the owner's exact source version and runs
  the check. Clients cannot submit a result/state or an executable/path/URL.
- `GET /api/v1/action-validations/{id}` reads the resulting versioned report and opaque
  evidence references. `GET /api/v1/action-validations/{id}/evidence/{artifact_id}` returns
  one checksum-verified artifact. Missing/foreign IDs return 404; responses are no-store.
- The internal callable is `ValidationService.validate(owner, deck_id, key, request)`.
  It derives source revision/hash from the owned record. It remains independent of
  analysis generation, queues, sessions and `analysis_versions`.

`validated` means the action executed with the expected deterministic state and pixels
in the reported runtime/profile. `rejected` covers bad catalog parameters, stale IDs,
unknown slides, unsupported actions or an unexpected execution/image result.
`needs-review` covers crashes, resource limits, cancellation or an unsupported runtime.
Every non-passing result has machine-readable reasons. `semantic_verified` is always
false: a correctly executed highlight may still refer to the wrong content.

Malformed top-level requests reject with 422, oversized requests with 413, occupied local
capacity with 429, unavailable runtime configuration with 503 and retry/source conflicts
with 409. Well-formed requests containing invalid action instances return a stored
`rejected` result, not a client-asserted success. API input is capped at 16 KiB and action
content at 8 KiB, with bounded depth/arrays/objects and finite JSON values.

## What is verified

The isolated process loads an immutable PDF snapshot, checks its digest, resolves the
expected current page, and resets it to a full-page baseline. It dispatches one action
(`reset_view` uses a deterministic seeded zoom/highlight so its clearing behavior is tested)
through the production executor and waits for bounded Qt completion. Before/after PNGs
capture only the actual slide widget via `surface.grab()`, never another desktop/window.

An independent catalog calculation checks the destination page, canonical viewport,
highlight and source-to-view transform. A separate reference image checks the actual
painted output, including highlighting. Qt's widget and image interpolation can differ
slightly, so each color/alpha channel permits a difference of at most 4/255; geometry
comparison is much tighter (1e-7 absolute / 1e-9 relative). Invalid actions must leave the
baseline state and pixels unchanged. A successful completion callback alone never passes.

All six v1.1 controls and the original five-control v1.0 catalog are supported. Profiles:
`wide-1x` (1280x720), `standard-1x` (960x720), `portrait-1x` (720x960) and `wide-2x`
(1280x720 logical, DPR 2). Actual DPR must match. Reports pin validator `qt-executor-1`,
PDFium binding/144-dpi renderer version, Qt version, OS, source hash/revision, action hash
and display profile. A report from Linux is not proof of identical fonts/pixels on macOS
or Windows. Each OS/profile is tested in CI; unsupported runtime/profile stays unvalidated.

Backend contracts are in `buddy_api.validation.contracts`; the standalone request/result
schemas shipped with the desktop worker are checked for parity by integration tests.
The backend never imports desktop implementation or Qt. It launches the configured
Python executable with `-m buddy_desktop.validation_worker` and fixed private job files.

## Evidence, retries and cleanup

Migration `0007_action_checks` adds owner/deck-scoped `action_checks` with a unique
owner/retry key, request digest, bounded storage reservation, durable phase, result and
private artifact metadata. Existing analysis-validation history is not overwritten.
Future issue #12 can explicitly bind these reports/evidence to matching stored analysis
actions using issue #11's contract; this endpoint does not automatically publish analyses
or mark an action usable.

Before/after PNGs and a JSON report use KMS-encrypted, versioned private S3 objects under
`sources/<owner>/<deck>/validation/<run>/<kind>.<extension>`. No storage paths or S3 versions
are exposed. Exact-version reads verify MIME, length and SHA-256. Existing scoped
`sources/*` version-read/cleanup permissions suffice; no public bucket or new broad IAM
grant is introduced.

Each pending check reserves 32 MiB against the same private storage quota as original
uploads. Completion replaces that reservation with the actual artifact byte count.
Each artifact is capped at 16 MiB, reports at 64 KiB. Original uploads also count existing
evidence/reservations when admitting another upload. Same-key/same-request retries return
the same completed result; a changed source/action/profile conflicts. Failed attempts can
retry only after cleanup. Accepted evidence is preserved on a lost commit acknowledgement.

Before writing evidence, the run is durably reserved. On failure, only that run's three
known object keys are cleaned, including all S3 versions. Cleanup failures retain their
reservation for retry. Startup's bounded reconciler retries cleanup and pending runs
expired after five minutes; complete runs and original PDFs are never removed by it.
Private scratch directories are cleaned after completion, failure, timeout and cancellation.

## Runtime and staging wiring

`BUDDY_VALIDATOR_PYTHON` must be an operator-configured absolute installed executable.
Unset means validation is unavailable; the API does not silently use another interpreter.
The existing API image builds a separate `/opt/buddy-validator` virtual environment from
the desktop wheel, including PDFium, Qt and its notices. The API interpreter itself has
no desktop/Qt imports. The image installs the EGL/GL/fontconfig libraries and DejaVu fonts
needed for actual Linux offscreen rendering; retain their packaged license notices.

Terraform passes the validator executable to the staging task template. API memory is
2 GiB to leave headroom for API/PDF bytes plus the constrained Qt process tree. One local
resource slot is shared by upload validation, PDF/evidence reads and action checks.
The validator process tree has a 768-MiB sampled RSS limit, a 60-second parent deadline,
45-second Qt deadline, 32-file/256-MiB scratch limit, and POSIX CPU/file-size limits.
Windows additionally uses a kill-on-close Job Object; POSIX cleanup kills the isolated
process group. Cancellation signals the monitor and stops that process tree.

The worker uses a fresh offscreen application, private HOME/temp locations, and an
environment allowlist without AWS/database credentials. It only reads fixed job inputs
and invokes the known PDF renderer; catalog payloads cannot select a shell, local path,
network request, account or personal profile. PDF scripts/forms/links are not executed.
Resource/process containment is not a general native-code security sandbox. The container
smoke also exercises the packaged worker with no networking, a read-only root and dropped
capabilities, proving it requires neither a display server nor network access.

Apply the reviewed migration and task template, then promote the reviewed API image using
the existing staging release procedure. Local/CI container verification is not a live AWS
deployment; this PR does not claim a deployment that has not been performed.

## Checks

```sh
python scripts/quality.py all check
python scripts/test.py all
python -m buddy_api.schema --check --compare-ref origin/main
docker build -f infra/container/Dockerfile -t buddy-api:ci .
python infra/container/smoke_validator.py --image buddy-api:ci
```

Set `BUDDY_TEST_DATABASE_URL` to a disposable `*_test` PostgreSQL database for API/evidence
tests. Tests use real isolated Qt/PDF rendering; only S3 is injected at the external I/O
boundary. Coverage includes every catalog control, navigation boundaries, mixed page
shapes/crop/rotation, DPR, stale/malformed/out-of-bounds commands, cancellation, real child
crash/hang, owner isolation, lost S3 acknowledgement, expired-run cleanup and retries.
