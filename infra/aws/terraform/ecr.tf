# ECR repository for mirrored OpenStudio images.
#
# The sync-openstudio-to-ecr.sh script pushes nrel/openstudio images
# here so that AWS Batch jobs pull from ECR instead of Docker Hub,
# avoiding rate limits and improving reliability.
#
# See docs/container-image-strategy.md for the full picture.

# tfsec:ignore:aws-ecr-repository-customer-key Low-sensitivity public images.
resource "aws_ecr_repository" "openstudio" {
  name                 = "osimflow/openstudio"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "${local.name_prefix}-openstudio"
  }
}

# Keep the last 5 tagged images matching "3.*" to avoid unbounded storage
# growth. Older versions are expired automatically.
resource "aws_ecr_lifecycle_policy" "openstudio" {
  repository = aws_ecr_repository.openstudio.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 5 tagged OpenStudio images"
        action = {
          type = "expire"
        }
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["3."]
          countType     = "imageCountMoreThan"
          countNumber   = 5
        }
      },
    ]
  })
}
