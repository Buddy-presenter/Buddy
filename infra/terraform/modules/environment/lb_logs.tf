variable "enable_lb_access_logs" {
  description = "Record one load-balancer access-log line per request (kept 7 days), including requests answered while no task runs."
  type        = bool
  default     = false
}

# Elastic Load Balancing writes access logs with its regional log-delivery account.
data "aws_elb_service_account" "main" {
  count = var.enable_lb_access_logs ? 1 : 0
}

resource "aws_s3_bucket" "lb_logs" {
  count  = var.enable_lb_access_logs ? 1 : 0
  bucket = "${local.name}-${var.account_id}-lb-logs"
}
resource "aws_s3_bucket_public_access_block" "lb_logs" {
  count                   = var.enable_lb_access_logs ? 1 : 0
  bucket                  = aws_s3_bucket.lb_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "lb_logs" {
  count  = var.enable_lb_access_logs ? 1 : 0
  bucket = aws_s3_bucket.lb_logs[0].id
  rule { object_ownership = "BucketOwnerEnforced" }
}
# Load-balancer access logs support only SSE-S3, not KMS.
resource "aws_s3_bucket_server_side_encryption_configuration" "lb_logs" {
  count  = var.enable_lb_access_logs ? 1 : 0
  bucket = aws_s3_bucket.lb_logs[0].id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "lb_logs" {
  count  = var.enable_lb_access_logs ? 1 : 0
  bucket = aws_s3_bucket.lb_logs[0].id
  rule {
    id     = "expire-access-logs"
    status = "Enabled"
    filter { prefix = "" }
    expiration { days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_s3_bucket_policy" "lb_logs" {
  count  = var.enable_lb_access_logs ? 1 : 0
  bucket = aws_s3_bucket.lb_logs[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
      Resource  = [aws_s3_bucket.lb_logs[0].arn, "${aws_s3_bucket.lb_logs[0].arn}/*"],
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    },
    {
      Sid       = "LoadBalancerAccessLogs", Effect = "Allow",
      Principal = { AWS = data.aws_elb_service_account.main[0].arn }, Action = "s3:PutObject",
      Resource  = "${aws_s3_bucket.lb_logs[0].arn}/api/AWSLogs/${var.account_id}/*"
    },
  ] })
}
