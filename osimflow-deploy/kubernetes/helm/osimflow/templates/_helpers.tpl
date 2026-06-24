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

{{/*
Create the worker deployment name (optional — issue #583).
*/}}
{{- define "osimflow.worker.name" -}}
{{- printf "%s-worker" (include "osimflow.fullname" .) -}}
{{- end -}}

{{/*
Resolve the worker container image.
Uses worker.image if set, otherwise falls back to the openstudio image.
*/}}
{{- define "osimflow.worker.image" -}}
{{- .Values.worker.image | default (printf "%s:%s" .Values.openstudio.repository .Values.openstudio.version) -}}
{{- end -}}
