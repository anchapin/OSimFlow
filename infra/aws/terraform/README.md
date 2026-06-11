# OSimFlow AWS Batch Infrastructure

Terraform module that provisions the AWS resources required by OSimFlow's `AWSBatchExecutor`.

## File Layout

| File | Purpose |
|---|---|
| `main.tf` | Provider, VPC, S3 bucket, Batch compute environment, job queue |
| `iam.tf` | Least-privilege IAM roles (instance, task, task-execution, Batch service) |
| `job-definition.tf` | Batch job definition with parameterised container resources |
| `variables.tf` | Input variables |
| `outputs.tf` | Exported values (queue ARN, job definition name, bucket, etc.) |
| `versions.tf` | Terraform and provider version constraints |

## Resources Created

| Resource | Description |
|---|---|
| VPC (default) | Reuses the account default VPC and subnets |
| Security Group | Egress-only SG for Batch tasks |
| S3 Bucket | Campaign artifact storage (versioned, encrypted, public access blocked) |
| IAM Instance Profile | For EC2 instances in the compute environment |
| IAM Task Role | Application permissions: S3 (bucket-scoped) + CloudWatch Logs |
| IAM Task-Execution Role | ECS agent: ECR image pull + CloudWatch Logs |
| IAM Batch Service Role | AWS Batch management |
| Batch Compute Environment | Managed EC2/Spot with configurable vCPU range |
| Batch Job Queue | Single priority queue backed by the compute environment |
| Batch Job Definition | `nrel/openstudio` container with configurable vCPU / memory |

## Usage

```bash
# Initialize (no remote backend — local state for dev)
terraform -chdir=infra/aws/terraform init

# Validate
terraform -chdir=infra/aws/terraform validate

# Plan (requires AWS credentials)
terraform -chdir=infra/aws/terraform plan

# Apply (creates real resources — requires confirmation)
terraform -chdir=infra/aws/terraform apply
```

## Examples

```bash
# On-demand (predictable cost)
cd infra/aws/terraform/examples/basic
terraform init && terraform apply

# Spot (cost-optimized)
cd infra/aws/terraform/examples/spot
terraform init && terraform apply
```

## Variables

| Name | Default | Description |
|---|---|---|
| `region` | `us-east-1` | AWS region |
| `project_name` | `osimflow` | Resource name prefix |
| `environment` | `dev` | Environment label |
| `instance_types` | `["optimal"]` | EC2 instance types |
| `openstudio_version` | `3.5.0` | OpenStudio container tag |
| `use_spot` | `true` | Use Spot instances |
| `max_vcpus` | `256` | Max vCPUs for compute env |
| `min_vcpus` | `0` | Min vCPUs (idle capacity) |
| `desired_vcpus` | `8` | Desired vCPUs at steady state |
| `job_vcpus` | `2` | vCPUs per job container |
| `job_memory_mb` | `4096` | Memory (MiB) per job container |
| `job_timeout_seconds` | `14400` | Max wall-clock per job (4 hours) |
| `batch_job_retry_attempts` | `1` | Retry attempts on failure |
| `s3_force_destroy` | `false` | Force-destroy bucket even if non-empty |

## Security Notes

- **No hardcoded secrets** — credentials come from the IAM role on the compute environment.
- **Least-privilege IAM** — the task role has S3 access scoped to the campaign bucket and CWL scoped to the Batch log group. The task-execution role uses the AWS-managed `AmazonECSTaskExecutionRolePolicy`.
- **Public access blocked** on the S3 bucket.
- **Server-side encryption** enabled on the S3 bucket.
- For production, add a remote state backend (S3 + DynamoDB) with state locking.

See [docs/aws-batch-terraform.md](../../../docs/aws-batch-terraform.md) for the full deployment guide.
