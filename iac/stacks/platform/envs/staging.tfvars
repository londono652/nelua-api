# Staging: mismo código que producción, con el tamaño reducido.
# Es el ambiente que se despliega para probar la API de punta a punta.
environment = "staging"
vpc_cidr    = "10.0.0.0/16"

# Un solo NAT Gateway (ahorra unos 65 USD al mes). Si cae esa zona, los pods
# pierden salida a internet; en staging se acepta.
nat_per_az = false

# Staging se crea y se destruye a demanda, así que el balanceador no se protege.
alb_deletion_protection = false

# Igual con la tabla: el historial se reconstruye desde GitHub al volver a crearla.
dynamodb_deletion_protection = false

# Se pone en true solo mientras corre la prueba de carga desde dentro del clúster.
load_test_mode = false

# Staging usa API key: es el ambiente donde se prueba y se corre la prueba de
# carga. Producción usa Cognito (JWT validado en el ALB), el valor por defecto.
auth_mode = "api_key"
