# Staging deployment and live acceptance (#73)

## Decision and completion boundary

Use one approved AWS account with distinct staging/production roots, state, networks,
databases, buckets, queues, secrets, Cognito clients and protected-environment OIDC roles.
Staging uses `ingress_mode = "aws_https"`, CloudFront's AWS hostname and an internal ALB.
An owned domain can be added later without changing the ingress mode. No worker image
or worker service is supplied by this issue. See [infrastructure](infrastructure.md)
for configuration, the TLS policy limitation and the private HTTP origin boundary.

The issue PR prepares deployment and stays open until live checks and retained recovery
evidence pass. Its closing link drives Project status; do not merge it or close #73 while
these gates are pending. Build the API image only from reviewed main with passing checks.
An operator may apply infrastructure from the exact teammate-reviewed PR head using a
reviewed saved Terraform plan, after spending approval; never apply an unreviewed diff.
Record both the infrastructure head and the separately reviewed API revision in evidence.

## Operator prerequisites

- Confirm account/region with STS before every apply. Use MFA and short-lived login.
- Review the actual saved bootstrap and environment plans, including cost, IAM and
  public/private boundaries. No apply is authorized by a successful mocked plan.
- Agree an operating budget and test window before creating billable infrastructure.
  Confirm Free-plan service eligibility; upgrading the account is a separate decision.
- Install Terraform 1.13.5, AWS CLI v2 and Docker where permitted. Local HashiCorp downloads
  may be region-restricted. Existing GitHub infrastructure CI can validate changes; it
  does not provision resources, export a live plan or grant permission to bypass restrictions.
- A repository administrator must configure protected `staging`/`production` and
  `release-staging`/`release-production` environments: reviewers, prevent self-review,
  protected-main deployment rules, and the public variables in [releases](releases.md).
  AWS AdministratorAccess does not grant GitHub repository administration.

AWS CLI `aws login` credentials may be newer than the pinned Terraform/boto3 SDKs.
If they cannot consume `login_session`, configure a separate tools profile whose
`credential_process` runs the installed AWS CLI's `configure export-credentials
--profile buddy-staging --format process`. Let the SDK consume its stdout internally;
never paste credential JSON into chat, logs, state or files. The source login profile
must remain separate to avoid recursive credential resolution. Session expiry requires
another `aws login`; do not replace this with long-lived access keys.

## Cost and cleanup plan

The initial Frankfurt Price List lookup on 2026-10-17 gives approximately $92/month for
one NAT gateway, the ALB, a db.t4g.micro database and the 0.5-vCPU/1-GB API task at 730 hours.
Allow roughly $110–140/month for low-traffic always-on staging after storage, the NAT
IPv4 address, two WAF ACLs/rules, KMS/secrets and small logs/request usage; traffic,
backups, restore drills, taxes and price changes can increase this. This is an estimate,
not a spending cap. Credits offset eligible charges, not the underlying resource cost.
The account initially reported $100 earned credits, not a guaranteed $200 balance.

Before applying, configure notifications against **gross usage including credit-covered
usage** as well as remaining credits, with agreed thresholds and a recipient. AWS Budget
alerts do not enforce a hard spending limit. A short test window still incurs retained
storage/key/snapshot charges. Scaling ECS to zero does not stop NAT, ALB or RDS billing.
Do not create production during staging acceptance.

At the end of the test window, explicitly review whether to retain staging. Inventory
resources by environment tags and Terraform state. A reviewed teardown must account for
RDS deletion protection/final snapshots, Cognito deletion protection and S3/state
`prevent_destroy`; these intentionally stop a blind destroy. Export permitted evidence,
retain agreed backups, then remove only approved disposable resources. Never disable
protection or delete shared OIDC/state as an automatic cleanup shortcut.

Pricing sources: [VPC](https://aws.amazon.com/vpc/pricing/),
[Fargate](https://aws.amazon.com/fargate/pricing/),
[load balancing](https://aws.amazon.com/elasticloadbalancing/pricing/),
[WAF](https://aws.amazon.com/waf/pricing/).

## Provision and deploy

1. Follow the bootstrap and environment phases in [infrastructure](infrastructure.md).
   Use separate state/workspaces, the real account ID and `ingress_mode = "aws_https"`;
   omit `api_domain`/`zone_id`. Reuse the account OIDC provider for the second environment.
2. Review/apply the initial infrastructure with images absent and services stopped.
   Confirm CloudFront/VPC origin deployed, private ALB and DB, buckets and secrets.
3. Build the exact reviewed main SHA, push to staging ECR with the full SHA in its immutable
   tag, and retain build provenance plus the resolved image digest. Apply that digest with
   services stopped. Never use a mutable tag or a production repository image.
4. Run the operator-only bootstrap task and require exit code zero. Run migration twice
   and require zero each time. Never print the master/scoped database passwords.
5. Start the API, require ECS stability and exact running definition/count, then verify
   `deployment.api_url` from Terraform, using its trusted certificate without TLS bypasses.
6. Populate protected GitHub variables using `deployment_configuration` and public identity
   outputs. Readiness success alone does not mean authentication or recovery passed.

Export `terraform output -json` to a local ignored `.codex-temp` evidence directory, then
run this **read-only** verifier with the tools profile:

```powershell
python infra/staging/verify.py <outputs.json> --profile buddy-staging-tools `
  --revision <reviewed-full-main-sha> --image <staging-ecr-uri@sha256:digest>
```

It verifies account, exact stable task/image/revision-tag agreement, available private
encrypted protected RDS, private ALB, private versioned encrypted buckets, deployed
CloudFront with uncached HTTPS/WAF, and HTTPS readiness including the migrated database.
It exits nonzero on failure and never retrieves secret values or mutates resources.
Its JSON explicitly lists the **remaining** acceptance work; it is not a completion stamp.
Keep output in the single local `.codex-temp` folder and attach sanitized evidence to #73.

## Required live evidence (all pending until executed)

- **Identity/TLS:** actual signup, verified email, code/PKCE login, recovery, rotation and
  revocation from the desktop against staging. Verify wrong issuer/client and missing
  authentication fail. Record public configuration; retain no passwords or tokens.
  Verify certificate hostname/trust and negotiated TLS 1.2+ from supported clients;
  record the AWS default-certificate minimum-policy limitation for reviewer acceptance.
- **Private service access:** run an operator-controlled disposable task using the actual
  API identity/network. Write/read/delete a unique synthetic S3 object, send a unique
  synthetic queue message and confirm it with a scoped operator. Verify anonymous/public
  access fails and unencrypted transport is denied. Clean only those test keys/messages;
  never purge a shared queue. Verify runtime roles cannot read master/other-role secrets
  or write Alembic metadata. Confirm DB TLS verification succeeds and plaintext fails.
- **Durability/streaming:** create synthetic account-owned persisted data, replace the
  running API task without deleting the database, wait for readiness, then verify the
  same data with its owner. Verify a second account is denied. Test authenticated SSE
  incrementally through CloudFront, including inactivity/reconnect behavior and terminal
  events; a buffered final response is not a streaming pass. Clean the synthetic records.
- **Migration rejection:** run a disposable, reviewed intentionally failing migration
  task against an isolated acceptance DB/schema. Run the actual promotion failure path;
  require nonzero exit, unchanged healthy service definition/count and retained immutable
  `migration_failed` audit. Do not modify the live Alembic version or introduce broken
  migrations into main merely to manufacture a failure.
- **Rollback:** make two compatible reviewed API deployments, retain definitions, counts
  and audit keys, then use the real rollback driver to restore the previous deployment.
  Require stable tasks and verified HTTPS readiness plus a `restored` immutable record.
  Never downgrade the database automatically. Exercise readiness failure/recovery too.
- **Restore:** create a snapshot or use available PITR after synthetic data exists;
  restore to a uniquely named isolated private encrypted RDS instance with restricted
  security groups. Verify schema and the synthetic owned data over verified TLS before
  deleting the disposable restored instance under the reviewed cleanup plan. Never
  point the staging application at the restored DB as part of the drill.
- **Operations:** verify actual protected-environment approval, main-only deployment,
  gross-spend/credit alerts and alert receipt. Record resource IDs, UTC timestamps,
  reviewed SHA/digest, commands with sensitive arguments omitted, actual outcomes,
  failure/recovery audit keys, costs and resource retention decisions.

Only after every applicable check has real evidence and teammate review may #73 be
completed. Missing access, spending approval, failed checks or unexecuted drills remain
explicit blockers; mocked tests and this checklist cannot replace them.
