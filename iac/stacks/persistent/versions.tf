terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Estado remoto en el bucket que creó el stack bootstrap.
  # use_lockfile bloquea el estado con un archivo en S3 (sin DynamoDB).
  backend "s3" {
    bucket       = "nelua-api-tfstate-402365884764"
    key          = "persistent/terraform.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      Stack     = "persistent"
      ManagedBy = "terraform"
    }
  }
}
