# Desktop and backend releases

Releases are explicit operator actions from reviewed main. Pull requests only build
unsigned preview packages and test the real installed/extracted application. They never
receive AWS, signing or notarization credentials. No silent updater is installed.

## Native packages

package-desktop.yml builds a per-user Windows x64 Inno Setup installer and a native macOS
application ZIP with PyInstaller 6.22.3/Python 3.12. Architecture is the runner's native
architecture and is recorded in manifest.json; universal2 and Windows ARM are not claimed.
The frozen GUI is exercised with isolated preferences before packaging and again from
the installed/extracted artifact. Windows smoke checks require a disposable runner with
no existing Buddy installation. Preview artifacts are unsigned and are not production
releases. Support is limited to the Windows/macOS runner architectures that pass CI.

Publish desktop channel uses protected GitHub environments release-staging and
release-production. Configure reviewers and main-only deployment branch rules. Supply
environment variables BUDDY_RELEASE_ROLE_ARN, BUDDY_AWS_REGION, BUDDY_AWS_ACCOUNT_ID,
and BUDDY_RELEASE_BUCKET from the corresponding Terraform outputs. AWS uses short-lived
OIDC; the release role can access only that account's private release prefix.

Production additionally requires WINDOWS_SIGNING_PFX_BASE64/WINDOWS_SIGNING_PASSWORD
or MAC_SIGNING_P12_BASE64/MAC_SIGNING_PASSWORD/MAC_SIGNING_IDENTITY and
APPLE_NOTARY_KEY_BASE64/APPLE_NOTARY_KEY_ID/APPLE_NOTARY_ISSUER. These belong only in
protected release-production, never repository-wide secrets or PR/staging contexts.
Windows signs the application before embedding it in the installer, then signs and
verifies the installer. macOS uses Developer ID hardened-runtime signing, Apple
notarization, stapling and Gatekeeper assessment. Missing credentials stop publication.

All final bytes have SHA-256/size manifests and GitHub build provenance attestations.
Verify downloaded artifacts with gh attestation verify <artifact> -R HusseinMoukalled/Buddy
and verify manifest hashes before installation. Private download authorization is a
separate storage feature; this workflow does not expose a public bucket. Artifacts live
under <environment>/desktop/<full SHA>/<platform>/; production manifests use stable,
staging uses preview. Artifacts and manifests are versioned; there is no mutable latest
pointer or automatic client promotion. Desktop packages declare backend API v1; feature
changes must update compatibility checks before any incompatible release.

Rollback means explicitly reinstalling a verified previous package that supports the
running API. Preserve account vault entries and owned data; never restore a bundled
database or overwrite user settings. Installation/reinstallation is user initiated.

## Backend promotion and rollback

Configure protected staging/production GitHub environments with main-only branch rules,
reviewers, BUDDY_DEPLOY_ROLE_ARN, BUDDY_AWS_REGION, BUDDY_AWS_ACCOUNT_ID and
BUDDY_DEPLOYMENT_CONFIG. The latter is public JSON containing environment, account_id,
region, cluster (buddy-staging/production), bucket (the corresponding release bucket),
api_url (HTTPS origin), tasks (API/migration and optional worker task-definition ARNs),
and network (private subnet IDs, migration security group IDs, assignPublicIp: DISABLED).
Use #27's output identifiers; do not include secret values. Infrastructure bootstrap
must already have initialized database roles/secrets and task definitions.

The workflow builds the exact checked main revision using the reviewed Dockerfile,
pushes an immutable run-specific tag to this environment's ECR registry with OIDC,
and deploys its resolved @sha256 digest. The standalone driver also accepts reviewed
digests for operator recovery. There is no user-supplied API image override in the
workflow, mutable-tag deployment or cross-account/cross-environment image promotion.
Supply worker_image only when a real reviewed worker task/image exists with a container
health check. #8 owns that implementation; the release never invents a worker image.

Backend promotion and rollback registers tagged definitions with reviewed digests, runs
an explicit one-shot migration, waits for exit code zero, then promotes services. It
requires stability, exact task-definition/count agreement and verified HTTPS readiness.
ECS circuit-breaker rollback or drift is a failed deployment, not a successful waiter.
Failed readiness restores previous definitions/counts and checks recovery. Failure to
recover records rollback_failed. Migration failure does not promote traffic. No path
automatically downgrades the database: use expand/contract changes compatible with both
old/new code, reviewed schema checks and isolated backups before rollout.

Promotion outcomes and explicit rollback attempts have immutable audit objects under
<environment>/backend/operations/<operation SHA>/<unique operation ID>.json. Conditional
S3 writes prevent replacing an existing history object. Records include operation type,
ID, current workflow revision, account/environment and start/completion timestamps.
Logs include the exact saved object key on success and failure, so operators can fetch
the retained record with scoped GetObject access without bucket-wide list permissions.
Promotion records retain previous/target definitions and counts plus migration revision.
Rollback records identify the target deployment SHA and source audit key, the state before
rollback and the requested state. Only a verified healthy rollback records `restored`;
failures record `rollback_failed`, the failing stage and observed service state when
available, without leaking arbitrary exception text. No rollback executes migration/DDL.

<environment>/backend/<deployment SHA>.json is only a compatibility lookup for the latest
healthy promotion of that SHA. It points to its immutable audit object; failed promotions
and rollback operations never overwrite it. Repeated healthy deployments may update the
lookup but retain every earlier attempt's history. Rollback input names a healthy
deployment whose previous state should be restored and validates account/environment/task
family before changing services. Recovery does not require approving the current app
build as production-ready. Audit-write failures fail the command rather than reporting a
fully recorded release; operators must inspect service state if a write fails after a
service change. AWS/bootstrap/signing credentials and real sessions never enter records.

Every new promotion requires successful CI and infrastructure checks on exactly the
current main SHA. Production also requires environment variables
BUDDY_STAGING_ACCEPTED_REVISION equal to that SHA and BUDDY_CALLBACK_RELEASE_APPROVED=true.
Set these only after actual staging provider/recovery acceptance and #5's production
callback integration review. An operator must exercise migration failure, readiness
failure, rollback and backup restoration in staging and record evidence before promotion.

## Verification and remaining external prerequisites

Run python -m pytest tests/test_release.py for migration-failure, unhealthy promotion,
explicit rollback audit success/failure, repeated-SHA history preservation,
cross-environment rejection, CI and tampering regressions.
Native packaging CI executes actual installers/applications without provider credentials.
Local native packaging requires PyInstaller 6.22.3 and Inno Setup 6 on Windows; use
python scripts/release/build_desktop.py --output packaged --revision <reviewed full SHA>
then python scripts/release/smoke_package.py packaged on a disposable native environment.

No AWS deployment, Apple notarization or trusted Windows signing is claimed until real
protected environments, provider access and signing identities are supplied. Live staging
recovery drills remain pending, as does the real worker implementation. These are
explicit completion gates for #38; passing simulated tests cannot close that evidence gap.
