# =============================================================================
# OSimFlow AWS Batch Infrastructure — Production Hardening
# =============================================================================
# Adds:
#   • OIDC identity federation (GitHub Actions + workload identity)
#   • CloudWatch log retention policy
#   • Cost anomaly and budget alerts
#   • DynamoDB encryption (for Terraform state lock table)
#
# Remote state (S3 backend with DynamoDB locking) is configured in versions.tf.
# =============================================================================

# ---------------------------------------------------------------------------
# 1. CloudWatch log group with configurable retention
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "batch" {
  name              = "/aws/batch/${local.name_prefix}"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "${local.name_prefix}-batch-logs"
  }
}

# ---------------------------------------------------------------------------
# 2. DynamoDB table for Terraform state locking (referenced by backend)
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "terraform_locks" {
  name         = "osimflow-terraform-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Name = "${local.name_prefix}-terraform-locks"
  }
}

# ---------------------------------------------------------------------------
# 3. OIDC identity provider — federated GitHub Actions workload identity
# ---------------------------------------------------------------------------
# Allows GitHub Actions workflows to assume an IAM role without storing
# long-lived AWS credentials. Used by the nightly aws-batch-e2e workflow.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "github_oidc_assume_role" {
  statement {
    effect = "Allow"

    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "StringEquals:sub"
      values = [
        "repo:anchapin/OSimFlow:ref:refs/heads/main",
        "repo:anchapin/OSimFlow:pull_request",
      ]
    }

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "iat-normally-openstack:sub"
      values   = ["*"]
    }
  }
}

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = [
    "sts.amazonaws.com",
  ]

  thumbprint_list = ["6938fd4d98bab03faadb97b343472631e80fb7e1"]
}

resource "aws_iam_role" "github_actions" {
  name               = "${local.name_prefix}-github-actions-role"
  assume_role_policy = data.aws_iam_policy_document.github_oidc_assume_role.json
}

# Scoped permissions for the GitHub Actions E2E workflow:
#   • Batch submit/get describe jobs
#   • S3 read/write campaign artifacts
#   • CloudWatch Logs write
#   • ECR pull (read-only handled by the task-execution role)
data "aws_iam_policy_document" "github_actions" {
  statement {
    effect = "Allow"
    actions = [
      "batch:SubmitJob",
      "batch:DescribeJobs",
      "batch:ListJobs",
      "batch:TerminateJob",
    ]
    resources = ["*"]
  }

  statement {
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]
  }

  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "${aws_cloudwatch_log_group.batch.arn}:*",
    ]
  }

  statement {
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_actions" {
  name   = "${local.name_prefix}-github-actions-policy"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions.json
}

# ---------------------------------------------------------------------------
# 4. Cost alerts — budget at 80 % of monthly limit + daily anomaly
# ---------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly_cost" {
  name              = "${local.name_prefix}-monthly-cost"
  budget_type       = "COST"
  limit_amount      = tostring(var.monthly_budget_usd)
  limit_unit        = "USD"
  time_period_start = "2024-01-01"
  time_unit         = "MONTHLY"

  cost_types {
    include_reservations = true
    include_support      = true
    include_tax          = true
    include_other        = true
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_operator     = "GREATER_THAN"
    type                      = "ACTUAL"
    trigger_type              = "ACTUAL"
    subscription_type         = "EMAIL"
    recipient_email_addresses = var.alert_email_addresses
  }
}

resource "aws_cloudwatch_metric_alarm" "daily_cost_anomaly" {
  alarm_name          = "${local.name_prefix}-daily-cost-anomaly"
  comparison_operator = "LessThanLowerThreshold"
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 0
  treat_missing_data  = "BREACHING"

  metric_query {
    id          = "anomalyDetection"
    expression  = "ANOMALY_DETECTION_BAND(monthly_cost, 2)"
    label       = "Expected Monthly Cost"
    return_data = true
  }

  metric_query {
    id = "monthly_cost"
    metric {
      namespace   = "AWS/Billing"
      metric_name = "EstimatedCharges"
      period      = 21600 # 6 hours
      stat        = "Maximum"
      dimensions = {
        ServiceName = "AWS Batch"
        Currency    = "USD"
      }
    }
  }

  actions_enabled = true
  ok_actions      = []

  tags = {
    Name = "${local.name_prefix}-daily-cost-anomaly"
  }
}
