terraform {
  required_version = "~> 1.13.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.19.0" }
  }
  backend "s3" {}
}
variable "account_id" { type = string }
variable "other_environment_account_id" {
  type = string
  validation {
    condition     = var.other_environment_account_id != var.account_id
    error_message = "Staging and production must use separate AWS accounts."
  }
}
variable "region" {
  type    = string
  default = "eu-central-1"
}
variable "api_domain" { type = string }
variable "zone_id" { type = string }
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
variable "github_oidc_provider_arn" { type = string }
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { Application = "Buddy", Environment = "staging", ManagedBy = "Terraform" }
  }
}
module "environment" {
  source                   = "../../modules/environment"
  environment              = "staging"
  account_id               = var.account_id
  region                   = var.region
  api_domain               = var.api_domain
  zone_id                  = var.zone_id
  api_image                = var.api_image
  worker_image             = var.worker_image
  start_services           = var.start_services
  github_oidc_provider_arn = var.github_oidc_provider_arn
  vpc_cidr                 = "10.40.0.0/16"
}
output "deployment" { value = module.environment }
