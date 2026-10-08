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
  description = "Dominio delegado a Route 53 (la zona la crea el stack bootstrap)"
  type        = string
  default     = "nelua.site"
}

variable "api_key_version" {
  description = "Versión de las API keys. Al subir el número, Terraform genera llaves nuevas (rotación)"
  type        = number
  default     = 1
}

variable "monthly_budget_usd" {
  description = "Presupuesto mensual de la cuenta en USD. La API lo expone en GET /v1/budget"
  type        = number
  default     = 300
}

variable "budget_alert_email" {
  description = "Correo que recibe los avisos del presupuesto (80 % gastado o pronóstico por encima del límite). Vacío: sin avisos"
  type        = string
  default     = ""
}
