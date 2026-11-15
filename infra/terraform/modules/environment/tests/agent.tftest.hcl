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
  api_domain               = "staging.example.com"
  zone_id                  = "Z0123456789"
  vpc_cidr                 = "10.40.0.0/16"
  github_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  api_image                = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  worker_image             = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
run "agent_requires_semantic_key" {
  command = plan
  variables { enable_presentation_agent = true }
  expect_failures = [var.enable_presentation_agent]
}
run "agent_uses_existing_key_and_api_only_flag" {
  command = plan
  variables {
    enable_semantic_candidates = true
    enable_presentation_agent  = true
  }
  override_resource {
    target = aws_lb.api
    values = {
      dns_name = "buddy-staging.eu-central-1.elb.amazonaws.com"
      arn      = "arn:aws:elasticloadbalancing:eu-central-1:123456789012:loadbalancer/app/buddy-staging/0123456789abcdef"
    }
    override_during = plan
  }
  override_resource {
    target = aws_cognito_user_pool.main
    values = {
      id  = "eu-central-1_Example"
      arn = "arn:aws:cognito-idp:eu-central-1:123456789012:userpool/eu-central-1_Example"
    }
    override_during = plan
  }
  override_resource {
    target          = aws_cognito_user_pool_client.desktop
    values          = { id = "client123" }
    override_during = plan
  }
  override_resource {
    target          = aws_s3_bucket.private["data"]
    values          = { id = "buddy-staging-123456789012-data" }
    override_during = plan
  }
  override_resource {
    target          = aws_kms_key.data
    values          = { arn = "arn:aws:kms:eu-central-1:123456789012:key/11111111-1111-1111-1111-111111111111" }
    override_during = plan
  }
  override_resource {
    target          = aws_sqs_queue.processing
    values          = { url = "https://sqs.eu-central-1.amazonaws.com/123456789012/buddy-staging-processing" }
    override_during = plan
  }
  override_resource {
    target          = aws_secretsmanager_secret.database["api"]
    values          = { arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:api-database-ABC123" }
    override_during = plan
  }
  override_resource {
    target          = aws_secretsmanager_secret.database["worker"]
    values          = { arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:worker-database-ABC123" }
    override_during = plan
  }
  override_resource {
    target          = aws_secretsmanager_secret.database["migration"]
    values          = { arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:migration-database-ABC123" }
    override_during = plan
  }
  override_resource {
    target          = aws_secretsmanager_secret.cohere[0]
    values          = { arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:buddy-staging/embeddings/cohere-ABC123" }
    override_during = plan
  }
  override_resource {
    target          = aws_iam_role.execution["api"]
    values          = { id = "buddy-staging-api-execution", arn = "arn:aws:iam::123456789012:role/buddy-staging-api-execution" }
    override_during = plan
  }
  assert {
    condition = (
      one([for value in jsondecode(aws_ecs_task_definition.service["api"].container_definitions)[0].environment : value.value if value.name == "BUDDY_AGENT_ENABLED"]) == "true" &&
      length([for value in jsondecode(aws_ecs_task_definition.service["worker"].container_definitions)[0].environment : value if value.name == "BUDDY_AGENT_ENABLED"]) == 0 &&
      length([for value in jsondecode(aws_ecs_task_definition.service["migration"].container_definitions)[0].environment : value if value.name == "BUDDY_AGENT_ENABLED"]) == 0 &&
      length([for secret in jsondecode(aws_ecs_task_definition.service["api"].container_definitions)[0].secrets : secret if secret.name == "BUDDY_COHERE_API_KEY"]) == 1 &&
      length([for secret in jsondecode(aws_ecs_task_definition.service["worker"].container_definitions)[0].secrets : secret if secret.name == "BUDDY_COHERE_API_KEY"]) == 0 &&
      length([for secret in jsondecode(aws_ecs_task_definition.service["migration"].container_definitions)[0].secrets : secret if secret.name == "BUDDY_COHERE_API_KEY"]) == 0 &&
      aws_iam_role_policy.cohere_secret[0].role == aws_iam_role.execution["api"].id
    )
    error_message = "Agent configuration and its Cohere credential must stay API-only."
  }
}
