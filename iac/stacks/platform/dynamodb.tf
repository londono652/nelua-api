# Almacén compartido entre el recolector y la API, uno por ambiente.
#
# Guarda dos cosas (ver api/src/app/store.py):
#   - el historial de despliegues de GitHub, para calcular la tasa de éxito en
#     ventanas de 7 y 30 días. Cada registro expira solo a los 90 días (TTL);
#   - la última foto ya calculada de cada vista, que es lo único que leen los
#     pods de la API.
#
# Bajo demanda (PAY_PER_REQUEST): la carga es pequeña y estable. El recolector
# escribe unas pocas veces por minuto y cada pod de la API lee cada 5 s, sin
# importar cuántas peticiones reciba. Con 30 pods son ~6 lecturas por segundo.
resource "aws_dynamodb_table" "api" {
  #checkov:skip=CKV_AWS_119:Cifrado en reposo con la llave administrada por AWS (activo por defecto). Una llave KMS propia agrega costo y gestión sin beneficio: la tabla no guarda datos sensibles.
  name         = local.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Permite volver a cualquier momento de los últimos 35 días.
  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = var.dynamodb_deletion_protection
}
