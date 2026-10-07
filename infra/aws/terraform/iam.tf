# =============================================================================
# OSimFlow — IAM Roles (least privilege)
# =============================================================================
# Three roles:
#   1. ECS instance role  — for EC2 instances in the Batch compute env
#   2. ECS task role       — permissions the *application* container gets
#   3. ECS task-exec role  — permissions the ECS agent needs to pull images
#                           and write CloudWatch Logs
# =============================================================================

# ---------------------------------------------------------------------------
# 1. ECS instance role (EC2 instances in the compute environment)
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "instance_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${local.name_prefix}-batch-instance-role"
  assume_role_policy = data.aws_iam_policy_document.instance_assume_role.json
}

resource "aws_iam_role_policy_attachment" "instance_ecs" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "batch" {
  name = "${local.name_prefix}-batch-instance-profile"
  role = aws_iam_role.instance.name
}

# ---------------------------------------------------------------------------
# 2. ECS task role — application-level permissions inside the container
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "task_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task" {
  name               = "${local.name_prefix}-batch-task-role"
  assume_role_policy = data.aws_iam_policy_document.task_assume_role.json
}

# S3 access scoped to the campaign artifacts bucket only.
data "aws_iam_policy_document" "task_s3" {
  statement {
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
      "s3:DeleteObject",
    ]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "task_s3" {
  name   = "${local.name_prefix}-task-s3-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_s3.json
}

# CloudWatch Logs — scoped to the Batch log group.
data "aws_iam_policy_document" "task_logs" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/batch/${local.name_prefix}*",
    ]
  }
}

resource "aws_iam_role_policy" "task_logs" {
  name   = "${local.name_prefix}-task-cwl"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_logs.json
}

# ---------------------------------------------------------------------------
# 3. ECS task-execution role — needed by the ECS agent to pull images
#    from ECR and write container logs to CloudWatch.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "task_execution" {
  name               = "${local.name_prefix}-task-execution-role"
  assume_role_policy = data.aws_iam_policy_document.task_assume_role.json
}

# AWS-managed policy for ECS task execution (ECR pull + CWL write).
resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Issue #1811: the ECS agent (execution role, not the task role) resolves
# job-definition secrets at container start.
locals {
  payload_secret_is_ssm = var.payload_secret_arn != null ? can(regex("^arn:[^:]+:ssm:", var.payload_secret_arn)) : false
}

data "aws_iam_policy_document" "task_execution_payload_secret" {
  count = var.payload_secret_arn == null ? 0 : 1

  statement {
    effect    = "Allow"
    actions   = local.payload_secret_is_ssm ? ["ssm:GetParameters", "ssm:GetParameter"] : ["secretsmanager:GetSecretValue"]
    resources = [var.payload_secret_arn]
  }

  dynamic "statement" {
    for_each = var.payload_secret_kms_key_arn == null ? [] : [1]
    content {
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = [var.payload_secret_kms_key_arn]
    }
  }
}

resource "aws_iam_role_policy" "task_execution_payload_secret" {
  count  = var.payload_secret_arn == null ? 0 : 1
  name   = "${local.name_prefix}-payload-secret"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.task_execution_payload_secret[0].json
}

# ---------------------------------------------------------------------------
# 4. Batch service role
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "batch_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["batch.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "batch_service" {
  name               = "${local.name_prefix}-batch-service-role"
  assume_role_policy = data.aws_iam_policy_document.batch_assume_role.json
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}
