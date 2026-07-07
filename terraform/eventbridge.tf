# Retention-sweep placeholder: a scheduled Fargate task on the same image.
# S3 lifecycle already expires the objects; this task will delete the matching
# Postgres rows. Its real command is out of scope for now, so the rule ships
# DISABLED with a no-op command — flip state and the command together.

resource "aws_cloudwatch_event_rule" "retention_sweep" {
  name                = "${local.name}-retention-sweep"
  description         = "Placeholder for the Postgres retention sweep (rows past retention_days)"
  schedule_expression = "rate(1 day)"
  state               = "DISABLED"
}

resource "aws_cloudwatch_event_target" "retention_sweep" {
  rule     = aws_cloudwatch_event_rule.retention_sweep.name
  arn      = aws_ecs_cluster.main.arn
  role_arn = aws_iam_role.events.arn

  ecs_target {
    task_definition_arn = aws_ecs_task_definition.ingestion.arn
    launch_type         = "FARGATE"
    task_count          = 1

    network_configuration {
      subnets          = aws_subnet.private[*].id
      security_groups  = [aws_security_group.task.id]
      assign_public_ip = false
    }
  }

  input = jsonencode({
    containerOverrides = [{
      name    = "ingestion"
      command = ["/bin/true"] # placeholder — real sweep command is out of scope
    }]
  })
}

resource "aws_iam_role" "events" {
  name = "${local.name}-events"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "events.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "events" {
  name = "run-retention-sweep"
  role = aws_iam_role.events.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        # Any revision of the ingestion family.
        Resource = ["arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.ingestion.family}:*"]
      },
      {
        Effect    = "Allow"
        Action    = ["iam:PassRole"]
        Resource  = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      }
    ]
  })
}
