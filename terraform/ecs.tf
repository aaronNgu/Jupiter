resource "aws_ecs_cluster" "main" {
  name = local.name
}

resource "aws_cloudwatch_log_group" "ingestion" {
  name              = "/ecs/${local.name}-ingestion"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "ingestion" {
  family                   = "${local.name}-ingestion"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # CI builds on GitHub's amd64 runners; local first-push must use
  # `docker buildx build --platform linux/amd64` (Apple Silicon defaults to arm64).
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name = "ingestion"
    # Tracks the stable tag; CI pushes :sha + :latest then force-redeploys,
    # so the image reference here never drifts and applies never roll back a
    # CI deploy (the service-level ignore_changes below is the backstop).
    image     = "${aws_ecr_repository.ingestion.repository_url}:${var.image_tag}"
    essential = true

    portMappings = [{
      containerPort = var.container_port
      protocol      = "tcp"
    }]

    environment = [
      { name = "S3_BUCKET", value = aws_s3_bucket.screenshots.bucket },
      { name = "AWS_DEFAULT_REGION", value = var.aws_region },
      # S3_ENDPOINT_URL deliberately unset: boto3 defaults to real S3 and
      # auth comes from the task role — same code path as local MinIO dev.
    ]

    secrets = [{
      name      = "DATABASE_URL"
      valueFrom = aws_secretsmanager_secret.database_url.arn
    }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.ingestion.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ingestion"
      }
    }
  }])
}

resource "aws_ecs_service" "ingestion" {
  name            = "${local.name}-ingestion"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.ingestion.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # Migrations run on container start before the server binds.
  health_check_grace_period_seconds = 120

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.ingestion.arn
    container_name   = "ingestion"
    container_port   = var.container_port
  }

  # Applies never roll the service back over a CI deploy. Consequence: after
  # a Terraform change to the task definition, roll it out manually with
  #   aws ecs update-service --cluster luminque --service luminque-ingestion \
  #     --task-definition luminque-ingestion --force-new-deployment
  lifecycle {
    ignore_changes = [task_definition]
  }

  # The secret-version dependency is real, not cosmetic: the task definition
  # only references the secret's ARN, so without it ECS starts (and fails)
  # tasks for the ~7 min RDS takes to come up on a fresh apply.
  depends_on = [aws_lb_listener.http, aws_secretsmanager_secret_version.database_url]
}
