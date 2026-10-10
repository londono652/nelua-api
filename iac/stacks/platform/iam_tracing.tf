# Identidad del colector de trazas (iac/k8s/tracing/otel-collector.yaml).
#
# Solo puede escribir en X-Ray. La API y el recolector no tienen este permiso:
# mandan sus trazas al colector por OTLP dentro del clúster, y es él quien habla
# con AWS. Así la aplicación no sabe a dónde van las trazas.
resource "aws_iam_role" "otel_collector" {
  name               = "${local.name}-otel-collector"
  description        = "Rol del colector de trazas (OpenTelemetry -> X-Ray) en ${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
}

# Política administrada de AWS: PutTraceSegments, PutTelemetryRecords y las
# reglas de muestreo. Nada de lectura.
resource "aws_iam_role_policy_attachment" "otel_collector_xray" {
  role       = aws_iam_role.otel_collector.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_eks_pod_identity_association" "otel_collector" {
  cluster_name    = module.eks.cluster_name
  namespace       = "monitoring"
  service_account = "otel-collector"
  role_arn        = aws_iam_role.otel_collector.arn
}
