{{/*
Expand the name of the chart.
*/}}
{{- define "osimflow.name" -}}
osimflow
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "osimflow.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create the API deployment (optional — issue #138).
*/}}
{{- define "osimflow.api.name" -}}
{{- printf "%s-api" (include "osimflow.fullname" .) -}}
{{- end -}}
