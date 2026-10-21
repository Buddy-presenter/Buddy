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
variable "worker_start_services" {
  type    = bool
  default = true
}
variable "github_oidc_provider_arn" { type = string }
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { Application = "Buddy", Environment = "production", ManagedBy = "Terraform" }
  }
}
provider "aws" {
  alias               = "edge"
  region              = "us-east-1"
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { Application = "Buddy", Environment = "production", ManagedBy = "Terraform" }
  }
}
module "environment" {
  source                   = "../../modules/environment"
  providers                = { aws = aws, aws.edge = aws.edge }
  ingress_mode             = var.ingress_mode
  environment              = "production"
  account_id               = var.account_id
  region                   = var.region
  api_domain               = var.api_domain
  zone_id                  = var.zone_id
  api_image                = var.api_image
  worker_image             = var.worker_image
  start_services           = var.start_services
  worker_start_services    = var.worker_start_services
  github_oidc_provider_arn = var.github_oidc_provider_arn
  vpc_cidr                 = "10.50.0.0/16"
}
output "deployment" { value = module.environment }
