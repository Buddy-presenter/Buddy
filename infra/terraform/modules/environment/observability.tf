variable "alert_email_recipients" {
  description = "Approved operational alert recipients. Every SNS email subscription must be confirmed by its owner."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for address in var.alert_email_recipients : can(regex("^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$", address))])
    error_message = "Alert recipients must be valid email addresses."
  }
}

locals {
  monitoring_namespace = "Buddy/Operational"
  alarm_actions        = var.enable_terraform_monitoring ? [aws_sns_topic.operations[0].arn] : []
  dashboard_body = {
    start          = "-PT6H"
    periodOverride = "inherit"
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6,
        properties = {
          title = "Upload outcomes and worker processing"
          view  = "timeSeries", region = var.region, stat = "Sum", period = 300
          metrics = [
            [local.monitoring_namespace, "UploadRequest", "Environment", var.environment, "Service", "api", "Outcome", "success", { label = "Uploads accepted" }],
            [local.monitoring_namespace, "UploadRequest", "Environment", var.environment, "Service", "api", "Outcome", "rejected", { label = "Uploads rejected" }],
            [local.monitoring_namespace, "JobOutcome", "Environment", var.environment, "Service", "worker", "Outcome", "success", { label = "Jobs completed" }],
            [local.monitoring_namespace, "JobOutcome", "Environment", var.environment, "Service", "worker", "Outcome", "retry", { label = "Jobs retrying" }],
            [local.monitoring_namespace, "JobOutcome", "Environment", var.environment, "Service", "worker", "Outcome", "dead_letter", { label = "Jobs dead-lettered" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6,
        properties = {
          title = "Processing queue and dead-letter queue"
          view  = "timeSeries", region = var.region, stat = "Maximum", period = 300
          metrics = [
            ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", aws_sqs_queue.processing.name, { label = "Oldest message age (seconds)" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.processing.name, { label = "Visible backlog" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.dead.name, { label = "Dead-letter queue" }]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6,
        properties = {
          title = "Worker and provider durations"
          view  = "timeSeries", region = var.region, stat = "Average", period = 300
          metrics = [
            [local.monitoring_namespace, "DurationMs", "Environment", var.environment, "Service", "worker", "Stage", "pdf_metadata.v1", "Outcome", "success", { label = "Full job from metadata stage (ms)" }],
            [local.monitoring_namespace, "DurationMs", "Environment", var.environment, "Service", "worker", "Stage", "provider", "Outcome", "success", { label = "Bedrock analysis (ms)" }],
            [local.monitoring_namespace, "DurationMs", "Environment", var.environment, "Service", "worker", "Stage", "pdf_render", "Outcome", "success", { label = "PDF render-manifest retry (ms)" }],
            [local.monitoring_namespace, "DurationMs", "Environment", var.environment, "Service", "worker", "Stage", "validation", "Outcome", "success", { label = "Action validation (ms)" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6,
        properties = {
          title = "Provider token usage (input includes document processing)"
          view  = "timeSeries", region = var.region, stat = "Sum", period = 3600
          metrics = [
            [local.monitoring_namespace, "InputTokens", "Environment", var.environment, "Service", "worker", "Stage", "provider", "Outcome", "success", { label = "Input tokens" }],
            [local.monitoring_namespace, "OutputTokens", "Environment", var.environment, "Service", "worker", "Stage", "provider", "Outcome", "success", { label = "Output tokens" }]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 12, height = 4,
        properties = {
          title = "Account-wide estimated AWS charges (USD; billing data may lag)"
          view  = "timeSeries", region = "us-east-1", stat = "Maximum", period = 21600
          metrics = [
            ["AWS/Billing", "EstimatedCharges", "Currency", "USD", { label = "Estimated account charges" }]
          ]
        }
      }
    ]
  }
}

resource "aws_sns_topic" "operations" {
  count = var.enable_terraform_monitoring ? 1 : 0
  name  = "${local.name}-operations-alerts"
}

resource "aws_sns_topic_policy" "operations" {
  count = var.enable_terraform_monitoring ? 1 : 0
  arn   = aws_sns_topic.operations[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudWatchAlarms"
      Effect    = "Allow"
      Principal = { Service = "cloudwatch.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.operations[0].arn
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:cloudwatch:${var.region}:${var.account_id}:alarm:${local.name}-*" }
      }
    }]
  })
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = var.enable_terraform_monitoring ? var.alert_email_recipients : toset([])
  topic_arn = aws_sns_topic.operations[0].arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_cloudwatch_dashboard" "operations" {
  count          = var.enable_terraform_monitoring ? 1 : 0
  dashboard_name = "${local.name}-operations"
  dashboard_body = jsonencode(local.dashboard_body)
}

resource "aws_cloudwatch_metric_alarm" "queue_age" {
  count               = var.enable_terraform_monitoring ? 1 : 0
  alarm_name          = "${local.name}-queue-age"
  alarm_description   = "A PDF job has waited over five minutes; inspect worker health, queue depth and recent worker errors before retrying."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.processing.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 300
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "queue_backlog" {
  count               = var.enable_terraform_monitoring ? 1 : 0
  alarm_name          = "${local.name}-queue-backlog"
  alarm_description   = "More than ten PDFs are waiting; check worker desired/running tasks and processing failures."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.processing.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "dead_letters" {
  count               = var.enable_terraform_monitoring ? 1 : 0
  alarm_name          = "${local.name}-dead-letter-jobs"
  alarm_description   = "A message reached the SQS dead-letter queue; inspect its opaque job ID and sanitized worker logs, then follow the recovery runbook."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dead.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "worker_dead_letter" {
  count               = var.enable_terraform_monitoring ? 1 : 0
  alarm_name          = "${local.name}-worker-dead-letter-job"
  alarm_description   = "A processing job entered the durable dead_letter state; follow the PDF worker runbook before using the owner's bounded retry control."
  namespace           = local.monitoring_namespace
  metric_name         = "JobOutcome"
  dimensions          = { Environment = var.environment, Service = "worker", Outcome = "dead_letter" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "worker_retry" {
  count               = var.enable_terraform_monitoring ? 1 : 0
  alarm_name          = "${local.name}-worker-retry"
  alarm_description   = "A worker job failed and is scheduled for retry; inspect its sanitized stage logs and queue/worker health."
  namespace           = local.monitoring_namespace
  metric_name         = "JobOutcome"
  dimensions          = { Environment = var.environment, Service = "worker", Outcome = "retry" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}
