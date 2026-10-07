# Repositorio de imágenes de la API.
resource "aws_ecr_repository" "api" {
  #checkov:skip=CKV_AWS_136:Las imagenes ya se cifran en reposo con AES-256 administrado por AWS. Una llave KMS propia agrega costo sin beneficio para una imagen que no contiene secretos.
  name = var.project

  # Tags inmutables: una vez publicada, la imagen de un commit no se puede
  # reemplazar. Lo que se probó en staging es exactamente lo que llega a prod.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Costo: conserva solo las 15 imágenes más recientes.
resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Conservar las 15 imagenes mas recientes"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 15
      }
      action = { type = "expire" }
    }]
  })
}
