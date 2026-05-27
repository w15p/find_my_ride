terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
  }

  # ── State backend ─────────────────────────────────────────────────────────────
  # For a single-developer project, LOCAL state is the pragmatic default.
  # There is no chicken-and-egg problem: just run terraform init + apply
  # and terraform.tfstate lands in this directory (gitignored).
  #
  # When you are ready to move to remote state (team use, CI, or just peace
  # of mind that the state file won't disappear with your laptop):
  #
  # 1. Create a bucket for state and a DynamoDB table for locking OUTSIDE of
  #    this configuration (or use a separate one-time bootstrap config):
  #
  #      aws s3api create-bucket \
  #        --bucket haystack-tfstate-<your-account-id> \
  #        --region us-west-1 \
  #        --create-bucket-configuration LocationConstraint=us-west-1
  #
  #      aws s3api put-bucket-versioning \
  #        --bucket haystack-tfstate-<your-account-id> \
  #        --versioning-configuration Status=Enabled
  #
  #      aws s3api put-bucket-encryption \
  #        --bucket haystack-tfstate-<your-account-id> \
  #        --server-side-encryption-configuration \
  #          '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  #
  #      aws dynamodb create-table \
  #        --table-name haystack-tfstate-lock \
  #        --attribute-definitions AttributeName=LockID,AttributeType=S \
  #        --key-schema AttributeName=LockID,KeyType=HASH \
  #        --billing-mode PAY_PER_REQUEST \
  #        --region us-west-1
  #
  # 2. Run: terraform init -migrate-state
  #    Terraform will copy local state into S3 automatically.
  #
  # 3. Uncomment this block:
  #
  # backend "s3" {
  #   bucket         = "haystack-tfstate-<your-account-id>"
  #   key            = "prod/terraform.tfstate"
  #   region         = "us-west-1"
  #   dynamodb_table = "haystack-tfstate-lock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = "us-west-1"

  default_tags {
    tags = {
      Project     = "haystack"
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}
