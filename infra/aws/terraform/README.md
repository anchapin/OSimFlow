# OSimFlow AWS Batch Infrastructure

Terraform module that provisions the AWS resources required by OSimFlow's `AWSBatchExecutor`.

## Resources Created

| Resource | Description |
|---|---|
| VPC (default) | Reuses the account default VPC and subnets |
| Security Group | Egress-only SG for Batch tasks |
| S3 Bucket | Campaign artifact storage (versioned, encrypted, public access blocked) |
| IAM Roles | Instance profile, task role (S3 read/write), Batch service role |
| Batch Compute Environment | Managed EC2/Spot with configurable vCPU range |
| Batch Job Queue | Single priority queue backed by the compute environment |
| Batch Job Definition | `nrel/openstudio` container with 2 vCPU / 4 GB RAM |

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

## Security Notes

- **No hardcoded secrets** — credentials come from the IAM role on the compute environment.
- **Least-privilege IAM** — the task role only has S3 access to the campaign bucket.
- **Public access blocked** on the S3 bucket.
- **Server-side encryption** enabled on the S3 bucket.
- For production, add a remote state backend (S3 + DynamoDB) with state locking.
