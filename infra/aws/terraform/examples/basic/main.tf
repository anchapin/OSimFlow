# =============================================================================
# OSimFlow — Basic (on-demand) example
# =============================================================================
# Minimal configuration using on-demand EC2 instances.
# NOT deployable as-is — requires real AWS credentials.

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
  environment        = "dev"
  region             = "us-east-1"
  openstudio_version = "3.5.0"

  # On-demand instances (cost-predictable, no interruption)
  use_spot       = false
  instance_types = ["optimal"]

  # Compute capacity
  min_vcpus     = 0
  max_vcpus     = 64
  desired_vcpus = 0 # scale to zero when idle

  # Job sizing
  job_vcpus           = 2
  job_memory_mb       = 4096
  job_timeout_seconds = 14400 # 4 hours
}

output "job_queue" {
  value = module.osimflow.batch_job_queue_arn
}

output "job_definition" {
  value = module.osimflow.batch_job_definition_name
}
