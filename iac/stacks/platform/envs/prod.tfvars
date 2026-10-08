# Producción: los valores por defecto del stack ya son los de producción.
# Aquí solo va lo que identifica al ambiente.
environment = "prod"
vpc_cidr    = "10.1.0.0/16"

# Un NAT Gateway por zona: la caída de una zona no deja a las otras sin salida.
nat_per_az = true

# El balanceador no se puede borrar por accidente.
alb_deletion_protection = true

# La tabla tampoco: guarda el historial de despliegues.
dynamodb_deletion_protection = true

# Correo que recibe las alarmas de CloudWatch.
# alert_email = "guardia@ejemplo.com"
