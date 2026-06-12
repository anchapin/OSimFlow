output "batch_job_queue_arn" {
  description = "ARN of the Batch job queue"
  value       = aws_batch_job_queue.osimflow.arn
}

output "batch_job_definition_arn" {
  description = "ARN of the Batch job definition"
  value       = aws_batch_job_definition.osimflow.arn
}

output "batch_job_definition_name" {
  description = "Name of the Batch job definition (used by the osimflow CLI)"
  value       = aws_batch_job_definition.osimflow.name
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket for campaign artifacts"
  value       = aws_s3_bucket.artifacts.id
}

output "s3_bucket_arn" {
  description = "ARN of the S3 bucket for campaign artifacts"
  value       = aws_s3_bucket.artifacts.arn
}

output "vpc_id" {
  description = "VPC ID used by Batch compute"
  value       = data.aws_vpc.default.id
}

output "subnet_ids" {
  description = "Subnet IDs used by Batch compute"
  value       = data.aws_subnets.default.ids
}

output "security_group_id" {
  description = "Security group ID for Batch tasks"
  value       = aws_security_group.batch.id
}

output "task_role_arn" {
  description = "ARN of the IAM role assumed by Batch containers"
  value       = aws_iam_role.task.arn
}

output "task_execution_role_arn" {
  description = "ARN of the ECS task-execution role (ECR pull + CWL)"
  value       = aws_iam_role.task_execution.arn
}

output "container_image" {
  description = "Full container image URI used by the job definition"
  value       = local.container_image
}

# ---------------------------------------------------------------------------
# Copy-paste ready CLI command — the "Easy Button" for first-time users.
# After `terraform apply`, this output prints an osimflow run command
# pre-populated with the correct queue, job definition, and OpenStudio
# version for this deployment.
# ---------------------------------------------------------------------------

output "osimflow_run_command" {
  description = "Ready-to-run osimflow CLI command with correct ARNs/flags for this deployment"
  value       = <<-EOT
  osimflow run \
    --executor aws_batch \
    --aws-batch-queue ${aws_batch_job_queue.osimflow.name} \
    --aws-batch-job-definition ${aws_batch_job_definition.osimflow.name} \
    --openstudio_version ${var.openstudio_version} \
    --input_variables variables.yml \
    --template_sim_package ./example_package \
    --n_samples 100 \
    --outdir ./results
  EOT
}

output "osimflow_spot_command" {
  description = "Ready-to-run osimflow CLI command with Spot Instance cost guardrails enabled"
  value       = <<-EOT
  osimflow run \
    --executor aws_batch \
    --aws-batch-queue ${aws_batch_job_queue.osimflow.name} \
    --aws-batch-job-definition ${aws_batch_job_definition.osimflow.name} \
    --aws-batch-max-spot-price-usd 0.05 \
    --aws-batch-fallback-to-on-demand \
    --aws-batch-max-retries 3 \
    --openstudio_version ${var.openstudio_version} \
    --input_variables variables.yml \
    --template_sim_package ./example_package \
    --n_samples 100 \
    --outdir ./results
  EOT
}

output "region" {
  description = "AWS region for this deployment"
  value       = var.region
}
