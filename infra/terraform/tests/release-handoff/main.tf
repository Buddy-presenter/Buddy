terraform {
  required_version = "~> 1.13.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.20.0"
    }
  }
}

variable "task_definition" { type = string }
variable "desired_count" { type = number }

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = "arn:aws:ecs:eu-central-1:123456789012:cluster/buddy-staging"
  task_definition = var.task_definition
  desired_count   = var.desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = ["subnet-0123456789abcdef0"]
    security_groups  = ["sg-0123456789abcdef0"]
    assign_public_ip = false
  }

  # Mirrors the release-owned field on the actual API and worker services.
  lifecycle {
    ignore_changes = [task_definition]
  }
}
