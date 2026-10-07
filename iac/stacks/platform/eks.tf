data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  admin_policy = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  edit_policy  = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
}

# Clúster EKS en Auto Mode: AWS administra los nodos, su escalado y sus parches.
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.project
  kubernetes_version = var.kubernetes_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  endpoint_public_access = true

  compute_config = {
    enabled    = true
    node_pools = ["general-purpose", "system"]
  }

  # El HPA necesita metrics-server para leer el consumo de CPU de los pods.
  addons = {
    metrics-server = {}
  }

  # Accesos explícitos (no "quien creó el clúster"), para que el resultado sea
  # el mismo si aplica una persona o el pipeline.
  enable_cluster_creator_admin_permissions = false

  access_entries = {
    admin_user = {
      principal_arn = "arn:aws:iam::${local.account_id}:user/${var.admin_user}"
      policy_associations = {
        admin = {
          policy_arn   = local.admin_policy
          access_scope = { type = "cluster" }
        }
      }
    }

    pipeline_infra = {
      principal_arn = "arn:aws:iam::${local.account_id}:role/${var.project}-gha-infra"
      policy_associations = {
        admin = {
          policy_arn   = local.admin_policy
          access_scope = { type = "cluster" }
        }
      }
    }

    # El pipeline de la app solo puede desplegar en sus dos namespaces.
    pipeline_app = {
      principal_arn = "arn:aws:iam::${local.account_id}:role/${var.project}-gha-app"
      policy_associations = {
        deploy = {
          policy_arn = local.edit_policy
          access_scope = {
            type       = "namespace"
            namespaces = ["staging", "prod"]
          }
        }
      }
    }
  }
}
