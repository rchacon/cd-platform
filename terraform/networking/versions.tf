terraform {
  required_version = ">= 1.15"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Partial configuration -- bucket/key/region/dynamodb_table are supplied
  # via `terraform init -backend-config=backend.hcl` (gitignored), using
  # the state_bucket_name/lock_table_name outputs from ../bootstrap. Left
  # empty here since the bucket name is account-specific and shouldn't be
  # hardcoded into version-controlled config.
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region
}
