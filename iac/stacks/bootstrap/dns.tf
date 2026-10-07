# Zona DNS del dominio. Vive en bootstrap porque NUNCA debe recrearse:
# una zona nueva recibe name servers distintos y habría que volver a
# cambiarlos en GoDaddy.
resource "aws_route53_zone" "main" {
  #checkov:skip=CKV2_AWS_38:DNSSEC requiere una llave KMS en us-east-1 y publicar el registro DS en el registrador (GoDaddy); es un endurecimiento valido para produccion, fuera del alcance del reto.
  #checkov:skip=CKV2_AWS_39:El query logging de DNS genera costo en CloudWatch y no aporta a los requisitos del reto.
  name    = var.domain
  comment = "Zona publica de ${var.project} (dominio registrado en GoDaddy)"

  lifecycle {
    prevent_destroy = true
  }
}
