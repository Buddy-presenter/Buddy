resource "aws_ecr_repository" "service" {
  for_each             = toset(["api", "worker"])
  name                 = "${local.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.data.arn
  }
}
resource "aws_cloudwatch_log_group" "service" {
  for_each          = toset(["api", "migration", "worker", "bootstrap"])
  name              = "/buddy/${var.environment}/${each.key}"
  retention_in_days = local.production ? 30 : 7
}
resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = local.production ? "enabled" : "disabled"
  }
}
locals {
  environment = [
    { name = "BUDDY_ENVIRONMENT", value = "production" },
    { name = "BUDDY_BIND_HOST", value = "0.0.0.0" },
    { name = "BUDDY_ALLOWED_HOSTS", value = "${join(",", distinct(compact([var.api_domain, local.api_host, aws_lb.api.dns_name])))},localhost,127.0.0.1" },
    { name = "BUDDY_AWS_REGION", value = var.region },
    { name = "BUDDY_COGNITO_POOL_ID", value = aws_cognito_user_pool.main.id },
    { name = "BUDDY_COGNITO_CLIENT_ID", value = aws_cognito_user_pool_client.desktop.id },
    { name = "BUDDY_COGNITO_DOMAIN", value = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.region}.amazoncognito.com" },
    { name = "BUDDY_DATA_BUCKET", value = aws_s3_bucket.private["data"].id },
    { name = "BUDDY_PROCESSING_QUEUE_URL", value = aws_sqs_queue.processing.url },
    { name = "SSL_CERT_FILE", value = "/etc/ssl/certs/ca-certificates.crt" },
  ]
  images = merge(var.api_image == null ? {} : { api = var.api_image, migration = var.api_image }, var.worker_image == null ? {} : { worker = var.worker_image })
}
resource "aws_ecs_task_definition" "service" {
  for_each                 = local.images
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.key == "worker" ? 1024 : 512
  memory                   = each.key == "worker" ? 2048 : 1024
  execution_role_arn       = aws_iam_role.execution[each.key].arn
  task_role_arn            = aws_iam_role.task[each.key].arn
  container_definitions    = jsonencode([merge({
    name                   = each.key, image = each.value, essential = true,
    readonlyRootFilesystem = true,
    user                   = "10001:10001",
    linuxParameters        = { initProcessEnabled = true, capabilities = { drop = ["ALL"] } },
    environment            = local.environment,
    secrets                = [{ name = "BUDDY_DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.database[each.key].arn}:url::" }],
    logConfiguration       = { logDriver = "awslogs", options = {
      awslogs-group  = aws_cloudwatch_log_group.service[each.key].name,
      awslogs-region = var.region, awslogs-stream-prefix = each.key
    } },
    portMappings = each.key == "api" ? [{ containerPort = 8000, protocol = "tcp" }] : [],
    stopTimeout  = 30,
  }, each.key == "migration" ? { command = ["python", "/opt/buddy/migrate_database.py"] } : {})])
}
resource "aws_ecs_service" "api" {
  count                              = var.api_image == null ? 0 : 1
  name                               = "api"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.service["api"].arn
  desired_count                      = var.start_services ? (local.production ? 2 : 1) : 0
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  health_check_grace_period_seconds  = 90
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = local.task_subnets
    security_groups  = [aws_security_group.service["api"].id]
    assign_public_ip = local.public_task_ip
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
  depends_on = [aws_lb_listener.https, aws_lb_listener.private, aws_iam_role_policy.execution]
}
resource "aws_ecs_service" "worker" {
  count            = var.worker_image == null ? 0 : 1
  name             = "worker"
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.service["worker"].arn
  desired_count    = var.start_services ? (local.production ? 2 : 1) : 0
  launch_type      = "FARGATE"
  platform_version = "1.4.0"
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = local.task_subnets
    security_groups  = [aws_security_group.service["worker"].id]
    assign_public_ip = local.public_task_ip
  }
  depends_on = [aws_iam_role_policy.execution]
}
