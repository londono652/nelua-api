# VPC en 3 zonas: subnets públicas (ALB y NAT) y privadas (nodos y pods).
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = var.project
  cidr = var.vpc_cidr
  azs  = var.azs

  public_subnets  = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, i)]
  private_subnets = [for i in range(3) : cidrsubnet(var.vpc_cidr, 4, i + 1)]

  enable_dns_hostnames = true

  # Costo vs. resiliencia: un NAT en la demo, uno por zona en producción.
  enable_nat_gateway     = true
  single_nat_gateway     = !var.nat_per_az
  one_nat_gateway_per_az = var.nat_per_az

  public_subnet_tags = {
    "kubernetes.io/role/elb" = 1
  }

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = 1
  }
}

# Gateway endpoint de S3 (gratis): las capas de las imágenes de ECR se
# descargan desde S3 sin pasar por el NAT, que cobra por GB.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc.private_route_table_ids

  tags = {
    Name = "${var.project}-s3"
  }
}
