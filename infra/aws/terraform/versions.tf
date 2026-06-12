terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # -------------------------------------------------------------------------
  # Remote state — S3 with DynamoDB locking
  # -------------------------------------------------------------------------
  # State is encrypted at rest (S3 SSE-KMS) and versioned for rollback.
  #
  # PREREQUISITE (must exist before `terraform init`):
  #   • S3 bucket  : osimflow-terraform-state   (or custom via -backend-config)
  #   • DynamoDB table: osimflow-terraform-locks (with partition key LockID)
  #
  # To override at init time:
  #   terraform init -backend-config="bucket=your-bucket" -backend-config="key=prod/terraform.tfstate"
  # -------------------------------------------------------------------------
  backend "s3" {
    bucket         = "osimflow-terraform-state"
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "osimflow-terraform-locks"
  }
}
