# =============================================================================
# OSimFlow AWS Batch Infrastructure — Production Hardening
# =============================================================================
# Adds:
#   • OIDC identity federation (GitHub Actions + workload identity)
#   • CloudWatch log retention policy
#   • Cost anomaly and budget alerts
#
# Remote state (S3 backend with DynamoDB locking) is configured in versions.tf;
# the bucket and lock table are created by
# infra/aws/scripts/bootstrap-terraform-backend.sh (Terraform cannot manage its
# own backend).
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
# 2. OIDC identity provider — federated GitHub Actions workload identity
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
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:anchapin/OSimFlow:ref:refs/heads/main",
        "repo:anchapin/OSimFlow:pull_request",
      ]
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
      "batch:DescribeJobDefinitions",
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
# 3. Cost alerts — budget at 80 % of monthly limit + daily anomaly
# ---------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly_cost" {
  name              = "${local.name_prefix}-monthly-cost"
  budget_type       = "COST"
  limit_amount      = tostring(var.monthly_budget_usd)
  limit_unit        = "USD"
  time_period_start = "2024-01-01_00:00"
  time_unit         = "MONTHLY"

  # AWS requires at least one subscriber per notification.
  dynamic "notification" {
    for_each = length(var.alert_email_addresses) > 0 ? [1] : []
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = 80
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.alert_email_addresses
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "daily_cost_anomaly" {
  alarm_name          = "${local.name_prefix}-daily-cost-anomaly"
  comparison_operator = "LessThanLowerThreshold"
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold_metric_id = "anomalyDetection"
  treat_missing_data  = "breaching"

  metric_query {
    id          = "anomalyDetection"
    expression  = "ANOMALY_DETECTION_BAND(monthly_cost, 2)"
    label       = "Expected Monthly Cost"
    return_data = true
  }

  metric_query {
    id          = "monthly_cost"
    return_data = true # alarms on anomaly bands need both series to return data
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
