# Staging deployment record — issue #73

Deployment date: 17 October 2026, AWS account `549327541867`, Frankfurt
(`eu-central-1`). Staging is retained on the **Free account plan**. The requester
authorized deploying the issue branch before teammate review so reviewers can inspect
the running result. This record does not authorize merging or production deployment.

The live API is **https://d3n3ijrboj39jr.cloudfront.net**. `/health` is liveness;
`/api/v1/ready` verifies the real migrated database. Public desktop settings are in
[`public-config.json`](../infra/staging/public-config.json). These values are public
identifiers, not credentials; the desktop never receives database/provider secrets.

## Provenance and retained evidence

The deployed API source is reviewed main
`dc1bb7743f9fa95e5fd75fe7a488ca5262c06b75`. The issue branch supplies the container
packaging and infrastructure fixes. The applied infrastructure revision is
`9e5cf3fa63f7ceb39e0bb79007a3204d1f912b15`; subsequent evidence/documentation commits
do not change the deployed Terraform configuration. Image A's recorded build-definition
revision is `2800181ab4d773611b40fa36368be9418e4dca37`.

Final selected image A:
`549327541867.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api@sha256:f61c966f16ebd4ea59331ae3f54b7034c98b1e91d6716a078ae9f889041a9593`.

The compatible rollback-test image B has digest
`sha256:b2146f843f8d56495944ac8855f3bbf57bef379f2122f5d4efac5b65c47f0e22`, built
with definition `bfe386b0ac1a3d23d6de7feb692af4ade096b8fe`. The two builds use the
same reviewed API source; they are distinct immutable builds, not different API features.

The allow-listed [`deployment-evidence.json`](../infra/staging/deployment-evidence.json)
contains actual task/resource identifiers, UTC timestamps, recovery audit keys and final
state. Terraform plans/state and operator logs stay private. Local scratch remains under
`.codex-temp`; the encrypted/versioned operator evidence bucket retains the saved plans
and provenance. Release audit objects remain in the private releases bucket under
`staging/backend/operations/<reviewed-source-sha>/`. No passwords, tokens, private signup
addresses, credential exports or raw Terraform state belong in this record.

Gitleaks initially classified the public reviewed-source Git SHA under the metadata
label `reviewed_api_revision` as a generic API key. The field is now named
`reviewed_source_revision`. `.gitleaksignore` permits only that exact historical
commit/file/rule/line fingerprint; no rule or evidence-directory exclusion was added.
The value was verified identical to the public image-source commit above.

## Live verification

- Bootstrap of scoped database roles succeeded. Explicit migration ran twice with exit
  zero. API SQL sessions verified TLS and restricted runtime privileges; plaintext DB
  connections failed. Runtime roles cannot read the master/other-role secrets or mutate
  Alembic metadata.
- The stable Fargate API uses the selected private ECR digest. HTTPS certificate/hostname
  verification passed with TLS 1.3; public plaintext requests returned 403. RDS is private,
  encrypted and deletion protected. The ALB is internal; task ingress permits its security
  group only. Versioned KMS-encrypted object buckets deny public access. No NAT gateway or
  worker service is running.
- A disposable task with the real API identity wrote/read/deleted a unique S3 object and
  sent an SQS message. The operator received/deleted only the matching message. Anonymous
  HTTP/HTTPS object requests failed. Anonymous HTTP/HTTPS queue reads returned 403; the
  actual queue is encrypted and its resource policy denies insecure transport. Test
  object versions and delete markers were removed.
- Disposable Cognito identities exercised real SRP authentication/GetUser, refresh-token
  rotation, replay rejection and provider revocation. Actual tokens resolved to persisted
  account IDs through HTTPS; missing auth returned 401. Another account received 403 for
  owned deck and stream access. Synthetic Cognito identities were deleted.
- Actual SSE snapshots arrived at approximately 0.391, 5.703 and 11.687 seconds and ended
  with a terminal event. Replacing the running API task preserved the same account UUID,
  owned deck and cross-account denial. This is a short incremental-stream/restart check.
- A snapshot was restored into a separate private encrypted RDS instance. A task verified
  the migrated schema, owned deck and ended session over TLS. The staging API remained on
  its original database. The disposable restore instance was deleted after verification;
  the encrypted acceptance snapshot is retained.
- An actual Alembic missing-revision command against that isolated restored database
  exited **42**. The existing release driver's migration failure path kept the healthy
  API definition/count unchanged and retained a `migration_failed` audit.
- The real release driver promoted A and B, then rolled back to A with stable HTTPS
  readiness and a `rolled_back` audit. A separate deliberately unready schema candidate
  produced actual ALB readiness 503s; the driver recovered the previous healthy service.
  No database downgrade was performed. Exact audit keys are in the JSON record.
  The initial collector incorrectly expected HTTP access logs, which are disabled; its
  post-recovery log assertion failed. The corrected collection records the observed
  ELB response-code mismatch, three stopped tasks, stable HTTPS/service state and the
  retained rollback audit. It does not claim access-log evidence.
- The initial final Terraform reconciliation exposed an ownership defect: it reset the
  API service from promoted revision `:3` to the configured template revision `:1` (both
  used image A at that time). PR #84 now gives the release driver ownership of only the
  service `task_definition` attribute; Terraform still manages counts and other service
  settings. A Terraform mocked-provider regression test promotes a newer revision, then
  runs a plan with the older configured revision and checks it does not propose a rollback.
  Synthetic source-database records and temporary operator provisioning access were
  removed after acceptance. The final-state record distinguishes retained resources from
  disposable ones; the original live no-change plan predates this ownership fix.

## Configuration, costs and recovery limits

Staging keeps the internal ALB, 0.5-vCPU/1-GB Fargate API, PostgreSQL 17
`db.t4g.micro` with 20-GB encrypted gp3 storage, S3, SQS, Cognito, WAF, secrets and keys.
Public task IPs provide outbound access with ALB-only API ingress. Production is absent.
Automated database retention is **one day**: AWS rejected seven days under this account's
Free plan. Production configuration retains 35 days. This is a shorter staging recovery
window, not disabled backups. The isolated restore drill proves the retained snapshot can
recover the verified synthetic data.

The documented low-traffic planning allowance is **$75–105/month before credits/taxes**,
not a promise of free infrastructure or a spending cap. Usage paid by credits still
consumes credits. The account-wide gross-usage budget is $100/month with actual-cost
alerts at $25, $50, $75 and $100; email subscribers are configured privately. Staging stays
running as requested. Scaling the API down alone does not stop ALB/RDS/storage/key costs.
Retained snapshots, release images/audits and state also remain. Review the runbook before
any teardown; deletion protection and shared state/OIDC must not be bypassed blindly.

The `staging` and `release-staging` GitHub environments require HusseinMoukalled or
yasminahanna, prevent self-review and allow only `main`. Public deployment/release
variables and repository-specific OIDC trust are configured. GitHub's administrator bypass
setting remains enabled and is disclosed; it was not used to run a GitHub deployment.
This initial AWS operator deployment was explicitly authorized by the requester.

## Review limitations

The refreshed, digest-pinned base and OS upgrade removed the original critical findings.
The selected image's ECR scan still reports **0 critical and 7 high** findings. Six concern
util-linux; the workload runs non-root with a read-only filesystem, all Linux capabilities
dropped and no host mounts. These controls reduce relevant local mount/namespace attack
paths; they are not proof that every finding is harmless. The seventh is zlib
[CVE-2026-85091](https://security-tracker.debian.org/tracker/CVE-2026-85091): Debian
still lists the selected Bookworm package as vulnerable/unfixed despite discussion of
the affected upstream version range. It is **not** suppressed as a false positive.
See also Debian's [util-linux tracking](https://security-tracker.debian.org/tracker/CVE-2026-78409).
Reviewers must assess these residual findings before approving the deployed result;
rebuild and rescan when vendor fixes become available.

The AWS default CloudFront certificate uses the fixed `TLSv1` security policy. The
tested client negotiated TLS 1.3 and verified trust/hostname; strict server-side TLS 1.2
minimum enforcement is not claimed. The private CloudFront-to-ALB and ALB-to-task legs
use HTTP inside the VPC. Adding an owned domain later permits a custom ACM certificate
and `TLSv1.2_2021`, following the existing domain-migration runbook.

Full desktop managed-login signup/email verification/PKCE/password recovery/vault,
long-idle desktop SSE reconnect, actual budget-email receipt and a normal protected
GitHub environment approval run were **not exercised**. They are not implied by these
foundation results. The first normal workflow deployment follows PR review/merge;
account/desktop/release features retain their own acceptance work. No worker or production
acceptance is claimed. Issue closure still follows teammate review, passing current-head
CI and an explicitly requested merge.
