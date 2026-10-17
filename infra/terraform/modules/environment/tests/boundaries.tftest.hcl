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
}
run "private_staging_before_images_exist" {
  command = plan
  assert {
    condition     = aws_cognito_user_pool_client.desktop.callback_urls == toset(["org.buddy.desktop://oauth/callback"]) && aws_cognito_user_pool_client.desktop.logout_urls == toset(["org.buddy.desktop://oauth/signed-out"])
    error_message = "Native application callbacks must not use localhost or raw IP addresses."
  }
  assert {
    condition     = aws_cognito_user_pool.main.verification_message_template[0].default_email_option == "CONFIRM_WITH_CODE" && aws_cognito_user_pool.main.auto_verified_attributes == toset(["email"])
    error_message = "Email verification must use confirmation codes."
  }
  assert {
    condition     = !aws_db_instance.main.publicly_accessible && aws_db_instance.main.storage_encrypted && aws_db_instance.main.deletion_protection
    error_message = "Database must be private, encrypted and protected."
  }
  assert {
    condition     = aws_db_instance.main.backup_retention_period == 1 && !aws_db_instance.main.skip_final_snapshot
    error_message = "Staging backups and final snapshots are required."
  }
  assert {
    condition     = alltrue([for bucket in aws_s3_bucket_public_access_block.private : bucket.block_public_acls && bucket.block_public_policy && bucket.ignore_public_acls && bucket.restrict_public_buckets])
    error_message = "Both private data and distribution storage must block public access."
  }
  assert {
    condition     = length(aws_ecs_service.api) == 0 && length(aws_ecs_service.worker) == 0
    error_message = "Missing images must not be replaced with pretend API/worker services."
  }
  assert {
    condition     = !aws_cognito_user_pool_client.desktop.generate_secret && aws_cognito_user_pool_client.desktop.enable_token_revocation && aws_cognito_user_pool_client.desktop.refresh_token_rotation[0].feature == "ENABLED"
    error_message = "Desktop client must be public with revocation and rotation enabled."
  }
  assert {
    condition     = aws_cognito_user_pool_client.desktop.allowed_oauth_flows == toset(["code"])
    error_message = "Implicit token grants are forbidden."
  }
  assert {
    condition     = aws_wafv2_web_acl.main.visibility_config[0].sampled_requests_enabled == false
    error_message = "Identity request samples can contain sensitive parameters."
  }
}
run "production_redundancy" {
  command = plan
  variables {
    environment    = "production"
    api_domain     = "api.example.com"
    api_image      = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-production/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    worker_image   = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-production/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    start_services = true
  }
  assert {
    condition     = aws_db_instance.main.multi_az && aws_db_instance.main.backup_retention_period == 35 && length(aws_nat_gateway.main) == 2
    error_message = "Production requires independent AZ egress, multi-AZ database and 35-day backups."
  }
  assert {
    condition     = aws_ecs_service.api[0].desired_count == 2 && aws_ecs_service.worker[0].desired_count == 2 && !aws_ecs_service.api[0].network_configuration[0].assign_public_ip
    error_message = "Production services require redundancy and private addresses."
  }
  assert {
    condition     = aws_ecs_service.api[0].deployment_circuit_breaker[0].rollback
    error_message = "Failed deployments must roll back."
  }
  assert {
    condition     = jsondecode(aws_iam_role.github["deploy"].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:Buddy-presenter/Buddy:environment:production"
    error_message = "Deploy role must trust only this repository's protected production environment."
  }
  assert {
    condition     = jsondecode(aws_iam_role.github["release"].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:Buddy-presenter/Buddy:environment:release-production"
    error_message = "Desktop publishing must have a separate release identity."
  }
}
run "reject_mutable_image" {
  command = plan
  variables { api_image = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api:latest" }
  expect_failures = [var.api_image]
}
run "reject_start_without_image" {
  command = plan
  variables { start_services = true }
  expect_failures = [var.start_services]
}
run "reject_unknown_environment" {
  command = plan
  variables { environment = "development" }
  expect_failures = [var.environment]
}

run "aws_https_without_a_domain" {
  command = plan
  variables {
    ingress_mode        = "aws_https"
    service_egress_mode = "public_ip"
    api_domain          = null
    zone_id             = null
    api_image           = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  assert {
    condition     = length(aws_nat_gateway.main) == 0 && length(aws_eip.nat) == 0 && aws_ecs_service.api[0].network_configuration[0].assign_public_ip
    error_message = "Low-cost staging must have no NAT costs or private-subnet default internet route."
  }
  assert {
    condition     = aws_ecs_task_definition.service["api"].cpu == "512" && aws_ecs_task_definition.service["api"].memory == "2048" && aws_db_instance.main.instance_class == "db.t4g.micro" && !aws_db_instance.main.publicly_accessible && aws_db_instance.main.backup_retention_period == 1 && one(aws_ecs_cluster.main.setting).value == "disabled"
    error_message = "Savings must preserve compute/database capacity and backups; only optional metrics are disabled."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.api.from_port == 8000 && aws_vpc_security_group_ingress_rule.api.cidr_ipv4 == null && aws_vpc_security_group_ingress_rule.api.cidr_ipv6 == null
    error_message = "A public task address must not introduce public inbound API rules."
  }
  assert {
    condition     = aws_lb.api.internal && length(aws_vpc_security_group_ingress_rule.public) == 0 && length(aws_lb_listener.https) == 0 && length(aws_lb_listener.redirect) == 0
    error_message = "AWS HTTPS must not expose a public HTTP/HTTPS ALB listener."
  }
  assert {
    condition     = length(aws_acm_certificate.api) == 0 && length(aws_route53_record.api) == 0 && length(aws_acm_certificate.edge) == 0 && length(aws_route53_record.edge_api) == 0
    error_message = "An AWS-owned hostname must require no domain, zone or certificate purchase."
  }
  assert {
    condition     = aws_cloudfront_distribution.api[0].viewer_certificate[0].cloudfront_default_certificate && aws_cloudfront_distribution.api[0].default_cache_behavior[0].viewer_protocol_policy == "https-only"
    error_message = "The public endpoint must use AWS-managed HTTPS and reject plaintext."
  }
  assert {
    condition     = aws_cloudfront_distribution.api[0].default_cache_behavior[0].min_ttl == 0 && aws_cloudfront_distribution.api[0].default_cache_behavior[0].default_ttl == 0 && aws_cloudfront_distribution.api[0].default_cache_behavior[0].max_ttl == 0 && alltrue([for error in aws_cloudfront_distribution.api[0].custom_error_response : error.error_caching_min_ttl == 0])
    error_message = "Neither authenticated content nor error responses may be cached."
  }
  assert {
    condition     = aws_cloudfront_distribution.api[0].default_cache_behavior[0].forwarded_values[0].headers == toset(["*"]) && aws_cloudfront_distribution.api[0].default_cache_behavior[0].forwarded_values[0].query_string && !aws_cloudfront_distribution.api[0].default_cache_behavior[0].compress
    error_message = "Authentication/query data must reach the API and streaming must not be compressed."
  }
  assert {
    condition     = aws_wafv2_web_acl.edge[0].scope == "CLOUDFRONT" && !aws_wafv2_web_acl.edge[0].visibility_config[0].sampled_requests_enabled && length(aws_wafv2_web_acl_association.api) == 0 && length(aws_ecs_service.worker) == 0
    error_message = "Rate limit real viewers at the edge, retain privacy and leave workers absent."
  }
}
run "add_custom_domain_to_same_cloudfront_ingress" {
  command = plan
  variables {
    ingress_mode = "aws_https"
    api_domain   = "api.staging.example.com"
    zone_id      = "Z0123456789"
  }
  assert {
    condition     = aws_lb.api.internal && length(aws_cloudfront_distribution.api) == 1 && aws_cloudfront_distribution.api[0].aliases == toset(["api.staging.example.com"]) && !aws_cloudfront_distribution.api[0].viewer_certificate[0].cloudfront_default_certificate && aws_cloudfront_distribution.api[0].viewer_certificate[0].minimum_protocol_version == "TLSv1.2_2021"
    error_message = "Adding a domain must keep the private origin and use a modern custom certificate."
  }
}
run "reject_custom_domain_without_zone" {
  command = plan
  variables {
    ingress_mode = "aws_https"
    zone_id      = null
  }
  expect_failures = [var.zone_id]
}

run "reject_other_environment_image_in_shared_account" {
  command = plan
  variables {
    api_image = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-production/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  expect_failures = [var.api_image]
}
run "reject_foreign_account_oidc_provider" {
  command = plan
  variables {
    github_oidc_provider_arn = "arn:aws:iam::999999999999:oidc-provider/token.actions.githubusercontent.com"
  }
  expect_failures = [var.github_oidc_provider_arn]
}
run "reject_public_task_addresses_in_production" {
  command = plan
  variables {
    environment         = "production"
    service_egress_mode = "public_ip"
  }
  expect_failures = [var.service_egress_mode]
}

run "private_upload_runtime_and_version_cleanup" {
  command = plan
  variables {
    api_image = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  # Supply only external computed identities needed to evaluate the API template.
  # Keep its mount, environment and IAM policy expressions under test unchanged.
  override_resource {
    target = aws_lb.api
    values = {
      dns_name = "private.example.internal"
      arn      = "arn:aws:elasticloadbalancing:eu-central-1:123456789012:loadbalancer/app/test/1111111111111111"
    }
    override_during = plan
  }
  override_resource {
    target = aws_kms_key.data
    values = {
      arn = "arn:aws:kms:eu-central-1:123456789012:key/11111111-1111-1111-1111-111111111111"
    }
    override_during = plan
  }
  override_resource {
    target = aws_s3_bucket.private["data"]
    values = {
      id  = "buddy-staging-123456789012-data"
      arn = "arn:aws:s3:::buddy-staging-123456789012-data"
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
    target = aws_cognito_user_pool_client.desktop
    values = {
      id = "client123"
    }
    override_during = plan
  }
  override_resource {
    target = aws_sqs_queue.processing
    values = {
      url = "https://sqs.eu-central-1.amazonaws.com/123456789012/buddy-staging"
    }
    override_during = plan
  }
  override_resource {
    target = aws_secretsmanager_secret.database["api"]
    values = {
      arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:buddy-staging-api-test12"
    }
    override_during = plan
  }
  assert {
    condition = (
      jsondecode(aws_ecs_task_definition.service["api"].container_definitions)[0].readonlyRootFilesystem &&
      jsondecode(aws_ecs_task_definition.service["api"].container_definitions)[0].user == "10001:10001" &&
      jsondecode(aws_ecs_task_definition.service["api"].container_definitions)[0].mountPoints[0].containerPath == "/var/lib/buddy-upload" &&
      !jsondecode(aws_ecs_task_definition.service["api"].container_definitions)[0].mountPoints[0].readOnly
    )
    error_message = "Uploads need dedicated writable scratch while the API remains non-root with a read-only root."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.upload_versions.policy).Statement[0].Condition.StringLike["s3:prefix"] == "sources/*" &&
      jsondecode(aws_iam_role_policy.upload_versions.policy).Statement[1].Resource == "${aws_s3_bucket.private["data"].arn}/sources/*" &&
      toset(jsondecode(aws_iam_role_policy.upload_versions.policy).Statement[1].Action) == toset(["s3:GetObjectVersion", "s3:DeleteObjectVersion"])
    )
    error_message = "Version-aware upload cleanup must be restricted to private source objects."
  }
  assert {
    condition = (
      [for item in jsondecode(aws_ecs_task_definition.service["api"].container_definitions)[0].environment : item.value if item.name == "BUDDY_UPLOADS_ENABLED"] == ["true"] &&
      [for item in jsondecode(aws_ecs_task_definition.service["api"].container_definitions)[0].environment : item.value if item.name == "BUDDY_DATA_KMS_KEY_ARN"] == [aws_kms_key.data.arn]
    )
    error_message = "Upload workload configuration must use this environment's private KMS key."
  }
}
