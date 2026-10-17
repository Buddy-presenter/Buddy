# AWS infrastructure (#27)

Selected stack: Terraform 1.13.5, AWS provider 6.19.0, Cognito managed login, ECS Fargate,
PostgreSQL RDS, S3, SQS, CloudFront private VPC origins, optional ACM/Route 53 and WAF. The default region is Frankfurt
(`eu-central-1`); both environment roots allow an explicit different region. Buddy is
still a PySide6 desktop product. There is no browser application deployment.

## Isolation and service boundaries

`infra/terraform/environments/staging` and `production` have separate roots and remote
state in one approved AWS account. Providers reject an unexpected account; environment
resource names, roles, CIDRs and state remain distinct. Account administrators are a shared
trust boundary: this is resource/IAM isolation, not an account-level security boundary. Each environment owns its network,
database, user pool/client/domain, KMS keys, secrets, buckets, queue and service identities.
The account GitHub OIDC provider is shared; its protected-environment trust subjects and
permissions are not. No production resources are created by staging provisioning.
Both roots default to `ingress_mode = "aws_https"`; no owned domain or zone is required.

| Component | Configuration |
| --- | --- |
| Networking | Two public NAT subnets, two private service subnets, two isolated database subnets; RDS and the AWS HTTPS ALB remain private. Staging tasks use public IPs for outbound HTTPS; only the ALB security group can connect to the API, and migration/worker groups have no inbound internet rule. Production tasks retain private addresses. Production uses two NAT gateways; staging defaults to zero (`service_egress_mode = "public_ip"`). The optional staging `private_nat` mode uses one. Security groups allow CloudFront origin traffic to the internal ALB, then only ALB to API and service-to-database traffic. Service internet egress is HTTPS for AWS/provider calls. |
| TLS/real-time ingress | AWS HTTPS mode uses the AWS-owned `*.cloudfront.net` certificate, HTTPS-only viewer requests and private ENIs to an internal HTTP ALB; API and error caching are disabled. Authentication headers, cookies, query strings and all API methods are forwarded. CloudFront read timeout is 60 seconds, ALB idle timeout 120 seconds; live SSE timing/reconnection is an acceptance gate. Direct custom-domain mode retains the TLS 1.2/1.3 ALB listener and HTTP redirect. Actual session/event protocol is owned by its feature issues. No ALB access logs containing token-bearing paths are enabled. |
| API | Digest-pinned image, non-root read-only task, dropped capabilities, environment-specific egress, readiness checks, deployment circuit breaker/rollback. Two replicas in production, one staging. ECS task IPs are added to exact allowed hosts by the container entrypoint so ALB health checks work; forwarded headers remain untrusted. |
| Database | PostgreSQL 17, forced TLS and client certificate verification, KMS encryption, private security group, managed master secret, separate API/migration/worker DB roles. Production multi-AZ/35-day PITR backups; staging 1 day. Deletion protection and final snapshot required. |
| Private objects | Separate KMS-encrypted data and release buckets; all public access/ACLs blocked; TLS-only policies, versioning, bounded old-version retention (35/7 days) and abandoned multipart cleanup. |
| Processing | KMS-encrypted SQS with one-day retention, 15-minute visibility, three-attempt dead-letter handling. Worker has a distinct task role, database login and execution role. Only a real supplied worker image creates a service. |
| Identity | Public Cognito desktop client, code grant only, five-minute access/ID tokens, one-day rotating refresh tokens, zero replay grace, revocation, verified-email recovery, 12-character password policy and suppressed user-existence errors. Separate edge and regional WAF rate limits protect the CloudFront viewer IP and Cognito respectively (direct-domain mode protects ALB and Cognito); request sampling is disabled. |
| Workload identities | ECS task roles and repository/environment-bound GitHub OIDC roles; no AWS access keys or client secrets in images/installers. API/worker cannot access release artifacts or the RDS master secret. Migration has no object/queue permissions. Bootstrap alone can read master credentials and write the three scoped DB secrets. |

The shared WAF rule allows 300 requests per client IP per minute before blocking. Tune
this only with workload measurements; it is not a claim that feature-specific quotas
exist. AWS quotas and email deliverability must be verified before user acceptance.

## Prerequisites and current limits

AWS CLI access is available for staging; no deployment or live acceptance is implied by
this configuration. See [staging acceptance](staging-acceptance.md) for the approved
hostname decision, cost review and per-criterion evidence. Both environments use one
account, separate state and environment-scoped resources. Supply the real account ID,
region and short-lived credentials; never invent a second account ID or create access keys.
The edge provider uses `us-east-1` for CloudFront WAF and optional edge ACM only; the API,
RDS, objects, queues and identity stay in the selected workload region.

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

1. Install Terraform 1.13.5, AWS CLI v2 and Docker. Authenticate to **staging** with `aws login --profile buddy-staging` or SSO.
   Verify `aws sts get-caller-identity` matches the intended account before each phase.
2. Initialize `infra/terraform/bootstrap`. Use a separate local workspace/state for each
   environment (`terraform workspace new staging` or `production`). Pass `account_id`,
   `environment` and `region` explicitly, inspect a saved plan, then apply that plan.
   This creates the private encrypted versioned state bucket and account GitHub OIDC
   provider. If the account already has that provider, pass its ARN as
   `existing_github_oidc_provider_arn`. The staging bootstrap owns a newly created provider;
   the production bootstrap reuses it and never manages a second copy. Keep bootstrap state secure and backed up;
   `.tfstate` files must never enter Git.
3. In the environment root, create an ignored `backend.hcl` with the bootstrap outputs:
   `bucket`, `kms_key_id`, `region`, `key = "environment.tfstate"`, `encrypt = true`,
   `use_lockfile = true`, and `allowed_account_ids = ["<that account ID>"]`.
   Run `terraform init -backend-config=backend.hcl`. No credentials belong in this file.
4. Create ignored `deployment.auto.tfvars` with the required `account_id`,
   `region`, `ingress_mode = "aws_https"`, `service_egress_mode = "public_ip"` and `github_oidc_provider_arn`.
   Omit `api_domain` and `zone_id`; the obsolete `other_environment_account_id` input
   has been removed. Initially leave images null and services stopped. Run
   `terraform plan -out=initial.tfplan`, review it, then `terraform apply initial.tfplan`.
   The CloudFront distribution and VPC origin must finish deploying (allow 15 minutes).
   An optional custom hostname also requires ACM DNS validation. This phase creates ECR, networking, data and identity.
5. Build `docker build -f infra/container/Dockerfile -t buddy-api:<reviewed-revision> .`
   from the reviewed combined main after required feature PRs are approved and merged.
   Authenticate to the output ECR URI with `aws ecr get-login-password` piped to
   `docker login --username AWS --password-stdin`, tag/push, and read the registry's
   digest. Set `api_image` to that immutable ECR URI plus `@sha256:<digest>`; review/apply
   another plan. The container contains public configuration/code and trusted CA roots,
   never passwords or provider keys. Dependency/base-image refreshes require rebuild
   and review; deployment reproducibility comes from the reviewed image digest.
6. As an operator, launch the output `bootstrap_task` once with `aws ecs run-task`,
   `launch-type FARGATE`, the output cluster/`task_subnets` and **migration** security
   group, with `assignPublicIp` matching the `task_public_ip` output (ENABLED for low-cost staging, DISABLED for private NAT). Wait for STOPPED and require exit code **0**. It
   obtains the RDS-managed master secret through its scoped task role, generates and
   stores three database passwords in Secrets Manager, and creates restricted roles.
   Retrying reuses passwords after a partial failure. No secret values are printed.
7. Launch output `tasks.migration` with the same selected task network. Require exit code 0;
   it upgrades the schema and removes runtime write access to Alembic metadata. Run
   it a second time to verify idempotence. Applications never run DDL on startup.
8. Set `start_services = true`, review/apply the plan, wait for ECS stability, then check
   `<deployment.api_url>/api/v1/ready` and perform actual Cognito/signup/recovery/rotation/
   revocation acceptance. Inspect task logs for sanitized startup failures. A failed
   health check rolls back the ECS deployment; do not bypass readiness.
9. Repeat in the separate production root/state in the same account only after staging acceptance and review.
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

RDS backup retention is 35 days in production/1 in staging; final snapshots persist until
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
terraform -chdir=infra/terraform/modules/environment test
python -m pip install -e ./shared
python -m pip install -e './backend[api,dev]' boto3==1.40.50
python -m pytest -p no:pytest-qt infra/tests
```

The isolated database integration is enabled only in CI with `BUDDY_BOOTSTRAP_TEST=true`.
Run init/validate for `bootstrap`, `environments/staging` and `environments/production`.
Validate the reusable module through those consuming roots, which supply `aws.edge`;
its mocked tests supply the regional and edge providers without AWS access.
No paid providers, real deck material or real AWS secrets belong in validation.

Sources: [Terraform provider mocks](https://developer.hashicorp.com/terraform/language/tests/mocking),
[Cognito client configuration](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cognito_user_pool_client),
[Cognito callback constraints](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html).

## AWS HTTPS and adding a domain later

CloudFront is used instead of API Gateway HTTP APIs because Buddy already exposes SSE;
HTTP APIs buffer responses. CloudFront private origins retain the ALB/ECS service model,
unchanged API paths and a private origin. AWS added VPC-origin WebSocket support in May
2026; feature-specific live acceptance is still required. Public HTTP viewer requests
are rejected, not served or redirected. Database TLS verification is unchanged.

AWS fixes the default `*.cloudfront.net` certificate policy to `TLSv1`; it negotiates
modern TLS but cannot enforce a server-side TLS 1.2 minimum with that default certificate.
This staging-specific limitation must be recorded in deployment review; do not claim
strict TLS 1.2-only server enforcement. Buddy's Python HTTPS clients verify certificates
and use their modern default TLS minimum. An owned domain enables `TLSv1.2_2021`.
The private CloudFront-to-ALB leg is HTTP over service-managed VPC ENIs, as is the existing
ALB-to-task leg; there is no public plaintext origin. No request/access logs or WAF
samples containing auth/query data are enabled.

Later, keep `ingress_mode = "aws_https"` and set `api_domain` plus `zone_id`. Terraform
adds a DNS-validated ACM certificate in us-east-1 and a Route 53 alias to the same
CloudFront distribution. The AWS hostname remains in allowed hosts during client
migration. Verify both addresses with normal SNI-capable clients before updating desktop
configuration. Avoid changing to direct ALB ingress during this domain-only change;
that mode changes ALB placement. Certificate/alias updates are reviewed plans, not an
assumption of zero interruption.

Sources: [CloudFront private origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html),
[viewer certificates and policy](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesGeneral.html),
[response streaming support](https://docs.aws.amazon.com/apigateway/latest/developerguide/response-transfer-mode.html).

## Staging cost controls

Staging defaults to public task egress with **zero NAT gateways/EIPs**, while retaining
an internal ALB, private encrypted RDS, API-only load-balancer ingress, TLS, WAF and all
existing CPU/memory/database capacity. API tasks, temporary bootstrap/migration tasks,
and any future worker use the selected task subnets and public-IP output. Public IPs
are an explicit network-boundary tradeoff, not a claim of private task addressing.
Security groups deny direct inbound internet traffic; the DB's rules still reference
only the service security groups. Do not add an internet-facing API rule to make this work.

Production rejects public task egress. An operator can restore staging's prior private
address design using `service_egress_mode = "private_nat"`, with NAT charges returning.
Do not substitute a fleet of paid interface endpoints as a presumed saving without a
new cost comparison. The migration driver defaults legacy configurations to private NAT
and requires an explicit staging-only `egress_mode = "public_ip"` to accept ENABLED.

Optional Container Insights is disabled in staging. Standard Fargate service CPU/memory
metrics, task logs, load-balancer health checks and deployment rollback are retained.
Workers remain absent, and production is not provisioned by staging. No Spot capacity,
CPU/memory downsizing, database consolidation or backup reduction is used for savings.
