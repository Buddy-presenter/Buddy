terraform {
  required_version = "~> 1.13.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.20.0" }
  }
  backend "s3" {}
}
variable "account_id" { type = string }
variable "region" {
  type    = string
  default = "eu-central-1"
}
variable "service_egress_mode" {
  type    = string
  default = "public_ip"
}
variable "api_domain" {
  type    = string
  default = null
}
variable "zone_id" {
  type    = string
  default = null
}
variable "ingress_mode" {
  type    = string
  default = "aws_https"
}
variable "api_image" {
  type    = string
  default = null
}
variable "worker_image" {
  type    = string
  default = null
}
variable "start_services" {
  type    = bool
  default = false
}
variable "alert_email_recipients" {
  description = "Approved operational alert destinations. Store this input in protected operator configuration, not source control."
  type        = set(string)
  default     = []
}
variable "github_oidc_provider_arn" { type = string }
variable "github_oidc_subject_prefix" {
  description = "Prefix reported by GitHub's repository OIDC customization API."
  type        = string
  default     = "repo:Buddy-presenter@343976635/Buddy@1408836773"
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { Application = "Buddy", Environment = "staging", ManagedBy = "Terraform" }
  }
}
provider "aws" {
  alias               = "edge"
  region              = "us-east-1"
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { Application = "Buddy", Environment = "staging", ManagedBy = "Terraform" }
  }
}
module "environment" {
  source                     = "../../modules/environment"
  providers                  = { aws = aws, aws.edge = aws.edge }
  ingress_mode               = var.ingress_mode
  service_egress_mode        = var.service_egress_mode
  environment                = "staging"
  account_id                 = var.account_id
  region                     = var.region
  api_domain                 = var.api_domain
  zone_id                    = var.zone_id
  api_image                  = var.api_image
  worker_image               = var.worker_image
  start_services             = var.start_services
  alert_email_recipients     = var.alert_email_recipients
  github_oidc_provider_arn   = var.github_oidc_provider_arn
  github_oidc_subject_prefix = var.github_oidc_subject_prefix
  vpc_cidr                   = "10.40.0.0/16"
}
output "deployment" { value = module.environment }
