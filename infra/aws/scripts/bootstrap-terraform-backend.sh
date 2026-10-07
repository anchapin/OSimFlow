#!/usr/bin/env bash
# bootstrap-terraform-backend.sh — Create the Terraform remote-state S3 bucket
# and DynamoDB lock table if they do not already exist (idempotent).
#
# Terraform cannot create its own backend (it must exist before `terraform
# init`), so this one-off script does it with the AWS CLI. Re-running is safe:
# existing resources are left untouched.
#
# Usage:
#   ./bootstrap-terraform-backend.sh
#   ./bootstrap-terraform-backend.sh --bucket my-state-bucket --region us-east-1
#
# Defaults match infra/aws/terraform/versions.tf. S3 bucket names are global:
# if the default name is taken by another account, pass --bucket and then
# `terraform init -reconfigure -backend-config="bucket=<name>"`.
#
# Requires: aws CLI v2 and authenticated credentials (e.g. AWS_PROFILE).

set -euo pipefail

BUCKET="osimflow-terraform-state"
TABLE="osimflow-terraform-locks"
REGION="us-east-1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket) BUCKET="$2"; shift 2 ;;
    --table) TABLE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    -h | --help)
      sed -n '2,16p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

echo "Account: $(aws sts get-caller-identity --query Account --output text)  Region: ${REGION}"

# --- S3 state bucket -------------------------------------------------------
if aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  echo "S3 bucket ${BUCKET}: already exists"
else
  echo "S3 bucket ${BUCKET}: creating"
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" >/dev/null
  else
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
      --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  fi
fi

# Re-applying these settings is harmless, so run them unconditionally.
aws s3api put-bucket-versioning --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "${BUCKET}" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "${BUCKET}" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# --- DynamoDB lock table ---------------------------------------------------
if aws dynamodb describe-table --table-name "${TABLE}" --region "${REGION}" >/dev/null 2>&1; then
  echo "DynamoDB table ${TABLE}: already exists"
else
  echo "DynamoDB table ${TABLE}: creating"
  aws dynamodb create-table --table-name "${TABLE}" --region "${REGION}" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --table-name "${TABLE}" --region "${REGION}"
fi

echo "Backend ready. Next: cd infra/aws/terraform && terraform init"
