{{/* Common labels */}}
{{- define "workload.labels" -}}
app.kubernetes.io/part-of: pas
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
pas.dev/lane: {{ .Release.Namespace }}
{{- end }}

{{- define "workload.appSelector" -}}
app.kubernetes.io/name: {{ .Values.app.name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "workload.forecasterName" -}}
{{ .Values.app.name }}-forecaster
{{- end }}

{{- define "workload.forecasterSelector" -}}
app.kubernetes.io/name: {{ include "workload.forecasterName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* Restricted pod security settings shared by all containers. */}}
{{- define "workload.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: 65532
runAsGroup: 65532
capabilities:
  drop: ["ALL"]
seccompProfile:
  type: RuntimeDefault
{{- end }}
