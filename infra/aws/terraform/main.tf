# =============================================================================
# OSimFlow AWS Batch Infrastructure
# =============================================================================
# Creates: VPC, S3 bucket, IAM roles, Batch compute environment,
# job queue, and job definition for running OpenStudio simulations.
# =============================================================================

provider "aws" {
  region = var.region

  default_tags {
    tags = merge(
      {
        Project     = var.project_name
        Environment = var.environment
        ManagedBy   = "terraform"
      },
      var.tags,
    )
  }
}

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  name_prefix = "${var.project_name}-${var.environment}"

  # Container image referencing the NREL-published OpenStudio CLI image.
  # The executor passes the tag via OSIMFLOW_CONTAINER env var at runtime.
  container_image = "nrel/openstudio:${var.openstudio_version}"
}

# =============================================================================
# VPC — reuse default VPC if it exists, otherwise create one
# =============================================================================

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_security_group" "batch" {
  name_prefix = "${local.name_prefix}-batch-"
  description = "Security group for OSimFlow Batch tasks"
  vpc_id      = data.aws_vpc.default.id

  # Outbound only — Batch tasks pull images and write to S3.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "${local.name_prefix}-batch-sg"
  }
}

# =============================================================================
# S3 — campaign artifact bucket
# =============================================================================

resource "aws_s3_bucket" "artifacts" {
  bucket_prefix = "${local.name_prefix}-artifacts-"
  force_destroy = var.s3_force_destroy

  tags = {
    Name = "${local.name_prefix}-campaign-artifacts"
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "cleanup-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# =============================================================================
# IAM — least-privilege roles for Batch
# =============================================================================

# --- ECS instance role (for the EC2 instances in the compute env) ---

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

# --- ECS task role (what the container itself assumes) ---

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

# Task permissions: S3 read/write for campaign artifacts only.
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

# --- Batch service role ---

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

# =============================================================================
# AWS Batch — compute environment
# =============================================================================

resource "aws_batch_compute_environment" "osimflow" {
  compute_environment_name = "${local.name_prefix}-compute-env"

  type = "MANAGED"

  compute_resources {
    type           = var.use_spot ? "SPOT" : "EC2"
    allocation_strategy = var.use_spot ? "SPOT_CAPACITY_OPTIMIZED" : "BEST_FIT"
    min_vcpus      = var.min_vcpus
    max_vcpus      = var.max_vcpus
    desired_vcpus  = var.desired_vcpus

    instance_type = var.instance_types
    instance_role = aws_iam_instance_profile.batch.arn

    security_group_ids = [aws_security_group.batch.id]
    subnets            = data.aws_subnets.default.ids

    ec2_configuration {
      image_type = "ECS_AL2"
    }

    tags = {
      Name = "${local.name_prefix}-batch-compute"
    }
  }

  service_role = aws_iam_role.batch_service.arn

  depends_on = [
    aws_iam_role_policy_attachment.batch_service,
  ]
}

# =============================================================================
# AWS Batch — job queue
# =============================================================================

resource "aws_batch_job_queue" "osimflow" {
  name     = "${local.name_prefix}-job-queue"
  state    = "ENABLED"
  priority = 1

  compute_environments = [
    aws_batch_compute_environment.osimflow.arn,
  ]
}

# =============================================================================
# AWS Batch — job definition (nrel/openstudio container)
# =============================================================================

resource "aws_batch_job_definition" "osimflow" {
  name = "${local.name_prefix}-openstudio-job"
  type = "container"

  retry_strategy {
    attempts = var.batch_job_retry_attempts
  }

  timeout {
    attempt_duration_seconds = 14400 # 4 hours max per sim
  }

  container_properties = jsonencode({
    image      = local.container_image
    vcpus      = 2
    memory     = 4096
    privileged = false

    jobRoleArn = aws_iam_role.task.arn

    environment = [
      { name = "OSIMFLOW_CONTAINER", value = local.container_image },
    ]

    mountPoints = []
    volumes     = []

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/aws/batch/${local.name_prefix}"
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "osimflow"
      }
    }
  })

  tags = {
    Name = "${local.name_prefix}-openstudio-job-def"
  }
}
