terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Un estado por ambiente. La llave (key) no va aquí: se indica al inicializar,
  # con -backend-config=envs/<ambiente>.backend.hcl
  backend "s3" {
    bucket       = "nelua-api-tfstate-402365884764"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      Stack       = "platform"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
