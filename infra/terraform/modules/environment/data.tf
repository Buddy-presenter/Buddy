resource "aws_kms_key" "data" {
  description             = "${local.name} private data, database, queues and secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}
resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.data.key_id
}
resource "aws_s3_bucket" "private" {
  for_each = toset(["data", "releases"])
  bucket   = "${local.name}-${var.account_id}-${each.key}"
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "private" {
  for_each                = aws_s3_bucket.private
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "private" {
  for_each = aws_s3_bucket.private
  bucket   = each.value.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "private" {
  for_each = aws_s3_bucket.private
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}
resource "aws_s3_bucket_versioning" "private" {
  for_each = aws_s3_bucket.private
  bucket   = each.value.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_policy" "tls" {
  for_each = aws_s3_bucket.private
  bucket   = each.value.id
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [each.value.arn, "${each.value.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_s3_bucket_lifecycle_configuration" "private" {
  for_each = aws_s3_bucket.private
  bucket   = each.value.id
  rule {
    id     = "bounded-old-versions-and-incomplete-uploads"
    status = "Enabled"
    filter { prefix = "" }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
    noncurrent_version_expiration { noncurrent_days = local.production ? 35 : 7 }
  }
}
resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.database[*].id
}
resource "aws_db_parameter_group" "main" {
  name   = local.name
  family = "postgres17"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}
resource "aws_db_instance" "main" {
  identifier                    = local.name
  engine                        = "postgres"
  engine_version                = "17"
  instance_class                = local.production ? "db.t4g.medium" : "db.t4g.micro"
  allocated_storage             = 20
  max_allocated_storage         = 100
  storage_type                  = "gp3"
  storage_encrypted             = true
  kms_key_id                    = aws_kms_key.data.arn
  db_name                       = "buddy"
  username                      = "buddyadmin"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.data.arn
  multi_az                      = local.production
  publicly_accessible           = false
  db_subnet_group_name          = aws_db_subnet_group.main.name
  vpc_security_group_ids        = [aws_security_group.database.id]
  parameter_group_name          = aws_db_parameter_group.main.name
  backup_retention_period       = local.production ? 35 : 7
  backup_window                 = "01:00-02:00"
  maintenance_window            = "sun:03:00-sun:04:00"
  deletion_protection           = true
  skip_final_snapshot           = false
  final_snapshot_identifier     = "${local.name}-final"
  copy_tags_to_snapshot         = true
  auto_minor_version_upgrade    = true
}
resource "aws_secretsmanager_secret" "database" {
  for_each                = local.services
  name                    = "${local.name}/database/${each.key}"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = 30
}
resource "aws_sqs_queue" "dead" {
  name                      = "${local.name}-processing-dead"
  kms_master_key_id         = aws_kms_key.data.arn
  message_retention_seconds = 1209600
}
resource "aws_sqs_queue" "processing" {
  name                       = "${local.name}-processing"
  kms_master_key_id          = aws_kms_key.data.arn
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dead.arn, maxReceiveCount = 3 })
}
resource "aws_sqs_queue_policy" "tls" {
  queue_url = aws_sqs_queue.processing.id
  policy    = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.processing.arn,
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
