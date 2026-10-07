# =============================================================================
# OSimFlow — AWS Batch Job Definition
# =============================================================================
# Container job that runs `openstudio.cli` inside nrel/openstudio.
# The executor references this by name via --aws-batch-job-definition.
# =============================================================================

resource "aws_batch_job_definition" "osimflow" {
  name = "${local.name_prefix}-openstudio-job"
  type = "container"

  # Issue #1808: Fargate job definitions declare the platform capability.
  platform_capabilities = [var.compute_platform]

  retry_strategy {
    attempts = var.batch_job_retry_attempts
  }

  timeout {
    attempt_duration_seconds = var.job_timeout_seconds
  }

  container_properties = jsonencode(merge({
    image = local.container_image

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
    },
    # Issue #1808: Fargate requires resourceRequirements + networkConfiguration;
    # EC2 keeps the legacy vcpus/memory/privileged fields.
    local.is_fargate ? merge({
      resourceRequirements = [
        { type = "VCPU", value = tostring(var.job_vcpus) },
        { type = "MEMORY", value = tostring(var.job_memory_mb) },
      ]
      networkConfiguration         = { assignPublicIp = var.fargate_assign_public_ip ? "ENABLED" : "DISABLED" }
      fargatePlatformConfiguration = { platformVersion = "LATEST" }
      runtimePlatform = {
        operatingSystemFamily = "LINUX"
        cpuArchitecture       = "X86_64"
      }
      },
      var.fargate_ephemeral_storage_gib == null ? {} : {
        ephemeralStorage = { sizeInGiB = var.fargate_ephemeral_storage_gib }
      }
      ) : {
      vcpus      = var.job_vcpus
      memory     = var.job_memory_mb
      privileged = false
    },
    # Issue #1811: SubmitJob cannot carry secrets; inject the task-payload
    # HMAC secret here so the execution role resolves it at container start.
    var.payload_secret_arn == null ? {} : {
      secrets = [
        { name = "OSIMFLOW_TASK_PAYLOAD_SECRET", valueFrom = var.payload_secret_arn },
      ]
    }
  ))

  tags = {
    Name = "${local.name_prefix}-openstudio-job-def"
  }
}
