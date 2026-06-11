# AWS Deployment

OSimFlow runs parametric building-energy simulation campaigns on **AWS Batch** using Terraform-managed infrastructure.

## Quick start

See the zero-to-running deployment guide:

- [`docs/aws-batch-terraform.md`](../../docs/aws-batch-terraform.md)

## Infrastructure

All Terraform modules live in the **main `infra/` tree**, not here:

| Component | Path |
|---|---|
| VPC, S3, IAM, Batch compute env | [`infra/aws/terraform/`](../../infra/aws/terraform/) |
| IAM roles (least-privilege) | [`infra/aws/terraform/iam.tf`](../../infra/aws/terraform/iam.tf) |
| Batch job definition | [`infra/aws/terraform/job-definition.tf`](../../infra/aws/terraform/job-definition.tf) |
| ECR mirror + lifecycle | [`infra/aws/terraform/ecr.tf`](../../infra/aws/terraform/ecr.tf) |
| ECR sync script | [`infra/aws/scripts/sync-openstudio-to-ecr.sh`](../../infra/aws/scripts/sync-openstudio-to-ecr.sh) |
| Terraform examples | [`infra/aws/terraform/examples/`](../../infra/aws/terraform/examples/) |

## Deploying a campaign

```bash
# 1. Deploy infrastructure (one-time)
cd infra/aws/terraform
terraform init
terraform apply

# 2. Run a campaign
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --openstudio_version 3.5.0 \
  --input_variables variables.yml \
  --n_samples 1000 \
  --outdir ./results
```

## Security

- IAM roles for EC2 compute environment only. No long-lived AWS access keys.
- Task role scoped to campaign S3 bucket and CloudWatch Logs.
- Task-execution role scoped to ECR image pulls.
- See [`infra/aws/terraform/iam.tf`](../../infra/aws/terraform/iam.tf) for the full policy definitions.

## Spot instances

OSimFlow supports Spot instance bidding with automatic fallback to on-demand:

```bash
osimflow run \
  --executor aws_batch \
  --aws-batch-max-spot-price-usd 0.05 \
  --aws-batch-fallback-to-on-demand \
  --aws-batch-max-retries 3 \
  ...
```

## Container image strategy

OSimFlow consumes the upstream `nrel/openstudio` image from Docker Hub. For production Batch jobs, mirror to ECR to avoid Docker Hub rate limits:

```bash
./infra/aws/scripts/sync-openstudio-to-ecr.sh \
  --repository <account-id>.dkr.ecr.<region>.amazonaws.com/osimflow-openstudio \
  --versions 3.4.0 3.5.0
```

See [`docs/container-image-strategy.md`](../../docs/container-image-strategy.md) for details.

## See also

- [Container image strategy](../../docs/container-image-strategy.md)
- [OSimFlow PRD — Cloud Security Practices](../../docs/OSimFlow.md)
- [osimflow-deploy README](../README.md)
