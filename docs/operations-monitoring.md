# Operational monitoring for uploads and PDF analysis (#39)

## What is measured

The API and worker emit CloudWatch Embedded Metric Format (EMF) events into their existing
service log groups. `Buddy/Operational` contains:

| Signal | Source | Meaning |
| --- | --- | --- |
| UploadRequest | API | Count of accepted or rejected upload initialization/content-transfer requests. |
| JobAttempt / JobOutcome | Worker | Claimed attempts and their success, retry, or dead-letter outcome. |
| DurationMs | API/worker | Upload request, complete job, metadata/manifest work, validation, and Bedrock call durations. |
| StageOutcome | Worker | Metadata inspection, manifest rendering, action validation, Bedrock call, and failure outcomes. |
| InputTokens / OutputTokens | Bedrock response via worker | Provider-reported token use for successful calls. Input usage includes document processing. |
| Queue age/backlog/DLQ | Native SQS metrics | Oldest visible message, visible queue depth, and dead-letter queue depth. |

Dimensions are restricted to environment, service, stage and outcome, with aggregate
dimension sets for service/outcome and service. Request, job, deck, source-revision and
analysis UUIDs, plus the analysis version number, are log properties only, never metric
dimensions. They support log correlation without creating high-cardinality metrics. The
events contain no raw file bytes, summaries, slide text,
provider response, tokens, email codes or filenames. Do not enable Bedrock model invocation
logging for this workload: the request contains the original private PDF.

The worker reports job-level elapsed time and provider-call elapsed time. The current
PDF metadata/render-manifest and action-validation routines do not expose independent
stable timing contracts, so their failures are associated with the job's safe stage and
outcome rather than claiming separate per-function latency metrics. Desktop rendering is
local and is not observed by these server metrics.

Provider token counts are the usage signal for analysis consumption. The dashboard also
plots AWS's account-wide `EstimatedCharges` billing metric in `us-east-1`. Billing metrics
must be enabled in the AWS account (CloudWatch billing alerts) and update several times
daily, so the graph can lag. If the metric is absent, use the existing AWS Budget alerts
while an account administrator enables billing metrics; do not infer zero spend from a
missing graph.
Actual AWS spend is also alerted through the existing account-wide gross-usage budget
(USD 100/month; actual-cost alerts above USD 25, 50, 75 and 100). That budget includes all
account services and is not a per-job or Bedrock-only cost report. Budgets can arrive after
usage, do not cap spending, and credits do not change gross usage. Use AWS Billing/Cost
Explorer for model/service cost attribution; do not infer dollar cost from tokens using an
undocumented or stale price.

## Dashboard and alert thresholds

Terraform creates `${environment}-operations` dashboard, an SNS topic and alarms. Set
`alert_email_recipients` to the approved team destinations in the protected operator
configuration; every recipient must confirm the AWS SNS subscription before delivery is
active. No email address is committed to source control. An empty recipient set leaves
alarms visible in CloudWatch but does not deliver email, so verify subscriptions before
calling alert delivery enabled.

Initial thresholds are intentionally conservative and based on the current worker limits:

| Alarm | Threshold | Operator meaning |
| --- | --- | --- |
| Queue age | Oldest message at least 300 seconds | Work is waiting longer than the normal retry backoff; check worker service health and recent logs. |
| Queue backlog | More than 10 visible messages for 10 minutes | Demand is outpacing the current single staging worker; check its health and error rate. |
| SQS dead-letter queue | At least one visible message | Inspect the sanitized worker logs and queue metadata; do not purge the shared queue. |
| Durable worker dead letter | At least one job in a five-minute period | Check the opaque job ID and safe error/stage, then use the bounded owner retry only after resolving the cause. |
| Worker retry | At least one retry in a five-minute period | Inspect recent stage/provider errors; a transient failure may self-recover, but repeated retries can exhaust the three-attempt limit. |

The worker's configured maximum attempt deadline is 900 seconds; the 300-second queue-age
threshold detects stalled dispatch well before that deadline. The backlog threshold is a
staging starting point, not an autoscaling target. Review these limits against measured
staging traffic after #24 deploys the worker. The AWS account budget remains the spending
alert; there is no hard spend cap.

## Triage and recovery

1. Open the environment's CloudWatch dashboard and identify whether the signal is queue
   delay, a retry, a durable dead letter, an API rejection, or provider latency/usage.
2. Search only the relevant API/worker log group and a narrow UTC time range. Use opaque
   request/job/deck UUIDs to correlate events. Never export document contents or broaden
   log access to the private file bucket.
3. For queue growth or age, check the ECS worker service's desired/running task count,
   deployment health, recent sanitized worker logs, and API outbox activity. If the worker
   is absent or unhealthy, pause further upload/analysis testing and restore a healthy
   worker deployment before retrying.
4. For a worker retry/dead letter, inspect the stable error code and stage, confirm queue,
   database, storage and provider availability, and fix the cause first. The owner can
   retry a durable failed job only within the existing lifetime attempt budget. Do not
   manually delete the durable job or purge SQS to silence an alarm.
5. For provider latency or rising usage, stop submitting test PDFs, inspect Bedrock
   service health and AWS actual-cost alerts, then resume only after the cause and expected
   spend are understood. Do not enable Bedrock request/response logging.
6. Confirm recovery by observing queue age/backlog fall, healthy worker tasks and no new
   retries/dead letters. CloudWatch sends an OK notification when an alarm returns to
   normal; document the UTC interval and safe identifiers without attaching source content.

## Acceptance status and cost

Automated tests validate EMF dimensions, correlation-ID restrictions and rejection of
unsafe values. Terraform creates the dashboard, queue/worker alarms and configurable SNS
email subscriptions. These code/configuration checks do not prove that SNS emails arrive.
The worker is not currently deployed to staging. Per the agreed #39 then #24 sequence, run
the controlled worker failure, verify SNS delivery, and confirm recovery **after #24 deploys
the API and worker**. Keep that staging acceptance explicitly pending until the deployment
and delivery test are recorded.

EMF log ingestion, custom metric storage, dashboards, alarms and SNS delivery incur AWS
charges. EMF emits a small number of metrics for each bounded dimension set; identifiers
are excluded to prevent per-request metric creation. Review CloudWatch billing after the
staging test window and before increasing retention or adding new metric dimensions.
