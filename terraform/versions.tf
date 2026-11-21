# Terraform alternative to template.yaml + deploy.ps1. Same resources, same
# code package (../src), same URLs. See README.md.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40, < 7.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }

  # State: local by default. For a team, keep it in S3 with locking, e.g.
  #
  # backend "s3" {
  #   bucket       = "my-terraform-state"
  #   key          = "testhelper-v1/terraform.tfstate"
  #   region       = "ap-southeast-2"
  #   use_lockfile = true   # Terraform >= 1.10; older: dynamodb_table = "..."
  #   encrypt      = true
  # }
  #
  # The state holds the SMS device keys (sensitive) - keep it private.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = var.tags
  }
}
