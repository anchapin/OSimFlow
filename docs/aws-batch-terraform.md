# AWS Batch Terraform Deployment Guide

This guide walks you through provisioning the AWS Batch infrastructure for OSimFlow using the Terraform module in `infra/aws/terraform/`.

**Audience:** IT administrators setting up cloud infrastructure for an engineering team. No Terraform or DevOps experience is required — follow the steps in order and hand off the outputs at the end.

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

Review the resources that will be created. You should see approximately 12–15 resources.

### 3. Apply

```bash
terraform apply
```

Type `yes` when prompted.

### 4. After apply — copy your command

After `terraform apply` completes, Terraform prints a list of outputs. Look for `osimflow_run_command` — this is a **ready-to-copy** CLI command pre-populated with the correct queue name, job definition, and OpenStudio version for your deployment:

```
osimflow_run_command = <<-EOT
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-dev-job-queue \
  --aws-batch-job-definition osimflow-dev-openstudio-job \
  --openstudio_version 3.5.0 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
EOT
```

Copy this command and give it to the engineering team along with the handoff details in the next section.

> **Tip:** To print just the command without other outputs:
> ```bash
> terraform output -raw osimflow_run_command
> ```

## Configuration

### Key Variables

| Variable | Default | Description |
|---|---|---|
| `region` | `us-east-1` | AWS region |
| `project_name` | `osimflow` | Resource name prefix |
| `environment` | `dev` | Environment label (dev/staging/prod) |
| `openstudio_version` | `3.5.0` | Container image tag |
| `use_spot` | `true` | Use Spot instances (60–90% cheaper) |
| `max_vcpus` | `256` | Maximum compute capacity |
| `job_vcpus` | `2` | vCPUs per simulation job |
| `job_memory_mb` | `4096` | Memory (MiB) per simulation job |
| `job_timeout_seconds` | `14400` | Max wall-clock per job (4 hours) |

Override any variable via a `terraform.tfvars` file or the `-var` flag:

```bash
terraform apply -var="openstudio_version=3.9.0" -var="environment=prod"
```

### Spot Instance Cost Guardrails

The Terraform module defaults to Spot instances (`use_spot = true`), which are 60–90% cheaper than On-Demand. Spot instances can be reclaimed by AWS with two minutes of warning when capacity is tight.

**When to use Spot (default):**
- Batch simulation campaigns with hundreds or thousands of independent samples
- Runs where individual job interruptions are acceptable because OSimFlow retries automatically
- Development and testing environments

**When to use On-Demand (`use_spot = false`):**
- Time-critical runs where a two-minute interruption window is unacceptable
- Final production runs where the cost premium is justified by guaranteed completion
- Regulatory or audit scenarios requiring uninterrupted execution

**OSimFlow CLI flags for Spot cost control:**

| Flag | Default | Purpose |
|---|---|---|
| `--aws-batch-max-spot-price-usd` | (unset) | Maximum price per vCPU-hour in USD. Jobs will not launch if the Spot price exceeds this ceiling. |
| `--aws-batch-fallback-to-on-demand` | `false` | When set, falls back to On-Demand instances if Spot capacity is unavailable or retries are exhausted. |
| `--aws-batch-max-retries` | `3` | Maximum number of retries when a Spot job is interrupted. Each retry resubmits the same sample. |

The Terraform output `osimflow_spot_command` provides a ready-to-copy command with Spot guardrails enabled:

```bash
terraform output -raw osimflow_spot_command
```

This produces a command with `--aws-batch-max-spot-price-usd 0.05`, `--aws-batch-fallback-to-on-demand`, and `--aws-batch-max-retries 3` pre-configured.

**How retries work:** When AWS reclaims a Spot instance, the Batch job transitions to `FAILED` with a Spot interruption status. OSimFlow detects this and resubmits the same sample up to `--aws-batch-max-retries` times. If all retries are exhausted and `--aws-batch-fallback-to-on-demand` is set, the sample is resubmitted on an On-Demand instance.

### Cost-Tagging

All resources created by the Terraform module carry these default tags:

| Tag | Value | Purpose |
|---|---|---|
| `Project` | `var.project_name` | Group all OSimFlow resources in billing reports |
| `Environment` | `var.environment` | Distinguish dev/staging/prod spend |
| `ManagedBy` | `terraform` | Identify infrastructure managed by Terraform |

**Adding custom cost-center tags:** Pass additional tags via the `tags` variable:

```hcl
# terraform.tfvars
tags = {
  CostCenter = "BUILD-2024-Q3"
  Owner      = "jane@example.com"
  Department = "EnergyModeling"
}
```

These tags are merged with the defaults and applied to every resource. Use them to filter AWS Cost Explorer reports by cost center, team, or project.

### Examples

Two ready-made configurations are provided in `infra/aws/terraform/examples/`:

```bash
# On-demand (predictable cost, no interruptions)
cd infra/aws/terraform/examples/basic
terraform init && terraform apply

# Spot (cost-optimized, handles interruptions)
cd infra/aws/terraform/examples/spot
terraform init && terraform apply
```

## Handoff to Engineering

After running `terraform apply`, give the engineering team the following information:

| Item | How to get it | Example value |
|---|---|---|
| **Batch job queue name** | `terraform output batch_job_queue_arn` | `arn:aws:batch:us-east-1:123456789012:job-queue/osimflow-dev-job-queue` |
| **Batch job definition name** | `terraform output batch_job_definition_name` | `osimflow-dev-openstudio-job` |
| **S3 bucket name** | `terraform output s3_bucket_name` | `osimflow-dev-artifacts-a1b2c3d4` |
| **AWS region** | `terraform output region` | `us-east-1` |
| **OpenStudio version** | The value of `openstudio_version` | `3.5.0` |
| **Ready-to-run command** | `terraform output -raw osimflow_run_command` | (full osimflow CLI command) |

The engineering team needs:
1. **The `osimflow_run_command` output** — paste it into a terminal with `osimflow` installed and a `variables.yml` file ready.
2. **AWS credentials** — either an IAM user in the same account or an assumed role with `batch:SubmitJob`, `s3:PutObject`, and `s3:GetObject` permissions on the created bucket. On the Batch compute environment, credentials come from the IAM role automatically.
3. **The S3 bucket name** — used by the `--archive_intermediates` flag and for post-run artifact retrieval.

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
