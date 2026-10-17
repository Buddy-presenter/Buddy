output "api_url" { value = local.api_url }
output "database_host" { value = aws_db_instance.main.address }
output "database_admin_secret" { value = aws_db_instance.main.master_user_secret[0].secret_arn }
output "database_secrets" { value = { for name, secret in aws_secretsmanager_secret.database : name => secret.arn } }
output "cluster" { value = aws_ecs_cluster.main.name }
output "private_subnets" { value = aws_subnet.private[*].id }
output "task_security_groups" { value = { for name, group in aws_security_group.service : name => group.id } }
output "tasks" { value = { for name, task in aws_ecs_task_definition.service : name => task.arn } }
output "ecr" { value = { for name, repository in aws_ecr_repository.service : name => repository.repository_url } }
output "github_roles" { value = { for name, role in aws_iam_role.github : name => role.arn } }
output "release_channel" {
  value = { bucket = aws_s3_bucket.private["releases"].id, prefix = "${var.environment}/", channel = local.production ? "stable" : "preview" }
}
output "desktop_configuration" {
  value = {
    BUDDY_API_URL           = local.api_url
    BUDDY_AWS_REGION        = var.region
    BUDDY_COGNITO_POOL_ID   = aws_cognito_user_pool.main.id
    BUDDY_COGNITO_CLIENT_ID = aws_cognito_user_pool_client.desktop.id
    BUDDY_COGNITO_DOMAIN    = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.region}.amazoncognito.com"
  }
}

output "deployment_configuration" {
  value = {
    environment = var.environment
    egress_mode = var.service_egress_mode
    account_id  = var.account_id
    region      = var.region
    cluster     = aws_ecs_cluster.main.name
    bucket      = aws_s3_bucket.private["releases"].id
    api_url     = local.api_url
    tasks       = { for name, task in aws_ecs_task_definition.service : name => task.arn }
    network     = {
      subnets        = local.task_subnets
      securityGroups = [aws_security_group.service["migration"].id]
      assignPublicIp = local.public_task_ip ? "ENABLED" : "DISABLED"
    }
  }
}
output "cloudfront_domain" {
  value = local.aws_https ? aws_cloudfront_distribution.api[0].domain_name : null
}
output "verification_resources" {
  value = {
    database_id        = aws_db_instance.main.identifier
    load_balancer_arn  = aws_lb.api.arn
    cloudfront_id      = local.aws_https ? aws_cloudfront_distribution.api[0].id : null
    api_security_group = aws_security_group.service["api"].id
    alb_security_group = aws_security_group.ingress.id
    buckets            = { for name, bucket in aws_s3_bucket.private : name => bucket.id }
    queue_url          = aws_sqs_queue.processing.url
  }
}

output "task_subnets" { value = local.task_subnets }
output "task_public_ip" { value = local.public_task_ip ? "ENABLED" : "DISABLED" }
