# AWS Batch Deployment Guide

This guide walks you through deploying OSimFlow on AWS Batch. By the end you will have a fully functional Batch environment that can run parametric OpenStudio simulation campaigns.

**Estimated setup time:** 30–45 minutes (one-time). Subsequent campaigns require zero infrastructure changes.

**Estimated cost:** See [Cost Estimation Guide](../cost-estimation.md) for per-campaign pricing. A 100-sample Spot campaign costs ~$6.

---

## Prerequisites

| Requirement | Details |
|---|---|
| AWS account | With permissions to create IAM roles, Batch resources, ECR repos, and S3 buckets |
| AWS CLI | Installed and configured (`aws configure`) or IAM role-based auth |
| Docker | Installed locally (only needed if building a custom image) |
| OSimFlow | `pip install -e ".[aws]"` (brings in `boto3`) |
| OpenStudio version | Decide which `--openstudio_version` to target (e.g. `3.11.0`, `3.11.0`) |

---

## Architecture Overview

```
                        ┌──────────────────┐
                        │  Your Machine    │
                        │  (osimflow CLI)  │
                        └────────┬─────────┘
                                 │ boto3 submit_job
                                 ▼
┌─────────────────────────────────────────────────────┐
│  AWS Batch                                           │
│                                                      │
│  ┌──────────┐    ┌───────────────────────────────┐   │
│  │  Job      │───▶│  Compute Environment          │   │
│  │  Queue    │    │  (EC2 Spot/On-Demand + ECS)   │   │
│  └──────────┘    └───────────────────────────────┘   │
│       │                                              │
│       ▼                                              │
│  ┌──────────────────────────────┐                    │
│  │  Job Definition              │                    │
│  │  (nrel/openstudio container) │                    │
│  └──────────────────────────────┘                    │
│                                                      │
│  ┌──────────┐                                        │
│  │  S3       │  ← results, intermediates             │
│  └──────────┘                                        │
└─────────────────────────────────────────────────────┘
```

OSimFlow's `AWSBatchExecutor` submits one Batch job per sample in the `RUN_OPENSTUDIO_SIM` step. Each job runs the `nrel/openstudio` container (or a custom image). The executor polls `describe_jobs` with exponential backoff until the task completes.

---

## Step-by-Step Setup

### 1. Create an S3 Bucket for Campaign Data

```bash
BUCKET_NAME="osimflow-$(aws sts get-caller-identity --query Account --output text)-$(date +%s)"

aws s3api create-bucket \
  --bucket "$BUCKET_NAME" \
  --region us-east-1

# Enable versioning for reproducibility
aws s3api put-bucket-versioning \
  --bucket "$BUCKET_NAME" \
  --versioning-configuration Status=Enabled
```

Upload your template simulation package and variables file:

```bash
aws s3 cp ./example_package s3://$BUCKET_NAME/template_package --recursive
aws s3 cp variables.yml s3://$BUCKET_NAME/variables.yml
```

### 2. Create IAM Roles

OSimFlow requires two IAM roles: one for the Batch compute environment (EC2 instances) and one for the task itself.

#### 2a. ECS Instance Role (for EC2 compute hosts)

This role lets the EC2 instances in the compute environment pull containers, write logs, and access S3.

```bash
# Trust policy for EC2
cat > /tmp/ec2-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ec2.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name osimflow-ecsInstanceRole \
  --assume-role-policy-document file:///tmp/ec2-trust.json

# Attach managed policies
aws iam attach-role-policy \
  --role-name osimflow-ecsInstanceRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role

aws iam attach-role-policy \
  --role-name osimflow-ecsInstanceRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess
```

Create the instance profile:

```bash
aws iam create-instance-profile \
  --instance-profile-name osimflow-ecsInstanceProfile

aws iam add-role-to-instance-profile \
  --instance-profile-name osimflow-ecsInstanceProfile \
  --role-name osimflow-ecsInstanceRole
```

#### 2b. Batch Service Role

```bash
cat > /tmp/batch-service-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "batch.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name osimflow-AWSBatchServiceRole \
  --assume-role-policy-document file:///tmp/batch-service-trust.json

aws iam attach-role-policy \
  --role-name osimflow-AWSBatchServiceRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole
```

#### 2c. Task Role (for per-job S3 access)

This is the IAM role that individual Batch tasks assume. It controls what each simulation task can do.

```bash
cat > /tmp/task-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ecs-tasks.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name osimflow-taskRole \
  --assume-role-policy-document file:///tmp/task-trust.json
```

Create a least-privilege policy for the task role:

```bash
cat > /tmp/task-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadWriteCampaignBucket",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::osimflow-*",
        "arn:aws:s3:::osimflow-*/*"
      ]
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/batch/osimflow-*"
    },
    {
      "Sid": "ECRPull",
      "Effect": "Allow",
      "Action": [
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:GetAuthorizationToken"
      ],
      "Resource": "*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name osimflow-taskRole \
  --policy-name osimflow-task-policy \
  --policy-document file:///tmp/task-policy.json
```

### 3. Create a VPC Security Group (if needed)

If you don't have a suitable VPC, create a security group that allows outbound internet access (for pulling container images):

```bash
VPC_ID=$(aws ec2 describe-vpcs \
  --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)

aws ec2 create-security-group \
  --group-name osimflow-batch-sg \
  --description "OSimFlow Batch tasks" \
  --vpc-id "$VPC_ID"

SG_ID=$(aws ec2 describe-security-groups \
  --filters Name=group-name,Values=osimflow-batch-sg \
  --query 'SecurityGroups[0].GroupId' --output text)

# Outbound: allow all (for pulling images, S3 access)
aws ec2 authorize-security-group-egress \
  --group-id "$SG_ID" \
  --ip-permissions IpProtocol=-1,FromPort=-1,ToPort=-1,IpRanges=[{CidrIp=0.0.0.0/0}]
```

### 4. Create the Compute Environment

Use Spot instances for maximum cost savings (OSimFlow campaigns are embarrassingly parallel — ideal for Spot):

```bash
SUBNET_ID=$(aws ec2 describe-subnets \
  --filters Name=vpc-id,Values=$VPC_ID \
  --query 'Subnets[0].SubnetId' --output text)

aws batch create-compute-environment \
  --compute-environment-name osimflow-spot \
  --type MANAGED \
  --service-role arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/osimflow-AWSBatchServiceRole \
  --compute-resources '{
    "type": "SPOT",
    "allocationStrategy": "BEST_FIT_PROGRESSIVE",
    "minvCpus": 0,
    "maxvCpus": 256,
    "desiredvCpus": 0,
    "instanceTypes": ["m5", "c5", "m5a", "c5a"],
    "subnets": ["'"$SUBNET_ID"'"],
    "securityGroupIds": ["'"$SG_ID"'"],
    "instanceRole": "arn:aws:iam::'"$(aws sts get-caller-identity --query Account --output text)"':instance-profile/osimflow-ecsInstanceProfile",
    "tags": {"Project": "osimflow"}
  }'
```

For On-Demand (no interruption risk, higher cost):

```bash
aws batch create-compute-environment \
  --compute-environment-name osimflow-ondemand \
  --type MANAGED \
  --service-role arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/osimflow-AWSBatchServiceRole \
  --compute-resources '{
    "type": "EC2",
    "allocationStrategy": "BEST_FIT",
    "minvCpus": 0,
    "maxvCpus": 256,
    "desiredvCpus": 0,
    "instanceTypes": ["m5", "c5"],
    "subnets": ["'"$SUBNET_ID"'"],
    "securityGroupIds": ["'"$SG_ID"'"],
    "instanceRole": "arn:aws:iam::'"$(aws sts get-caller-identity --query Account --output text)"':instance-profile/osimflow-ecsInstanceProfile"
  }'
```

**Tip:** Set `maxvCpus` based on your campaign size. For a 1000-sample campaign with 4 vCPUs per task, `maxvCpus = 64` allows 16 concurrent simulations. See [Cost Estimation](../cost-estimation.md#batch-concurrency-optimization) for sizing guidance.

### 5. Create the Job Queue

```bash
aws batch create-job-queue \
  --job-queue-name osimflow-batch-queue \
  --state ENABLED \
  --priority 1 \
  --compute-environment-order '[
    {"order": 1, "computeEnvironment": "osimflow-spot"}
  ]'
```

### 6. Register the Job Definition

The job definition tells Batch how to run each OpenStudio simulation. The container image uses the NREL `nrel/openstudio` image from Docker Hub (see [OpenStudio Image Distribution](../openstudio-image-distribution.md)).

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws batch register-job-definition \
  --job-definition-name osimflow-openstudio-job-def \
  --type container \
  --container-properties '{
    "image": "nrel/openstudio:3.11.0",
    "vcpus": 4,
    "memory": 8192,
    "jobRoleArn": "arn:aws:iam::'"$ACCOUNT_ID"':role/osimflow-taskRole",
    "executionRoleArn": "arn:aws:iam::'"$ACCOUNT_ID"':role/osimflow-taskRole",
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/aws/batch/osimflow",
        "awslogs-region": "us-east-1",
        "awslogs-stream-prefix": "osimflow"
      }
    },
    "environment": [
      {"name": "OSIMFLOW_CONTAINER", "value": "nrel/openstudio:3.11.0"},
      {"name": "OSIMFLOW_OS_VERSION", "value": "3.11.0"}
    ]
  }'
```

The OSimFlow executor overrides the `image`, `vcpus`, `memory`, and `environment` at submit time via `containerOverrides`, so the job definition defaults are just fallbacks.

### 7. Create a CloudWatch Log Group

```bash
aws logs create-log-group --log-group-name /aws/batch/osimflow
```

---

## Running a Campaign

Once the infrastructure is set up, run campaigns from your local machine (or any host with `boto3` and AWS credentials):

```bash
# Basic: 100 samples, Spot, OpenStudio 3.11.0
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results \
  --openstudio_version 3.11.0

# With MLflow tracking
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --mlflow_tracking_uri http://your-mlflow-server:5000 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 500 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

### How OSimFlow Interacts with Batch

1. The `AWSBatchExecutor` calls `boto3.client("batch").submit_job()` once per sample.
2. Each job's `containerOverrides` sets `vcpus`, `memory` (MiB), and `environment` (including `OSIMFLOW_CONTAINER` and `OSIMFLOW_OS_VERSION`).
3. The `timeout.attemptDurationSeconds` is set from the Campaign's `time_min` parameter (default: 240 min = 14400 s for the simulation step).
4. The executor polls `describe_jobs` with exponential backoff (starting at 5 s, capped at 60 s) until the job reaches `SUCCEEDED` or `FAILED`.
5. The campaign writes `run.json` to `--outdir` with per-sample timing and status.

### Environment Variables

The executor injects these environment variables into each Batch task:

| Variable | Source | Example |
|---|---|---|
| `OSIMFLOW_CONTAINER` | Dynamic from `--openstudio_version` | `nrel/openstudio:3.11.0` |
| `OSIMFLOW_OS_VERSION` | `--openstudio_version` | `3.11.0` |

Your work scripts (in `bin/` or BYOS) can read these to select the correct OpenStudio binary or container.

---

## Security

### IAM Roles, Not Access Keys

OSimFlow's `AWSBatchExecutor` does **not** accept `aws_access_key_id` or `aws_secret_access_key`. Credentials come from the IAM role attached to the compute environment (see `osimflow/executors/__init__.py` — the constructor only takes `job_queue`, `job_definition`, and optional `region_name`).

This is a deliberate security decision (PRD §6 *Cloud Security Practices*): long-lived access keys in config files are a common attack vector. The IAM role on the EC2 compute environment provides temporary credentials that rotate automatically.

### Least-Privilege Policy

The task role policy above grants only:

- **S3:** Read/write on `osimflow-*` buckets only.
- **CloudWatch Logs:** Create log streams and put events under `/aws/batch/osimflow-*`.
- **ECR:** Pull images (needed for the NREL OpenStudio container).

If your campaign does not need to write to S3 (e.g., results are written to shared storage instead), further restrict the S3 permissions to `s3:GetObject` and `s3:ListBucket` only.

### Network

- The compute environment's security group only needs outbound access (pulling images, S3, Docker Hub).
- No inbound ports need to be opened — Batch tasks pull work, they don't receive connections.
- If your organization requires a VPC endpoint for S3, add it to the subnet configuration.

---

## Monitoring

### CloudWatch Logs

Each Batch task streams stdout/stderr to CloudWatch under `/aws/batch/osimflow`. View logs:

```bash
# List recent log streams
aws logs describe-log-streams \
  --log-group-name /aws/batch/osimflow \
  --order-by LastEventTime \
  --descending \
  --limit 10

# Read a specific stream
aws logs get-log-events \
  --log-group-name /aws/batch/osimflow \
  --log-stream-name "osimflow/default/abc123"
```

### Batch Console

The [AWS Batch console](https://console.aws.amazon.com/batch) shows:

- **Jobs** — status (SUBMITTED, PENDING, RUNNABLE, STARTING, RUNNING, SUCCEEDED, FAILED)
- **Job queue** — depth (how many jobs waiting)
- **Compute environment** — vCPU utilization, instance count

### run.json

OSimFlow writes `${outdir}/run.json` with per-step timing and per-sample status. This is the primary monitoring artifact:

```bash
# Check campaign progress
cat ./results/run.json | python -m json.tool | head -50

# Count completed vs failed samples
cat ./results/run.json | python -c "
import json, sys
data = json.load(sys.stdin)
samples = data.get('samples', {})
completed = sum(1 for s in samples.values() if s.get('status') == 'completed')
failed = sum(1 for s in samples.values() if s.get('status') == 'failed')
print(f'Completed: {completed}, Failed: {failed}, Total: {len(samples)}')
"
```

### S3 Results

After the campaign completes, results are in S3:

```bash
aws s3 ls s3://$BUCKET_NAME/results/ --recursive
aws s3 cp s3://$BUCKET_NAME/results/ ./results --recursive
```

---

## Troubleshooting

### Job Stuck in RUNNABLE

| Cause | Fix |
|---|---|
| No EC2 instances available | Increase `maxvCpus` on the compute environment, or check your vCPU service quota |
| Missing IAM instance profile | Verify `osimflow-ecsInstanceProfile` exists and has the `AmazonEC2ContainerServiceforEC2Role` policy |
| No subnet / security group | Verify the compute environment references valid subnet and SG IDs |
| Docker Hub rate limit | Pull the NREL image and push to your own ECR repo (see [Image Distribution](../openstudio-image-distribution.md)) |

### Job FAILED with "Essential container in task exited"

This is the generic ECS error. Check the CloudWatch log stream for the specific exit code:

```bash
aws logs get-log-events \
  --log-group-name /aws/batch/osimflow \
  --log-stream-name "<stream-name>" \
  --limit 100
```

Common causes:
- **Exit code 137**: OOM kill. Increase `memory_mb` in the campaign (edit `osimflow/campaign.py` or use BYOS).
- **Exit code 1**: OpenStudio error. Check `eplusout.err` in the task output.
- **Exit code 127**: Missing `openstudio` binary. The container image may not match the expected version.

### Boto3 NoRegionError

Set `AWS_REGION` (or `AWS_DEFAULT_REGION`) in your environment:

```bash
export AWS_REGION=us-east-1
```

### Boto3 NoCredentialsError

Run from an EC2 instance with an IAM role that includes Batch permissions, or configure local credentials:

```bash
aws configure  # sets up ~/.aws/credentials
```

**Never** put credentials in `variables.yml` or any OSimFlow config file.

### Docker Hub Rate Limit

Docker Hub limits anonymous pulls to 100 per 6 hours. For large campaigns (>100 samples), pull the image to ECR once:

```bash
# Pull and push to ECR
aws ecr create-repository --repository-name nrel/openstudio
ECR_URI=$(aws ecr describe-repositories --repository-names nrel/openstudio --query 'repositories[0].repositoryUri' --output text)

docker pull nrel/openstudio:3.11.0
docker tag nrel/openstudio:3.11.0 $ECR_URI:3.11.0
aws ecr get-login-password | docker login --username AWS --password-stdin $ECR_URI
docker push $ECR_URI:3.11.0
```

Then update the job definition to use `$ECR_URI:3.11.0` instead of `nrel/openstudio:3.11.0`.

### Spot Interruption

Spot interruptions are expected and handled gracefully by OSimFlow:

1. The interrupted job transitions to `FAILED` in Batch.
2. The Campaign's `except Exception` path logs the failure.
3. Re-run the campaign with the same `--outdir` — the `SQLiteCache` hits on every completed sample, so only the interrupted samples re-execute.

---

## Tear Down

To remove all OSimFlow AWS resources:

```bash
# Delete job queue
aws batch update-job-queue --job-queue osimflow-batch-queue --state DISABLED
aws batch delete-job-queue --job-queue osimflow-batch-queue

# Delete compute environment
aws batch update-compute-environment --compute-environment osimflow-spot --state DISABLED
aws batch delete-compute-environment --compute-environment osimflow-spot

# Delete job definition (deregisters the latest revision)
aws batch deregister-job-definition --job-definition osimflow-openstudio-job-def

# Delete IAM roles
aws iam delete-role-policy --role-name osimflow-taskRole --policy-name osimflow-task-policy
aws iam delete-role --role-name osimflow-taskRole
aws iam remove-role-from-instance-profile --instance-profile-name osimflow-ecsInstanceProfile --role-name osimflow-ecsInstanceRole
aws iam delete-instance-profile --instance-profile-name osimflow-ecsInstanceProfile
aws iam delete-role --role-name osimflow-ecsInstanceRole
aws iam detach-role-policy --role-name osimflow-AWSBatchServiceRole --policy-arn arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole
aws iam delete-role --role-name osimflow-AWSBatchServiceRole

# Delete log group
aws logs delete-log-group --log-group-name /aws/batch/osimflow

# Delete S3 bucket (warning: destroys all campaign data)
aws s3 rb s3://$BUCKET_NAME --force
```

---

## References

- [Cost Estimation Guide](../cost-estimation.md) — per-campaign pricing, Spot strategy, right-sizing
- [OpenStudio Image Distribution](../openstudio-image-distribution.md) — container image selection and versioning
- [AGENTS.md §10](../../AGENTS.md) — security policy (IAM roles, no access keys)
- [AWS Batch Documentation](https://docs.aws.amazon.com/batch/)
- [submitit Documentation](https://github.com/facebookincubator/submitit) — used by the Slurm executor
