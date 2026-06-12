variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name prefix for resources"
  type        = string
  default     = "osimflow"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,62}$", var.project_name))
    error_message = "project_name must be lowercase alphanumeric with hyphens, 2-64 chars, starting with a letter."
  }
}

variable "environment" {
  description = "Environment label (e.g. dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "instance_types" {
  description = "EC2 instance types for Batch compute"
  type        = list(string)
  default     = ["optimal"]
}

variable "openstudio_version" {
  description = "OpenStudio version tag used in the container image"
  type        = string
  default     = "3.5.0"
}

variable "use_spot" {
  description = "Use Spot instances for the compute environment"
  type        = bool
  default     = true
}

variable "max_vcpus" {
  description = "Maximum vCPUs for compute environment"
  type        = number
  default     = 256
}

variable "min_vcpus" {
  description = "Minimum vCPUs (warm idle capacity)"
  type        = number
  default     = 0
}

variable "desired_vcpus" {
  description = "Desired vCPUs at steady state"
  type        = number
  default     = 8
}

variable "batch_job_retry_attempts" {
  description = "Maximum retry attempts for Batch jobs"
  type        = number
  default     = 1
}

variable "job_vcpus" {
  description = "Number of vCPUs allocated to each Batch job container"
  type        = number
  default     = 2
}

variable "job_memory_mb" {
  description = "Memory (MiB) allocated to each Batch job container"
  type        = number
  default     = 4096
}

variable "job_timeout_seconds" {
  description = "Maximum wall-clock time per Batch job attempt (seconds)"
  type        = number
  default     = 14400 # 4 hours
}

variable "s3_force_destroy" {
  description = "Force destroy S3 bucket even if non-empty (dev only)"
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional tags applied to all resources"
  type        = map(string)
  default     = {}
}

variable "log_retention_days" {
  description = "CloudWatch log retention period in days"
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827], var.log_retention_days)
    error_message = "log_retention_days must be a valid CloudWatch Logs retention period (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827)."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget limit in USD for AWS Batch"
  type        = number
  default     = 500
}

variable "alert_email_addresses" {
  description = "Email addresses to receive cost alerts"
  type        = list(string)
  default     = []
}
