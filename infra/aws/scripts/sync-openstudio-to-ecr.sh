#!/usr/bin/env bash
# sync-openstudio-to-ecr.sh — Pull nrel/openstudio from Docker Hub, push to ECR.
#
# Mirrors the upstream nrel/openstudio container image into one or more
# AWS ECR regions so that OSimFlow's AWS Batch jobs never hit Docker Hub
# rate limits.
#
# Usage:
#   ./sync-openstudio-to-ecr.sh --version 3.5.0 --region us-east-1
#   ./sync-openstudio-to-ecr.sh --version 3.5.0 --region us-east-1 --regions us-east-1,us-west-2,eu-west-1
#
# Requires: docker, aws CLI v2, authenticated AWS credentials.
#
# See docs/container-image-strategy.md for the full picture.

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
VERSION=""
REGION=""
REGIONS=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --version) VERSION="$2"; shift 2 ;;
    --region)  REGION="$2";  shift 2 ;;
    --regions) REGIONS="$2"; shift 2 ;;
    -h | --help)
      echo "Usage: $0 --version VERSION --region REGION [--regions r1,r2,...]"
      echo ""
      echo "  --version   OpenStudio version tag (e.g. 3.5.0). Required."
      echo "  --region    Primary AWS region for ECR. Required."
      echo "  --regions   Comma-separated list of regions to replicate to."
      echo "              Defaults to --region if omitted."
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

: "${VERSION:?Error: --version is required}"
: "${REGION:?Error: --region is required}"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SOURCE_IMAGE="nrel/openstudio:${VERSION}"
ECR_REPO="osimflow/openstudio"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Pull with exponential backoff to handle Docker Hub rate limiting.
# Retries up to 5 times with delays of 2, 4, 8, 16, 32 seconds.
pull_with_retry() {
  local image=$1
  local max_attempts=5
  local attempt=1

  while [[ $attempt -le $max_attempts ]]; do
    echo ":: Pull attempt ${attempt}/${max_attempts}: ${image}"
    if docker pull "$image"; then
      return 0
    fi

    local delay
    delay=$((2 ** attempt))
    echo ":: Pull failed. Retrying in ${delay}s..."
    sleep "$delay"
    attempt=$((attempt + 1))
  done

  echo ":: ERROR: Failed to pull ${image} after ${max_attempts} attempts" >&2
  return 1
}

# Ensure an ECR repository exists in the given region.
ensure_ecr_repo() {
  local region=$1
  local repo_name=$2

  if aws ecr describe-repositories \
       --repository-name "$repo_name" \
       --region "$region" \
       --output text \
       >/dev/null 2>&1; then
    echo ":: Repository ${repo_name} already exists in ${region}"
  else
    echo ":: Creating ECR repository ${repo_name} in ${region}"
    aws ecr create-repository \
      --repository-name "$repo_name" \
      --region "$region" \
      --image-scanning-configuration scanOnPush=true \
      --tags Key=ManagedBy,Value=osimflow-sync-script
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

echo "=== sync-openstudio-to-ecr ==="
echo ":: Source: ${SOURCE_IMAGE}"
echo ":: ECR repo: ${ECR_REPO}"

# Pull from Docker Hub (with retry)
pull_with_retry "$SOURCE_IMAGE"

# Determine target regions
IFS=',' read -ra REGION_LIST <<< "${REGIONS:-$REGION}"

echo ":: Target regions: ${REGION_LIST[*]}"

for r in "${REGION_LIST[@]}"; do
  echo ""
  echo "=== Processing region: ${r} ==="

  # Resolve account ID
  AWS_ACCOUNT_ID=$(aws sts get-caller-identity --region "$r" --query Account --output text)
  ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${r}.amazonaws.com"

  # Login to ECR
  echo ":: Logging in to ECR (${r})..."
  aws ecr get-login-password --region "$r" \
    | docker login --username AWS --password-stdin "$ECR_URI"

  # Ensure the repository exists
  ensure_ecr_repo "$r" "$ECR_REPO"

  # Tag and push
  FULL_TAG="${ECR_URI}/${ECR_REPO}:${VERSION}"
  echo ":: Tagging ${SOURCE_IMAGE} -> ${FULL_TAG}"
  docker tag "$SOURCE_IMAGE" "$FULL_TAG"

  echo ":: Pushing ${FULL_TAG}"
  docker push "$FULL_TAG"

  echo ":: Pushed ${FULL_TAG}"
done

echo ""
echo "=== Sync complete ==="
