# Container Image Strategy

OSimFlow's AWS Batch jobs run inside the `nrel/openstudio` container image published by NREL on Docker Hub. For production deployments this introduces two problems that an ECR mirror solves.

## Why Mirror to ECR?

| Problem | How ECR helps |
|---|---|
| **Docker Hub rate limits** — 100 pulls / 6 h for anonymous users, 200 pulls / 6 h for free accounts. A 1000-sample campaign exceeds this quickly. | ECR has no per-account pull limits within the same region. |
| **Reliability** — Docker Hub outages block simulations. | ECR runs within the AWS network; Batch jobs in the same region pull over a private path. |
| **Cost** — Cross-region Docker Hub traffic is not free. | Same-region ECR pulls are free; cross-region is cheaper than Docker Hub. |

## Sync Script

`infra/aws/scripts/sync-openstudio-to-ecr.sh` pulls a tagged image from Docker Hub and pushes it to ECR in one or more regions.

### Usage

```bash
# Single region
./infra/aws/scripts/sync-openstudio-to-ecr.sh \
  --version 3.11.0 \
  --region us-east-1

# Multi-region replication
./infra/aws/scripts/sync-openstudio-to-ecr.sh \
  --version 3.11.0 \
  --region us-east-1 \
  --regions us-east-1,us-west-2,eu-west-1
```

### Prerequisites

- Docker daemon running and logged in.
- AWS CLI v2 configured with credentials that can access ECR.
- The ECR repository must exist (provisioned by `infra/aws/terraform/ecr.tf`).

### Rate-Limit Handling

The script uses **exponential backoff** (2, 4, 8, 16, 32 seconds) on `docker pull` failures. Docker Hub returns `429 Too Many Requests` when the rate limit is hit; the backoff gives the window time to reset.

## ECR Lifecycle Policy

The Terraform resource in `infra/aws/terraform/ecr.tf` applies a lifecycle policy that **keeps the 5 most recent tagged images** whose tags start with `3.` (i.e. OpenStudio 3.x versions). Older images are automatically expired.

You can adjust the retention count by editing `countNumber` in the lifecycle policy.

## Multi-Region Strategy

For campaigns that run across multiple AWS regions:

1. **Primary region** — run the sync script with `--region <primary>`.
2. **Replica regions** — pass `--regions r1,r2,r3` to push to all regions in one invocation.
3. **Batch job definitions** in each region should reference the local ECR URI:
   ```
   <account-id>.dkr.ecr.<region>.amazonaws.com/osimflow/openstudio:<version>
   ```

## Cost Considerations

| Item | Cost |
|---|---|
| ECR storage | ~$0.10 / GB / month. Each OpenStudio image is ~2 GB. 5 images = ~$1/month. |
| ECR pull (same region) | Free. |
| ECR pull (cross-region) | Standard AWS data transfer rates apply. |
| Docker Hub pull | Free tier (rate-limited); Pro / Team plans remove limits. |

For most teams the ECR storage cost is negligible compared to compute costs.

## Terraform Provisioning

The ECR repository is defined in `infra/aws/terraform/ecr.tf` alongside the rest of the AWS Batch infrastructure. Apply with:

```bash
cd infra/aws/terraform
terraform init
terraform apply -target=aws_ecr_repository.openstudio -target=aws_ecr_lifecycle_policy.openstudio
```

Or apply the full stack to get the repository along with the Batch compute environment, job queue, and IAM roles.

## OSimFlow CLI Image (`anchapin/osimflow`)

In addition to the consumed `nrel/openstudio` simulation image, the
project publishes its own CLI image to Docker Hub:

```
docker.io/anchapin/osimflow:<version>
docker.io/anchapin/osimflow:latest
```

### What's inside

| Layer | Details |
|---|---|
| Base image | `python:3.12-slim` |
| OSimFlow | Installed from source with `[aws,slurm]` extras |
| `bin/` scripts | Copied to `/opt/osimflow/bin` (called by the work layer) |
| Entry point | `osimflow` CLI (`osimflow --help`) |

The Dockerfile lives at `docker/osimflow-cli/Dockerfile` and uses a
**multi-stage build** to keep the runtime image small — only the
installed packages and entry point are carried into the final stage.

### Building locally

From the repository root:

```bash
docker build -f docker/osimflow-cli/Dockerfile -t anchapin/osimflow:local .
```

Run a quick smoke test:

```bash
docker run --rm anchapin/osimflow:local --help
```

Run a campaign with local input files mounted via volume:

```bash
docker run --rm \
  -v $(pwd)/variables.yml:/workspace/variables.yml \
  -v $(pwd)/example_package:/workspace/example_package \
  -v $(pwd)/results:/workspace/results \
  anchapin/osimflow:local \
  run --executor local \
      --input_variables /workspace/variables.yml \
      --template_sim_package /workspace/example_package \
      --n_samples 5 \
      --outdir /workspace/results
```

### CI/CD pipeline

The workflow at `.github/workflows/osimflow-cli-image.yml` builds and
pushes the image automatically:

| Trigger | Action |
|---|---|
| Tag push `osimflow-v*` | Build `linux/amd64` + `linux/arm64`, push `<version>` + `latest` tags |
| `workflow_dispatch` | Manual build (optionally push) |

**Docker Hub authentication** uses repository secrets
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.

Build caches are stored on GitHub Actions cache (`type=gha`) so
subsequent builds are fast when only metadata changed.

## Updating the Batch Job Definition

After syncing a new version, update the Batch job definition to reference the new tag:

```bash
# In your terraform.tfvars or via -var
openstudio_version = "3.6.0"
```

Then re-apply the Terraform to update the job definition container image.
