mock_provider "aws" {}
variables {
  account_id  = "123456789012"
  environment = "staging"
}
run "staging_owns_the_shared_account_provider" {
  command = plan
  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 1 && aws_s3_bucket.state.bucket == "buddy-staging-123456789012-tfstate"
    error_message = "The initial environment needs its own state and one account OIDC provider."
  }
}
run "production_reuses_provider_with_independent_state" {
  command = plan
  variables {
    environment                     = "production"
    existing_github_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  }
  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 0 && aws_s3_bucket.state.bucket == "buddy-production-123456789012-tfstate"
    error_message = "The second environment must reuse OIDC while keeping independent state."
  }
}
run "reject_reusing_another_accounts_provider" {
  command = plan
  variables {
    existing_github_oidc_provider_arn = "arn:aws:iam::999999999999:oidc-provider/token.actions.githubusercontent.com"
  }
  expect_failures = [var.existing_github_oidc_provider_arn]
}
