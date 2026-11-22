variable "idle_scale_to_zero" {
  description = "Stop the API and worker after a quiet period and start them again on the next request (staging only)."
  type        = bool
  default     = false
  validation {
    condition     = !var.idle_scale_to_zero || var.environment != "production"
    error_message = "Idle scale-to-zero is for non-production environments only."
  }
}
variable "idle_minutes" {
  description = "Minutes without API requests and queued work before staging scales to zero."
  type        = number
  default     = 30
  validation {
    condition     = var.idle_minutes >= 10 && var.idle_minutes <= 240 && var.idle_minutes % 5 == 0
    error_message = "Idle minutes must be a multiple of 5 between 10 and 240."
  }
}

locals {
  # Services that scale together. The worker follows API traffic, and also wakes or
  # stays up while upload jobs are queued or in flight.
  idle_services = var.idle_scale_to_zero ? toset(compact([
    var.api_image == null ? "" : "api",
    var.worker_image == null ? "" : "worker",
  ])) : toset([])
  idle_periods = var.idle_minutes / 5
  # ALB counts every request it receives, including the ones it answers with 503
  # while no task is running, so the first request after a quiet period wakes staging.
  requests_metric = {
    id          = "requests"
    namespace   = "AWS/ApplicationELB"
    metric_name = "RequestCount"
    dimensions  = { LoadBalancer = aws_lb.api.arn_suffix }
  }
}

resource "aws_appautoscaling_target" "idle" {
  for_each           = local.idle_services
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${each.key}"
  min_capacity       = 0
  max_capacity       = 1
  depends_on         = [aws_ecs_service.api, aws_ecs_service.worker]
}

resource "aws_appautoscaling_policy" "wake" {
  for_each           = local.idle_services
  name               = "${local.name}-${each.key}-wake"
  service_namespace  = "ecs"
  scalable_dimension = aws_appautoscaling_target.idle[each.key].scalable_dimension
  resource_id        = aws_appautoscaling_target.idle[each.key].resource_id
  policy_type        = "StepScaling"
  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 60
    metric_aggregation_type = "Maximum"
    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = 1
    }
  }
}

resource "aws_appautoscaling_policy" "sleep" {
  for_each           = local.idle_services
  name               = "${local.name}-${each.key}-sleep"
  service_namespace  = "ecs"
  scalable_dimension = aws_appautoscaling_target.idle[each.key].scalable_dimension
  resource_id        = aws_appautoscaling_target.idle[each.key].resource_id
  policy_type        = "StepScaling"
  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 300
    metric_aggregation_type = "Maximum"
    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = 0
    }
  }
}

# Wake: any API request (or, for the worker, any queued upload job) in the last minute.
resource "aws_cloudwatch_metric_alarm" "wake" {
  count               = var.idle_scale_to_zero ? 1 : 0
  alarm_name          = "${local.name}-wake-on-request"
  alarm_description   = "Starts idle staging services when the API receives a request or upload work is queued."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [for service in local.idle_services : aws_appautoscaling_policy.wake[service].arn]
  metric_query {
    id          = "activity"
    expression  = "FILL(requests, 0) + FILL(queued, 0)"
    label       = "Requests and queued upload jobs"
    return_data = true
  }
  metric_query {
    id = local.requests_metric.id
    metric {
      namespace   = local.requests_metric.namespace
      metric_name = local.requests_metric.metric_name
      dimensions  = local.requests_metric.dimensions
      period      = 60
      stat        = "Sum"
    }
  }
  metric_query {
    id = "queued"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = aws_sqs_queue.processing.name }
      period      = 60
      stat        = "Maximum"
    }
  }
}

# Sleep: no API requests and no queued or in-flight upload jobs for the whole idle window.
resource "aws_cloudwatch_metric_alarm" "sleep" {
  count               = var.idle_scale_to_zero ? 1 : 0
  alarm_name          = "${local.name}-sleep-when-idle"
  alarm_description   = "Stops staging services after ${var.idle_minutes} minutes without requests or upload work."
  comparison_operator = "LessThanOrEqualToThreshold"
  evaluation_periods  = local.idle_periods
  datapoints_to_alarm = local.idle_periods
  threshold           = 0
  treat_missing_data  = "breaching"
  alarm_actions       = [for service in local.idle_services : aws_appautoscaling_policy.sleep[service].arn]
  metric_query {
    id          = "activity"
    expression  = "FILL(requests, 0) + FILL(queued, 0) + FILL(inflight, 0)"
    label       = "Requests and upload jobs"
    return_data = true
  }
  metric_query {
    id = local.requests_metric.id
    metric {
      namespace   = local.requests_metric.namespace
      metric_name = local.requests_metric.metric_name
      dimensions  = local.requests_metric.dimensions
      period      = 300
      stat        = "Sum"
    }
  }
  metric_query {
    id = "queued"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = aws_sqs_queue.processing.name }
      period      = 300
      stat        = "Maximum"
    }
  }
  metric_query {
    id = "inflight"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesNotVisible"
      dimensions  = { QueueName = aws_sqs_queue.processing.name }
      period      = 300
      stat        = "Maximum"
    }
  }
}
