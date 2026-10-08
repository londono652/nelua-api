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
