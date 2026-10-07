# Valores de ESTE despliegue (la demo del reto).
#
# Los valores por defecto de variables.tf describen producción. Aquí solo va lo
# que se reduce a propósito para no gastar de más en una demo de pocas horas.
# El detalle está en docs/decisiones.md ("Producción y demo").

# Un solo NAT Gateway en lugar de uno por zona (ahorra unos 65 USD al mes).
# Riesgo aceptado en la demo: si cae esa zona, los pods pierden salida a internet.
nat_per_az = false
