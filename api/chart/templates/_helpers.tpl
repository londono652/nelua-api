{{- define "nelua-api.labels" -}}
app.kubernetes.io/name: nelua-api
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
La misma imagen corre como dos componentes: la API y el recolector. El
Service, el HPA y el PDB seleccionan solo los pods de la API.
*/}}
{{- define "nelua-api.selectorLabels" -}}
app.kubernetes.io/name: nelua-api
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: api
{{- end }}

{{- define "nelua-api.collectorSelectorLabels" -}}
app.kubernetes.io/name: nelua-api
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: collector
{{- end }}

{{/* Variables que comparten la API y el recolector. */}}
{{- define "nelua-api.commonEnv" -}}
- name: ENVIRONMENT
  value: {{ .Values.environment | quote }}
- name: AWS_REGION
  value: {{ .Values.awsRegion | quote }}
- name: AWS_DEFAULT_REGION
  value: {{ .Values.awsRegion | quote }}
- name: TABLE_NAME
  value: {{ .Values.store.tableName | default "nelua-api-local" | quote }}
- name: GITHUB_REPOS
  value: {{ join "," .Values.githubRepos | quote }}
{{- with .Values.store.endpoint }}
- name: DYNAMODB_ENDPOINT
  value: {{ . | quote }}
{{- end }}
{{- end }}

{{/*
Trazas (OpenTelemetry). Variables estándar del SDK; la app las lee en
app/tracing.py. Sin tracing.enabled no se define el endpoint y las trazas
quedan apagadas.
Uso: include "nelua-api.tracingEnv" (dict "root" . "service" "nelua-api" "sampler" "...")
*/}}
{{- define "nelua-api.tracingEnv" -}}
{{- if .root.Values.tracing.enabled }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .root.Values.tracing.endpoint | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ .service | quote }}
- name: OTEL_TRACES_SAMPLER
  value: parentbased_traceidratio
- name: OTEL_TRACES_SAMPLER_ARG
  value: {{ .sampleRatio | quote }}
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: OTEL_RESOURCE_ATTRIBUTES
  value: "k8s.pod.name=$(POD_NAME),k8s.namespace.name={{ .root.Release.Namespace }}"
{{- end }}
{{- end }}
