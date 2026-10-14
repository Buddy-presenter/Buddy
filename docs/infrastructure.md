# AWS infrastructure (#27)

Selected stack: Terraform 1.13.5, AWS provider 6.19.0, Cognito managed login, ECS Fargate,
PostgreSQL RDS, S3, SQS, ACM/Route 53 and WAF. The default region is Frankfurt
(`eu-central-1`); both environment roots allow an explicit different region. Buddy is
still a PySide6 desktop product. There is no browser application deployment.

## Isolation and service boundaries

`infra/terraform/environments/staging` and `production` have separate roots and remote
state, and must use different AWS accounts. Providers reject an unexpected account;
the other environment's account ID must differ. Each environment owns its network,
database, user pool/client/domain, KMS keys, secrets, buckets, queue and service identities.
Use separate delegated Route 53 zones and API hostnames in those accounts.

| Component | Configuration |
| --- | --- |
| Networking | Two public ALB/NAT subnets, two private service subnets, two isolated database subnets; no public addresses on tasks or RDS. Production uses two NAT gateways; staging one. Security groups allow ingress only from ALB to API and service-to-database traffic. Service internet egress is HTTPS for AWS/provider calls. |
| TLS/real-time ingress | DNS-validated ACM certificate, TLS 1.2/1.3 HTTPS listener, HTTP redirect, ALB WebSocket support with 120-second idle timeout. Actual session/event protocol is owned by its feature issues. No ALB access logs containing token-bearing paths are enabled. |
| API | Digest-pinned image, non-root read-only task, dropped capabilities, private addresses, readiness checks, deployment circuit breaker/rollback. Two replicas in production, one staging. ECS task IPs are added to exact allowed hosts by the container entrypoint so ALB health checks work; forwarded headers remain untrusted. |
| Database | PostgreSQL 17, forced TLS and client certificate verification, KMS encryption, private security group, managed master secret, separate API/migration/worker DB roles. Production multi-AZ/35-day PITR backups; staging 7 days. Deletion protection and final snapshot required. |
| Private objects | Separate KMS-encrypted data and release buckets; all public access/ACLs blocked; TLS-only policies, versioning, bounded old-version retention (35/7 days) and abandoned multipart cleanup. |
| Processing | KMS-encrypted SQS with one-day retention, 15-minute visibility, three-attempt dead-letter handling. Worker has a distinct task role, database login and execution role. Only a real supplied worker image creates a service. |
| Identity | Public Cognito desktop client, code grant only, five-minute access/ID tokens, one-day rotating refresh tokens, zero replay grace, revocation, verified-email recovery, 12-character password policy and suppressed user-existence errors. WAF rate limit protects both ALB and managed login; request sampling is disabled. |
| Workload identities | ECS task roles and repository/environment-bound GitHub OIDC roles; no AWS access keys or client secrets in images/installers. API/worker cannot access release artifacts or the RDS master secret. Migration has no object/queue permissions. Bootstrap alone can read master credentials and write the three scoped DB secrets. |

The shared WAF rule allows 300 requests per client IP per minute before blocking. Tune
this only with workload measurements; it is not a claim that feature-specific quotas
exist. AWS quotas and email deliverability must be verified before user acceptance.

## Prerequisites and current limits

No AWS account credentials, domains, hosted zones or running Docker daemon were available
locally. Nothing has been deployed. Terraform validation/mocked plans establish structural
boundaries, not actual cloud IAM, certificate, backup-restore or provider acceptance.

An operator must supply two AWS account IDs/SSO profiles, account-owned Route 53 zone IDs,
API hostnames and AWS permissions to create these resources. Log in through AWS SSO or
another short-lived role session; do not create permanent access keys for this process.
The region must support the selected services and RDS instance classes in that account.

The repository has no processing-worker implementation. Queue/IAM infrastructure is
real, but `worker_image = null` deliberately leaves the worker service absent. Supply the
worker issue's reviewed image digest when implemented; do not deploy a pretend worker.
`api_image = null` similarly permits the initial ECR/network/data provisioning before
the first image can be built and pushed. `start_services = false` keeps defined services
stopped until database initialization and migration succeed. Missing images never imply
a successful deployment.

The Cognito localhost callback agrees with #5's development flow. AWS documents that HTTP
localhost exception for testing. Production installer callback registration/HTTPS handoff
must be reviewed before release, with matching client and desktop changes. Production
rollout also requires #5's live Cognito acceptance and all applicable security-baseline
release controls; provisioning infrastructure alone does not satisfy those controls.

## Reproducible provisioning

1. Install Terraform 1.13.5, AWS CLI v2 and Docker. Authenticate to **staging** through SSO.
   Verify `aws sts get-caller-identity` matches the intended account before each phase.
2. Initialize `infra/terraform/bootstrap`. Use a separate local workspace/state for each
   environment (`terraform workspace new staging` or `production`). Pass `account_id`,
   `environment` and `region` explicitly, inspect a saved plan, then apply that plan.
   This creates the private encrypted versioned state bucket and account GitHub OIDC
   provider. If the account already has that provider, import it into this root instead
   of attempting to create a duplicate. Keep bootstrap state secure and backed up;
   `.tfstate` files must never enter Git.
3. In the environment root, create an ignored `backend.hcl` with the bootstrap outputs:
   `bucket`, `kms_key_id`, `region`, `key = "environment.tfstate"`, `encrypt = true`,
   `use_lockfile = true`, and `allowed_account_ids = ["<that account ID>"]`.
   Run `terraform init -backend-config=backend.hcl`. No credentials belong in this file.
4. Create ignored `deployment.auto.tfvars` with the required `account_id`,
   `other_environment_account_id`, `api_domain`, `zone_id` and
   `github_oidc_provider_arn`. Initially leave images null and services stopped. Run
   `terraform plan -out=initial.tfplan`, review it, then `terraform apply initial.tfplan`.
   ACM DNS validation must complete. This phase creates ECR, networking, data and identity.
5. Build `docker build -f infra/container/Dockerfile -t buddy-api:<reviewed-revision> .`
   from the reviewed combined main after required feature PRs are approved and merged.
   Authenticate to the output ECR URI with `aws ecr get-login-password` piped to
   `docker login --username AWS --password-stdin`, tag/push, and read the registry's
   digest. Set `api_image` to that immutable ECR URI plus `@sha256:<digest>`; review/apply
   another plan. The container contains public configuration/code and trusted CA roots,
   never passwords or provider keys. Dependency/base-image refreshes require rebuild
   and review; deployment reproducibility comes from the reviewed image digest.
6. As an operator, launch the output `bootstrap_task` once with `aws ecs run-task`,
   `launch-type FARGATE`, the output cluster/private subnets and **migration** security
   group, with public IP disabled. Wait for STOPPED and require exit code **0**. It
   obtains the RDS-managed master secret through its scoped task role, generates and
   stores three database passwords in Secrets Manager, and creates restricted roles.
   Retrying reuses passwords after a partial failure. No secret values are printed.
7. Launch output `tasks.migration` with the same private network. Require exit code 0;
   it upgrades the schema and removes runtime write access to Alembic metadata. Run
   it a second time to verify idempotence. Applications never run DDL on startup.
8. Set `start_services = true`, review/apply the plan, wait for ECS stability, then check
   `https://<api_domain>/api/v1/ready` and perform actual Cognito/signup/recovery/rotation/
   revocation acceptance. Inspect task logs for sanitized startup failures. A failed
   health check rolls back the ECS deployment; do not bypass readiness.
9. Repeat in the separate production account only after staging acceptance and review.
   Record actual provision/restore/provider evidence in the PR. This guide does not
   authorize bypassing PR approval or unreviewed production promotion.

Export `terraform output -json` for the public resource IDs and desktop configuration.
Outputs contain secret ARNs, not secret values. DB passwords remain server-side and are
injected by the execution role into the corresponding task; never include them in desktop
configuration. Treat saved plans/state as sensitive even though this configuration does
not generate plaintext passwords in Terraform state.

## Migration, secrets, backups and rollback

API and worker logins have data access without schema creation, superuser, role creation
or Alembic-version writes. Migration owns newly created objects. The privileged bootstrap
task is run only by an operator; GitHub deployment roles cannot launch it or fetch its
master secret. Runtime DB passwords can be rotated in a reviewed maintenance operation:
update the scoped secret, run bootstrap to apply the password, then force deployment of
affected services. Existing connections may survive until drained; verify new connections.
Automatic DB-secret rotation is not configured. RDS manages master-password rotation.

Use backward-compatible expand/contract migrations. Roll back to the previous reviewed
image digest/task definition; do not automatically downgrade a database containing user
data. A failed migration stops promotion. Restore RDS PITR/snapshots into an isolated
instance, verify schema and owned data, then review endpoint/secret changes before cutover.
Bootstrap can reconcile connection URLs to a restored host without changing passwords.
Never disable TLS or deletion protection as a recovery shortcut.

RDS backup retention is 35 days in production/7 in staging; final snapshots persist until
an operator removes them. S3 noncurrent versions remain for 35/7 days after replacement or
deletion, so application deletion cannot promise immediate removal from backups. Current
objects persist until the owning workflow deletes them. Logs retain metadata for 30/7
days; do not log raw user content or identity tokens. Actual restore drills, deletion
exceptions and applicable provider policies remain release evidence, not assumed facts.

## Desktop releases and CI

The private release bucket and `release_channel` output are separate from API/worker
deployment. Use `preview` (staging) or `stable` (production) and the environment-specific
prefix. The release GitHub OIDC subject is `environment:release-<environment>`; backend
deployment uses `environment:<environment>`. Configure these GitHub environments with
reviewers and a protected-main deployment restriction before using the roles. Publishing
installers/signatures/update manifests and private download authorization belong to #38;
there is no unsigned auto-update endpoint or public release bucket here.

`infrastructure.yml` runs formatting, all-root validation, mocked boundary tests, real
PostgreSQL role/migration tests, a Docker build and read-only container startup on PRs and
main pushes. It receives no AWS credentials and performs no cloud writes. This branch
does not change #30's CI consolidation or other issues' workflows. Require both the
infrastructure checks and existing CI to pass before merging; an administrator can make
the additional checks required in the ruleset.

Local read-only checks:

```sh
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform/modules/environment init -backend=false -lockfile=readonly
terraform -chdir=infra/terraform/modules/environment validate
terraform -chdir=infra/terraform/modules/environment test
python -m pip install -e '.[api,dev]' boto3==1.40.50
python -m pytest -p no:pytest-qt infra/tests
```

The isolated database integration is enabled only in CI with `BUDDY_BOOTSTRAP_TEST=true`.
Repeat init/validate for `bootstrap`, `environments/staging` and `environments/production`.
No paid providers, real deck material or real AWS secrets belong in validation.

Sources: [Terraform provider mocks](https://developer.hashicorp.com/terraform/language/tests/mocking),
[Cognito client configuration](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cognito_user_pool_client),
[Cognito callback constraints](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html).
