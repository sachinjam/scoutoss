{{- define "scoutoss.fullname" -}}{{ .Release.Name }}{{- end -}}

{{- define "scoutoss.labels" -}}
app.kubernetes.io/part-of: scoutoss
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "scoutoss.selector" -}}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/name: {{ .name }}
{{- end -}}

{{- define "scoutoss.image" -}}
{{ .root.Values.global.imageRegistry }}/{{ .name }}:{{ .root.Values.global.imageTag }}
{{- end -}}

{{- define "scoutoss.secretEnv" -}}
- name: {{ .key }}
  valueFrom:
    secretKeyRef:
      name: {{ .root.Values.existingSecret }}
      key: {{ .key }}
{{- end -}}

{{- define "scoutoss.commonEnv" -}}
- { name: ENV, value: {{ .Values.global.env | quote }} }
- { name: LOG_LEVEL, value: {{ .Values.global.logLevel | quote }} }
- { name: DEFAULT_PROJECT, value: {{ .Values.global.defaultProject | quote }} }
- { name: OTEL_EXPORTER_OTLP_ENDPOINT, value: {{ .Values.external.otlpEndpoint | quote }} }
- { name: OPENSEARCH_URL, value: {{ .Values.external.opensearch.url | quote }} }
- { name: OPENSEARCH_USER, value: {{ .Values.external.opensearch.user | quote }} }
- { name: OPENSEARCH_VERIFY_CERTS, value: {{ .Values.external.opensearch.verifyCerts | quote }} }
{{ include "scoutoss.secretEnv" (dict "root" . "key" "OPENSEARCH_PASSWORD") }}
- { name: AUTH_ISSUER, value: {{ .Values.external.keycloak.issuer | quote }} }
- { name: AUTH_JWKS_URL, value: "{{ .Values.external.keycloak.internalUrl }}/protocol/openid-connect/certs" }
- { name: XNAT_URL, value: {{ .Values.external.xnat.url | quote }} }
- { name: XNAT_PUBLIC_URL, value: {{ .Values.external.xnat.publicUrl | quote }} }
{{- end -}}

{{- define "scoutoss.pgUrl" -}}
postgresql://{{ .user }}:$({{ .passwordVar }})@{{ .root.Values.external.postgres.host }}:{{ .root.Values.external.postgres.port }}/{{ .root.Values.external.postgres.database }}?sslmode={{ .root.Values.external.postgres.sslmode }}
{{- end -}}

{{- define "scoutoss.probes" -}}
readinessProbe:
  httpGet: { path: /ready, port: http }
  periodSeconds: 10
  failureThreshold: 3
livenessProbe:
  httpGet: { path: /health, port: http }
  initialDelaySeconds: 20
  periodSeconds: 20
  failureThreshold: 6
{{- end -}}
