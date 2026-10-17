resource "aws_cognito_user_pool" "main" {
  name                     = local.name
  deletion_protection      = "ACTIVE"
  user_pool_tier           = "ESSENTIALS"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
  }
  username_configuration { case_sensitive = false }
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }
}
resource "aws_cognito_user_pool_client" "desktop" {
  name                                 = "${local.name}-desktop"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "aws.cognito.signin.user.admin"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["org.buddy.desktop://oauth/callback"]
  logout_urls                          = ["org.buddy.desktop://oauth/signed-out"]
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH"]
  enable_token_revocation              = true
  prevent_user_existence_errors        = "ENABLED"
  access_token_validity                = 5
  id_token_validity                    = 5
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
  refresh_token_rotation {
    feature                    = "ENABLED"
    retry_grace_period_seconds = 0
  }
  read_attributes  = ["email", "email_verified"]
  write_attributes = ["email"]
}
resource "aws_cognito_user_pool_domain" "main" {
  domain                = "${local.name}-${var.account_id}"
  user_pool_id          = aws_cognito_user_pool.main.id
  managed_login_version = 2
}
resource "aws_cognito_managed_login_branding" "desktop" {
  user_pool_id                = aws_cognito_user_pool.main.id
  client_id                   = aws_cognito_user_pool_client.desktop.id
  use_cognito_provided_values = true
  depends_on                  = [aws_cognito_user_pool_domain.main]
}
resource "aws_wafv2_web_acl" "main" {
  name  = local.name
  scope = "REGIONAL"
  default_action {
    allow {}
  }
  rule {
    name     = "per-ip-rate-limit"
    priority = 1
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit                 = 300
        aggregate_key_type    = "IP"
        evaluation_window_sec = 60
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate"
      sampled_requests_enabled   = false
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-waf"
    sampled_requests_enabled   = false
  }
}
resource "aws_wafv2_web_acl_association" "identity" {
  resource_arn = aws_cognito_user_pool.main.arn
  web_acl_arn  = aws_wafv2_web_acl.main.arn
}
