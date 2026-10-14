terraform {
  required_version = "~> 1.13.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.19.0" }
  }
}
variable "account_id" { type = string }
variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "Use staging or production."
  }
}
variable "region" {
  type    = string
  default = "eu-central-1"
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}
resource "aws_kms_key" "state" {
  description             = "Buddy ${var.environment} Terraform state"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket" "state" {
  bucket = "buddy-${var.environment}-${var.account_id}-tfstate"
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.state.arn
    }
  }
}
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}
output "state_bucket" { value = aws_s3_bucket.state.id }
output "state_key_arn" { value = aws_kms_key.state.arn }
output "github_oidc_provider_arn" { value = aws_iam_openid_connect_provider.github.arn }
