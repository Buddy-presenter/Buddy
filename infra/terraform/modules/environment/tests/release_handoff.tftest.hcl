mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-central-1a", "eu-central-1b"] }
  }
}

mock_provider "aws" {
  alias = "edge"
}

variables {
  environment              = "staging"
  region                   = "eu-central-1"
  account_id               = "123456789012"
  ingress_mode             = "aws_https"
  api_domain               = null
  zone_id                  = null
  service_egress_mode      = "public_ip"
  vpc_cidr                 = "10.40.0.0/16"
  github_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  api_image                = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  worker_image             = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  start_services           = true
}

# Simulate the release driver promoting revisions after Terraform creates the
# service. This override is applied only when the mocked resources are created;
# the following plan reads these revisions from test state like a refreshed service.
override_resource {
  target = aws_ecs_service.api[0]
  values = {
    task_definition = "arn:aws:ecs:eu-central-1:123456789012:task-definition/buddy-staging-api:42"
  }
  override_during = apply
}

override_resource {
  target = aws_ecs_service.worker[0]
  values = {
    task_definition = "arn:aws:ecs:eu-central-1:123456789012:task-definition/buddy-staging-worker:42"
  }
  override_during = apply
}

run "release_promotes_new_api_and_worker_revisions" {
  command = apply

  assert {
    condition     = aws_ecs_service.api[0].task_definition == "arn:aws:ecs:eu-central-1:123456789012:task-definition/buddy-staging-api:42"
    error_message = "The simulated release must leave the API service on the promoted revision."
  }
  assert {
    condition     = aws_ecs_service.worker[0].task_definition == "arn:aws:ecs:eu-central-1:123456789012:task-definition/buddy-staging-worker:42"
    error_message = "The simulated release must leave the worker service on the promoted revision."
  }
}

run "terraform_plan_after_promotion_keeps_active_revisions" {
  command = plan

  # A later infrastructure apply still controls the service size. In parallel,
  # the configured task-definition template remains older than the release revision.
  variables {
    start_services = false
  }

  assert {
    condition     = aws_ecs_service.api[0].task_definition == "arn:aws:ecs:eu-central-1:123456789012:task-definition/buddy-staging-api:42" && aws_ecs_service.api[0].desired_count == 0
    error_message = "Planning after promotion must keep the active API revision while continuing to manage desired count."
  }
  assert {
    condition     = aws_ecs_service.worker[0].task_definition == "arn:aws:ecs:eu-central-1:123456789012:task-definition/buddy-staging-worker:42" && aws_ecs_service.worker[0].desired_count == 0
    error_message = "Planning after promotion must keep the active worker revision while continuing to manage desired count."
  }
}
