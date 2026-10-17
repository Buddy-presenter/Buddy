mock_provider "aws" {}

variables {
  task_definition = "arn:aws:ecs:eu-central-1:123456789012:task-definition/buddy-staging-api:42"
  desired_count   = 1
}

run "release_promotes_new_revision" {
  command = apply

  assert {
    condition     = aws_ecs_service.api.task_definition == var.task_definition
    error_message = "The service should begin on the revision promoted by the release driver."
  }
}

run "terraform_plans_after_promotion_without_reverting_revision" {
  command = plan

  variables {
    # This is the older revision still recorded in the Terraform configuration.
    task_definition = "arn:aws:ecs:eu-central-1:123456789012:task-definition/buddy-staging-api:1"
    desired_count   = 0
  }

  assert {
    condition     = aws_ecs_service.api.task_definition == "arn:aws:ecs:eu-central-1:123456789012:task-definition/buddy-staging-api:42"
    error_message = "A Terraform plan after release promotion must retain the active task revision."
  }
  assert {
    condition     = aws_ecs_service.api.desired_count == 0
    error_message = "Terraform must continue managing service attributes outside release ownership."
  }
}
