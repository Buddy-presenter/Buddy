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
run "speech_disabled_by_default" {
  command = plan
  assert {
    condition     = length(aws_secretsmanager_secret.speech) == 0 && length(aws_iam_role_policy.speech_secret) == 0
    error_message = "Speech secrets and permissions must be opt-in."
  }
}
run "speech_secret_is_api_only" {
  command = apply
  variables { enable_speech = true }
  override_resource {
    target          = aws_secretsmanager_secret.speech[0]
    values          = { arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:buddy-staging/speech/deepgram-ABC123" }
    override_during = plan
  }
  assert {
    condition = (
      length([for secret in jsondecode(aws_ecs_task_definition.service["api"].container_definitions)[0].secrets : secret if secret.name == "BUDDY_DEEPGRAM_API_KEY"]) == 1 &&
      length([for secret in jsondecode(aws_ecs_task_definition.service["worker"].container_definitions)[0].secrets : secret if secret.name == "BUDDY_DEEPGRAM_API_KEY"]) == 0 &&
      length([for secret in jsondecode(aws_ecs_task_definition.service["migration"].container_definitions)[0].secrets : secret if secret.name == "BUDDY_DEEPGRAM_API_KEY"]) == 0 &&
      jsondecode(aws_iam_role_policy.speech_secret[0].policy).Statement[0].Resource == aws_secretsmanager_secret.speech[0].arn
    )
    error_message = "Only the API must receive the exact server-side provider secret."
  }
}
