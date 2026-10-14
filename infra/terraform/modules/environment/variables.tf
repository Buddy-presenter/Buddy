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
variable "api_domain" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]+\\.[a-z]{2,}$", var.api_domain))
    error_message = "Supply a DNS hostname owned by the team."
  }
}
variable "zone_id" { type = string }
variable "api_image" {
  type     = string
  default  = null
  nullable = true
  validation {
    condition     = var.api_image == null ? true : can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9/-]+@sha256:[a-f0-9]{64}$", var.api_image))
    error_message = "API image must be a private ECR image pinned by SHA-256 digest."
  }
}
variable "worker_image" {
  type     = string
  default  = null
  nullable = true
  validation {
    condition     = var.worker_image == null ? true : can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9/-]+@sha256:[a-f0-9]{64}$", var.worker_image))
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
variable "vpc_cidr" { type = string }
variable "github_repository" {
  type    = string
  default = "HusseinMoukalled/Buddy"
}
variable "github_oidc_provider_arn" {
  type = string
  validation {
    condition     = can(regex(":oidc-provider/token.actions.githubusercontent.com$", var.github_oidc_provider_arn))
    error_message = "Use the account's existing GitHub OIDC provider ARN."
  }
}
locals {
  name       = "buddy-${var.environment}"
  production = var.environment == "production"
  services   = toset(["api", "migration", "worker"])
}
