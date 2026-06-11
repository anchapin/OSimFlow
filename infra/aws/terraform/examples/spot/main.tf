# =============================================================================
# OSimFlow — Spot instance example
# =============================================================================
# Cost-optimized configuration using Spot instances.
# NOT deployable as-is — requires real AWS credentials.
#
# Spot savings: typically 60-90% vs on-demand. Trade-off: jobs can be
# interrupted if Spot capacity is reclaimed. OSimFlow retries automatically
# (see batch_job_retry_attempts).

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

module "osimflow" {
  source = "../../"

  project_name       = "osimflow"
  environment        = "prod"
  region             = "us-east-1"
  openstudio_version = "3.5.0"

  # Spot instances — best cost, capacity-optimized allocation
  use_spot       = true
  instance_types = ["optimal"]

  # Compute capacity
  min_vcpus     = 0
  max_vcpus     = 256
  desired_vcpus = 8

  # Job sizing (heavier workloads)
  job_vcpus           = 4
  job_memory_mb       = 8192
  job_timeout_seconds = 14400 # 4 hours

  # Extra tags for cost tracking
  tags = {
    CostCenter = "building-sim"
    Team       = "energy-modeling"
  }
}

output "job_queue" {
  value = module.osimflow.batch_job_queue_arn
}

output "job_definition" {
  value = module.osimflow.batch_job_definition_name
}

output "s3_bucket" {
  value = module.osimflow.s3_bucket_name
}
