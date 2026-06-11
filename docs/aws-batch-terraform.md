# AWS Batch Terraform Deployment Guide

This guide walks you through provisioning the AWS Batch infrastructure for OSimFlow using the Terraform module in `infra/aws/terraform/`.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.5
- AWS CLI configured with credentials (`aws configure` or environment variables)
- An AWS account with permissions to create VPC, S3, IAM, and Batch resources

## What Gets Created

| Resource | Purpose |
|---|---|
| VPC + subnets | Reuses the default VPC (no extra cost) |
| Security group | Egress-only; Batch tasks can reach ECR, S3, and CWL |
| S3 bucket | Campaign artifact storage (versioned, encrypted, public access blocked) |
| IAM instance profile | For EC2 instances in the Batch compute environment |
| IAM task role | Application permissions: S3 read/write (bucket-scoped), CloudWatch Logs |
| IAM task-execution role | ECS agent: ECR image pull + CloudWatch Logs |
| IAM Batch service role | AWS Batch management |
| Batch compute environment | Managed EC2 or Spot, scales 0–256 vCPUs |
| Batch job queue | Single priority queue |
| Batch job definition | `nrel/openstudio` container with configurable vCPU/memory |

## Quick Start

### 1. Initialize Terraform

```bash
cd infra/aws/terraform
terraform init
```

### 2. Review the plan

```bash
terraform plan
```

### 3. Apply

```bash
terraform apply
```

### 4. Note the outputs

After apply, Terraform prints the queue ARN, job definition name, and S3 bucket. You'll need these to run campaigns:

```bash
terraform output job_definition_name
# e.g. osimflow-dev-openstudio-job
```

### 5. Run a campaign

```bash
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-dev-job-queue \
  --aws-batch-job-definition osimflow-dev-openstudio-job \
  --openstudio_version 3.5.0 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

## Configuration

### Key Variables

| Variable | Default | Description |
|---|---|---|
| `region` | `us-east-1` | AWS region |
| `project_name` | `osimflow` | Resource name prefix |
| `environment` | `dev` | Environment label (dev/staging/prod) |
| `openstudio_version` | `3.5.0` | Container image tag |
| `use_spot` | `true` | Use Spot instances (60-90% cheaper) |
| `max_vcpus` | `256` | Maximum compute capacity |
| `job_vcpus` | `2` | vCPUs per simulation job |
| `job_memory_mb` | `4096` | Memory (MiB) per simulation job |
| `job_timeout_seconds` | `14400` | Max wall-clock per job (4 hours) |

### Spot vs On-Demand

Set `use_spot = true` (default) for cost-optimized runs. Jobs may be interrupted when Spot capacity is reclaimed; OSimFlow retries automatically. For critical runs where interruption is unacceptable, set `use_spot = false`.

### Examples

Two ready-made configurations are provided:

```bash
# On-demand (predictable cost)
cd infra/aws/terraform/examples/basic
terraform init && terraform apply

# Spot (cost-optimized)
cd infra/aws/terraform/examples/spot
terraform init && terraform apply
```

## Security

- **No hardcoded secrets.** Credentials come from the IAM role attached to the compute environment.
- **Least-privilege IAM.** The task role has S3 access scoped to the campaign bucket and CloudWatch Logs scoped to the Batch log group. The task-execution role uses the AWS-managed `AmazonECSTaskExecutionRolePolicy`.
- **S3 bucket** has versioning, AES256 encryption, and full public access blocking.
- **No inbound rules** on the security group — Batch tasks are egress-only.

## Production Considerations

1. **Remote state.** Add an S3 + DynamoDB backend for state locking:
   ```hcl
   terraform {
     backend "s3" {
       bucket         = "osimflow-terraform-state"
       key            = "infra/terraform.tfstate"
       region         = "us-east-1"
       dynamodb_table = "osimflow-tf-lock"
       encrypt        = true
     }
   }
   ```

2. **OIDC authentication.** For CI/CD, use GitHub OIDC (`aws-actions/configure-aws-credentials`) instead of long-lived access keys. See the nightly E2E workflow in `.github/workflows/aws-batch-e2e.yml`.

3. **Cost monitoring.** Set `desired_vcpus = 0` for dev environments so compute scales to zero when idle. Add AWS Budgets alerts for production.

4. **Log retention.** Add a CloudWatch Logs retention policy to avoid indefinite log storage costs.

## Teardown

```bash
terraform destroy
```

> **Warning:** this deletes all created resources. Set `s3_force_destroy = true` to remove the S3 bucket even if it contains objects.
