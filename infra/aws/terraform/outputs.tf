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

output "container_image" {
  description = "Full container image URI used by the job definition"
  value       = local.container_image
}
