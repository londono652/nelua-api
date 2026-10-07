variable "region" {
  description = "Región de AWS donde vive el proyecto"
  type        = string
  default     = "us-east-2"
}

variable "project" {
  description = "Nombre del proyecto; se usa como prefijo de los recursos"
  type        = string
  default     = "nelua-api"
}

variable "domain" {
  description = "Dominio comprado en GoDaddy y delegado a Route 53"
  type        = string
  default     = "nelua.site"
}

variable "github_owner" {
  description = "Usuario u organización de GitHub dueña del repositorio"
  type        = string
  default     = "londono652"
}

variable "repo" {
  description = "Repositorio único del proyecto (API, IaC y pipelines)"
  type        = string
  default     = "nelua-api"
}

variable "github_owner_id" {
  description = "ID numérico del usuario de GitHub (gh api users/<usuario> -q .id)"
  type        = string
}

variable "repo_id" {
  description = "ID numérico del repositorio (gh api repos/<usuario>/<repo> -q .id)"
  type        = string
}
