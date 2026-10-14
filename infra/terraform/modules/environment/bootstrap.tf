# An operator-run, one-shot bootstrap task. Neither normal service nor GitHub deployment
# roles may read the RDS master secret or run this administrative task.
resource "aws_iam_role" "bootstrap" {
  name               = "${local.name}-database-bootstrap"
  assume_role_policy = local.ecs_trust
}
resource "aws_iam_role_policy" "bootstrap" {
  role = aws_iam_role.bootstrap.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = concat([aws_db_instance.main.master_user_secret[0].secret_arn], [for secret in aws_secretsmanager_secret.database : secret.arn]) },
    { Effect = "Allow", Action = ["secretsmanager:PutSecretValue"], Resource = [for secret in aws_secretsmanager_secret.database : secret.arn] },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.data.arn, Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com" } } }
  ] })
}
resource "aws_iam_role" "bootstrap_execution" {
  name               = "${local.name}-bootstrap-execution"
  assume_role_policy = local.ecs_trust
}
resource "aws_iam_role_policy" "bootstrap_execution" {
  role = aws_iam_role.bootstrap_execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"], Resource = aws_ecr_repository.service["api"].arn },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.service["bootstrap"].arn}:*" }
  ] })
}
resource "aws_ecs_task_definition" "bootstrap" {
  count                    = var.api_image == null ? 0 : 1
  family                   = "${local.name}-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.bootstrap_execution.arn
  task_role_arn            = aws_iam_role.bootstrap.arn
  container_definitions = jsonencode([{
    name            = "bootstrap", image = var.api_image, essential = true,
    user            = "10001:10001", readonlyRootFilesystem = true,
    linuxParameters = { initProcessEnabled = true, capabilities = { drop = ["ALL"] } },
    command         = ["python", "/opt/buddy/bootstrap_database.py"],
    environment = [
      { name = "BUDDY_AWS_REGION", value = var.region },
      { name = "BUDDY_DB_HOST", value = aws_db_instance.main.address },
      { name = "BUDDY_DB_ADMIN_SECRET", value = aws_db_instance.main.master_user_secret[0].secret_arn },
      { name = "BUDDY_DB_SECRET_ARNS", value = jsonencode({ for name, secret in aws_secretsmanager_secret.database : name => secret.arn }) },
      { name = "SSL_CERT_FILE", value = "/etc/ssl/certs/ca-certificates.crt" }
    ],
    logConfiguration = { logDriver = "awslogs", options = {
      awslogs-group  = aws_cloudwatch_log_group.service["bootstrap"].name,
      awslogs-region = var.region, awslogs-stream-prefix = "bootstrap"
    } }
  }])
}
output "bootstrap_task" { value = one(aws_ecs_task_definition.bootstrap[*].arn) }
