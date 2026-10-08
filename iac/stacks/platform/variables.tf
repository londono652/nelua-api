variable "region" {
  description = "Región de AWS"
  type        = string
  default     = "us-east-2"
}

variable "project" {
  description = "Nombre del proyecto; prefijo de recursos y de Parameter Store"
  type        = string
  default     = "nelua-api"
}

variable "domain" {
  description = "Dominio delegado a Route 53"
  type        = string
  default     = "nelua.site"
}

variable "environment" {
  description = "Ambiente que crea este stack. Cada ambiente tiene su propio archivo de valores y su propio estado (carpeta envs/)"
  type        = string

  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "El ambiente debe ser staging o prod."
  }
}

variable "vpc_cidr" {
  description = "Rango de direcciones de la VPC. Distinto por ambiente, para poder conectarlas entre sí si hiciera falta"
  type        = string
  default     = "10.0.0.0/16"
}

variable "azs" {
  description = "Zonas de disponibilidad, fijadas de forma explícita para que la red no cambie sola si AWS agrega una zona"
  type        = list(string)
  default     = ["us-east-2a", "us-east-2b", "us-east-2c"]
}

variable "nat_per_az" {
  description = "true = un NAT Gateway por zona (producción). false = uno solo (staging, más barato)"
  type        = bool
  default     = true
}

variable "kubernetes_version" {
  description = "Versión de Kubernetes del clúster EKS"
  type        = string
  default     = "1.34"
}

variable "admin_user" {
  description = "Usuario IAM con acceso de administrador al clúster (kubectl desde tu equipo)"
  type        = string
  default     = "infra_admin"
}

variable "app_port" {
  description = "Puerto en el que escucha el contenedor de la API"
  type        = number
  default     = 8000
}

variable "waf_rate_limit" {
  description = "Máximo de peticiones por IP en 5 minutos antes de bloquearla"
  type        = number
  default     = 2000
}

variable "load_test_cidrs" {
  description = "IPs (formato CIDR) exentas del WAF, para el generador de la prueba de carga"
  type        = list(string)
  default     = []
}

variable "load_test_mode" {
  description = "true = el WAF deja pasar las IPs de salida del propio clúster (NAT), para lanzar la prueba de carga desde dentro. Se vuelve a false al terminar"
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Correo que recibe las alarmas de CloudWatch. Vacío = se crean las alarmas sin suscripción"
  type        = string
  default     = ""
}

variable "alb_deletion_protection" {
  description = "Protección contra borrado del balanceador. Activa en producción; apagada en staging, que se crea y se destruye a demanda"
  type        = bool
  default     = true
}

variable "dynamodb_deletion_protection" {
  description = "Protección contra borrado de la tabla de DynamoDB. Activa en producción; apagada en staging"
  type        = bool
  default     = true
}
