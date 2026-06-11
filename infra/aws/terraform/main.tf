# =============================================================================
# OSimFlow AWS Batch Infrastructure
# =============================================================================
# Creates: VPC, S3 bucket, Batch compute environment, and job queue.
#
# IAM roles and job definition live in separate files:
#   iam.tf             — least-privilege IAM roles
#   job-definition.tf  — Batch job definition (nrel/openstudio container)
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
# AWS Batch — compute environment
# =============================================================================

resource "aws_batch_compute_environment" "osimflow" {
  compute_environment_name = "${local.name_prefix}-compute-env"

  type = "MANAGED"

  compute_resources {
    type                = var.use_spot ? "SPOT" : "EC2"
    allocation_strategy = var.use_spot ? "SPOT_CAPACITY_OPTIMIZED" : "BEST_FIT"
    min_vcpus           = var.min_vcpus
    max_vcpus           = var.max_vcpus
    desired_vcpus       = var.desired_vcpus

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
