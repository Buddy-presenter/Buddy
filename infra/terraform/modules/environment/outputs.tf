output "api_url" { value = "https://${var.api_domain}" }
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
    BUDDY_API_URL           = "https://${var.api_domain}"
    BUDDY_AWS_REGION        = var.region
    BUDDY_COGNITO_POOL_ID   = aws_cognito_user_pool.main.id
    BUDDY_COGNITO_CLIENT_ID = aws_cognito_user_pool_client.desktop.id
    BUDDY_COGNITO_DOMAIN    = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.region}.amazoncognito.com"
  }
}
