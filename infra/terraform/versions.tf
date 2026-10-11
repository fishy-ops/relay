terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state is required before a shared deploy. Uncomment once the bucket exists.
  # backend "s3" {
  #   bucket         = "relay-tfstate-<account-id>"
  #   key            = "relay/prod.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "relay-tfstate-lock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project     = "relay"
      environment = var.environment
      managed_by  = "terraform"
    }
  }
}
