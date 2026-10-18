# Staging compatibility transition (#96)

The original staging API image is pinned in `infra/container/Dockerfile.compatibility`.
It expects migration `0003_extractions`, including `analysis_versions.graph`. Main's
new analysis storage removed that column in `0005_analysis`. Simply restoring the
column does not fix rollback: the original application's readiness rejects newer
migration heads. Do not approve the former one-step deployment or restore that original
image after the feature migration.

## Two separately approved deployments

After this PR merges and current-main CI succeeds, use **Backend promotion and rollback**
on reviewed `main`, target `staging`:

1. Select `rollout_phase=compatibility`. This builds the temporary bridge directly on
   the pinned original image. It changes only readiness to accept exactly `0003_extractions`
   and `0008_rollback_compatibility`, checking every legacy mapped column. Its packaged
   migrations remain at 0003, so this deployment does not expand the live schema.
   GitHub's staging environment approval is still required.
2. Verify the bridge is healthy, retain the successful deployment audit and inspect the
   compatibility report. Then dispatch a **separate** run with `rollout_phase=feature`.
   Require a second staging approval. This run must observe the bridge as the immediate
   rollback target. It applies the forward repair through 0008 and promotes the feature
   image. Failed promotion restores the tested bridge without downgrading the database.

Do not supply a worker image for this transition. The worker needs its own compatibility
contract when implemented. Production promotion retains its separate existing acceptance
policy; this staging transition does not certify production readiness.

## Mandatory proof before live migration

Each staging deployment and explicit rollback pulls immutable private ECR images and
runs their real entrypoints against disposable PostgreSQL 17 in an internal Docker
network with no published ports. Only synthetic records and separate migration/runtime
roles are used. No staging database, user data, Cognito session or AWS credentials enter
these test containers.

The check seeds the old schema, migrates the disposable database, and verifies readiness,
old and typed analysis reads/writes, original payload preservation, session references, owner
isolation and immutability. The feature reader must still recognize legacy results as
legacy. Bridge preparation also tests both database heads. The workflow retains a
credential-free JSON report as `staging-rollback-compatibility` and embeds it in the
private deployment audit.

The deployment driver binds proof to the candidate digest, actual previous task/image,
service counts, environment, account and workflow revision. Missing/failed proof or
service drift stops before live migration. A promotion also requires one completed
source deployment with matching running/desired counts and no pending tasks, both
before testing and immediately before DDL, so older containers cannot still be draining.
Explicit recovery remains available when the current application is unhealthy. Explicit rollback requires fresh proof of
its audited target against the retained feature-schema image, even when the current
application has already rolled back to the bridge. Recovery never infers the database
head from an old application. This conservatively forbids restoring the original
pre-bridge image through explicit rollback, including before expansion. The original image fails this test after
schema expansion; selecting an old healthy audit cannot bypass it.

PR CI builds an old-source fixture from the pinned image's exact source commit and tests
both phases without AWS credentials. This checks the source contract, not the private
historical image bytes. The protected release preflight always tests actual ECR digests.
Local actual-image test results, when available, are recorded separately in the PR.

## Data compatibility and recovery

Migration 0008 repairs forward; previously applied revisions remain unchanged. It restores
`graph` from the legacy archive, uses an empty compatibility object for new typed rows,
and archives writes made by the old application after rollback. A trigger supplies the
legacy unavailable-summary reason required by new readers. Existing IDs, ownership,
sessions and immutable revision rules are retained. The compatibility graph is never
interpreted as typed analysis or exposed in the current analysis API.

This bridge is temporary and deliberately rejects any untested migration head. Before
another schema change, review the next rollback contract. Do not downgrade the database
or remove the compatibility column while a deployed rollback image still requires it.

After the feature deployment, verify public `/api/v1/ready`, browser completion and
sign-out pages, then manually test sign-in, signup and recovery using approved staging
accounts and installed Windows/macOS packages. These live acceptance checks remain
separate from the disposable compatibility test. Record failures and fix through a
reviewed PR before another promotion.
