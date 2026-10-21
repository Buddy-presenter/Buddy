variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "Use staging or production."
  }
}
variable "region" { type = string }
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "An explicit AWS account ID is required."
  }
}
variable "ingress_mode" {
  type    = string
  default = "custom_domain"
  validation {
    condition     = contains(["custom_domain", "aws_https"], var.ingress_mode)
    error_message = "Use custom_domain (direct ALB) or aws_https (CloudFront private origin)."
  }
}
variable "api_domain" {
  type    = string
  default = null
  validation {
    condition     = var.api_domain == null ? var.ingress_mode == "aws_https" : can(regex("^[a-z0-9][a-z0-9.-]+\\.[a-z]{2,}$", var.api_domain))
    error_message = "Supply a DNS hostname owned by the team."
  }
}
variable "zone_id" {
  type    = string
  default = null
  validation {
    condition     = var.api_domain == null ? var.zone_id == null : try(length(var.zone_id) > 0, false)
    error_message = "A custom hostname requires its hosted zone; omit both for the AWS hostname."
  }
}
variable "api_image" {
  type     = string
  default  = null
  nullable = true
  validation {
    condition     = var.api_image == null ? true : can(regex("^${var.account_id}\\.dkr\\.ecr\\.${var.region}\\.amazonaws\\.com/buddy-${var.environment}/api@sha256:[a-f0-9]{64}$", var.api_image))
    error_message = "API image must be a private ECR image pinned by SHA-256 digest."
  }
}
variable "worker_image" {
  type     = string
  default  = null
  nullable = true
  validation {
    condition     = var.worker_image == null ? true : can(regex("^${var.account_id}\\.dkr\\.ecr\\.${var.region}\\.amazonaws\\.com/buddy-${var.environment}/worker@sha256:[a-f0-9]{64}$", var.worker_image))
    error_message = "Worker image must be null or a real ECR digest; never use a placeholder."
  }
}
variable "start_services" {
  type    = bool
  default = false
  validation {
    condition     = !var.start_services || var.api_image != null
    error_message = "A real API image is required before starting services."
  }
}
variable "worker_start_services" {
  description = "Whether Terraform should start a newly provisioned worker; staging keeps it paused until the reviewed release driver promotes it."
  type        = bool
  default     = true
  validation {
    condition     = !var.worker_start_services || (var.start_services && var.worker_image != null)
    error_message = "Starting a worker requires start_services and a real worker image."
  }
}
variable "vpc_cidr" { type = string }
variable "service_egress_mode" {
  description = "Staging may avoid NAT using public task IPs with SG-restricted inbound traffic."
  type        = string
  default     = "private_nat"
  validation {
    condition     = contains(["private_nat", "public_ip"], var.service_egress_mode) && (var.environment != "production" || var.service_egress_mode == "private_nat")
    error_message = "Use private_nat, or staging-only public_ip. Production retains private task addresses."
  }
}
variable "enable_terraform_monitoring" {
  description = "Manage CloudWatch/SNS monitoring with Terraform. Set false when another reviewed IaC stack owns the monitoring resources."
  type        = bool
  default     = true
}
variable "github_repository" {
  type    = string
  default = "Buddy-presenter/Buddy"
}
variable "github_oidc_subject_prefix" {
  description = "Exact repository OIDC prefix; null preserves the legacy name-only identity."
  type        = string
  default     = null
  validation {
    condition = var.github_oidc_subject_prefix == null ? true : (
      can(regex("^repo:[A-Za-z0-9_.-]+@[1-9][0-9]*/[A-Za-z0-9_.-]+@[1-9][0-9]*$", var.github_oidc_subject_prefix)) &&
      replace(var.github_oidc_subject_prefix, "/@[0-9]+/", "") == "repo:${var.github_repository}"
    )
    error_message = "Use this repository's exact immutable repo:owner@ID/name@ID prefix, without wildcards or environment suffix."
  }
}
variable "github_oidc_provider_arn" {
  type = string
  validation {
    condition     = var.github_oidc_provider_arn == "arn:aws:iam::${var.account_id}:oidc-provider/token.actions.githubusercontent.com"
    error_message = "Use the account's existing GitHub OIDC provider ARN."
  }
}
locals {
  name           = "buddy-${var.environment}"
  production     = var.environment == "production"
  services       = toset(["api", "migration", "worker"])
  aws_https      = var.ingress_mode == "aws_https"
  api_host       = local.aws_https ? aws_cloudfront_distribution.api[0].domain_name : var.api_domain
  api_url        = "https://${coalesce(var.api_domain, local.api_host)}"
  public_task_ip = var.service_egress_mode == "public_ip"
  nat_count      = local.public_task_ip ? 0 : (local.production ? 2 : 1)
  task_subnets   = local.public_task_ip ? aws_subnet.public[*].id : aws_subnet.private[*].id
}
