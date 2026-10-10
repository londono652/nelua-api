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

variable "auth_mode" {
  description = "Cómo se autentican los clientes: api_key (la API exige X-API-Key) o jwt (Cognito emite tokens y el ALB los valida antes de llegar a los pods)"
  type        = string
  default     = "jwt"

  validation {
    condition     = contains(["api_key", "jwt"], var.auth_mode)
    error_message = "auth_mode debe ser api_key o jwt."
  }
}

variable "api_consumers" {
  description = "Consumidores de la API con modo jwt: cada uno recibe su propio cliente de Cognito (client credentials)"
  type        = list(string)
  default     = ["tablero-plataforma"]
}

variable "slo" {
  description = "Objetivos de nivel de servicio a 30 días, en porcentaje. Las alarmas por consumo del presupuesto de error se calculan a partir de ellos"
  type = object({
    availability              = number # % de peticiones sin 5xx
    latency_target            = number # % de peticiones por debajo del umbral
    latency_threshold_seconds = number
    freshness                 = number # % de sincronizaciones buenas del recolector
  })
  default = {
    availability              = 99.9
    latency_target            = 99
    latency_threshold_seconds = 0.3
    freshness                 = 99
  }

  validation {
    condition     = alltrue([for v in [var.slo.availability, var.slo.latency_target, var.slo.freshness] : v > 90 && v < 100])
    error_message = "Los SLO van entre 90 y 100 (sin incluir 100: un SLO de 100 % no deja presupuesto de error)."
  }
}
