# Security baseline

This document records controls currently implemented in Buddy's PDF analysis path and
separates them from future policy, residency, and retention work. Issue #36 is cancelled;
privacy settings, policy acknowledgements, and a provider-retention approval workflow are
not prerequisites or release blockers for Sprint 2.

## Source and ownership

- Uploaded PDFs are stored as private, versioned S3 objects with server-side KMS
  encryption, TLS-only access, blocked public access, and owner/deck-scoped database rows.
- Worker messages contain only the job ID and generation. The worker reads the exact
  accepted S3 VersionId and verifies media type, size, and SHA-256 before parsing or
  submitting the source to the model.
- Analysis, action, and validation records use owner/deck composite foreign keys and
  immutable revision hashes. API reads require verified authentication and return
  `Cache-Control: no-store`.
- A worker lease and generation fence prevent cancelled, stale, or superseded work from
  publishing results. Idempotency keys bind retries to the same source/action request.

## Model request

The worker sends the unchanged, checksum-verified original PDF as a base64 PDF document to
Amazon Bedrock's native InvokeModel API for Claude Sonnet 4.6. The normal worker does not submit extracted text,
rendered-page images, converted files, or sanitized/redacted copies. PDF content is
untrusted input; the prompt says not to follow instructions embedded in the document, and
the response is constrained to JSON summaries plus `zoom_region` and `highlight_region`
objects. Buddy validates slide order, IDs, schema, catalog membership, coordinate bounds,
ownership, source revision, and action hash before persistence.

The opt-in [text-candidate experiment](analysis.md#opt-in-experiment-select-measured-text-ids-instead-of-predicting-boxes)
also submits extracted horizontal line text, location-specific IDs and measured regions
alongside the original PDF. It is not enabled in the worker. Its response selects IDs
instead of supplying coordinates; selected geometry is independently measured again
before grounding. Candidate text remains private, untrusted document content and is
excluded from logs. Extraction is bounded to 2,000 candidates and 80,000 text characters
per deck, with the existing encoded provider request limit still enforced.

The worker uses its ECS task role through SigV4; no Anthropic API key or long-lived AWS key
is stored in application configuration. IAM permits `bedrock:InvokeModel` only on the
configured EU Claude Sonnet 4.6 inference profile, the profile's routed foundation-model
resources, and the account's default Bedrock project. The EU profile can route a request
among supported EU AWS Regions. The native InvokeModel API does not expose the Responses
API's `store` option; this does not promise a particular provider-side retention behavior.

## Resource and spend limits

- Original provider input: at most 18 MiB and 100 PDF pages. Oversized documents fail with
  a safe, explicit error; Buddy does not switch representations or silently truncate.
- The InvokeModel call has a 90-second timeout and a 25 MiB encoded request bound, plus a
  12,000-token output cap. The input-size bound accounts for base64 expansion. Each job has
  at most three lifetime attempts.
- The generated result is bounded by the existing #11 record limits and at most 40 actions
  per deck. Every action is independently run by the isolated #49 validator with a
  15-second execution limit. The whole job has a 900-second deadline, a bounded SQS
  visibility period, worker concurrency, and per-owner active-job quota.
- These hard request/context/output/attempt bounds constrain token consumption and cost.
  AWS token rates and account quotas can change; account-level AWS billing alerts and
  budgets remain the operator's control for aggregate spend.

## Temporary data and diagnostics

The PDF metadata/parser stage runs in a short-lived isolated process with a bounded
deadline and memory. It computes the #11 raster checksums in memory for provenance only;
it does not write a page-image cache or send raster data to Bedrock. The #49 validation
runtime uses a private temporary directory for its source and output images, then stores
owner-scoped validation evidence in the encrypted private bucket. Temporary work is
removed on normal completion and bounded worker-owned scratch is cleaned on restart.

Provider responses, source bytes, email addresses, credentials, tokens, and document text
are excluded from application logs. Only stable sanitized failure codes are stored on
jobs. Failed validations remain unavailable with report evidence and reasons; rejected or
uncertain actions are never marked usable. Original sources and published referenced
analysis revisions are not deleted by worker cleanup.

## Scope and deployment state

Implemented in the issue #12 branch: the Bedrock adapter, bounded durable PDF pipeline,
analysis persistence, action validation binding, and this baseline. The account's EU Claude
Sonnet 4.6 profile is authorized and live original-PDF evaluation has been performed on the
permissioned model-evaluation corpus; results and limitations are recorded in the PR. This
branch has not yet been merged or deployed; staging acceptance must still verify the
reviewed worker image/task role, real staging output, and recovery behavior.

User consent/terms UI, provider-retention approval workflows, cross-Region residency
controls, content sanitization, general retention/deletion policy, PPTX analysis, preview
render persistence, and account-wide budget automation are outside issue #12. Private
storage, ownership, TLS, server-side credentials, validation, bounded resources, cleanup,
and sanitized diagnostics remain required controls.
