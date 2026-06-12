# =============================================================================
# OSimFlow — AWS Batch Job Definition
# =============================================================================
# Container job that runs `openstudio.cli` inside nrel/openstudio.
# The executor references this by name via --aws-batch-job-definition.
# =============================================================================

resource "aws_batch_job_definition" "osimflow" {
  name = "${local.name_prefix}-openstudio-job"
  type = "container"

  retry_strategy {
    attempts = var.batch_job_retry_attempts
  }

  timeout {
    attempt_duration_seconds = var.job_timeout_seconds
  }

  container_properties = jsonencode({
    image      = local.container_image
    vcpus      = var.job_vcpus
    memory     = var.job_memory_mb
    privileged = false

    jobRoleArn       = aws_iam_role.task.arn
    executionRoleArn = aws_iam_role.task_execution.arn

    environment = [
      { name = "OSIMFLOW_CONTAINER", value = local.container_image },
    ]

    mountPoints = []
    volumes     = []

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.batch.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "osimflow"
      }
    }
  })

  tags = {
    Name = "${local.name_prefix}-openstudio-job-def"
  }
}
