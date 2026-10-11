{{- define "core.image" -}}
{{- if or (contains ":" .service.image) (contains "@" .service.image) -}}
{{ .service.image }}
{{- else -}}
{{ .service.image }}:{{ .service.tag | default (index .tags .name) | default "latest" }}
{{- end -}}
{{- end -}}

{{- define "core.public" -}}
{{- $public := dict -}}
{{- range $name, $service := .Values.services -}}
{{- if $service.public -}}{{- $_ := set $public $name $service -}}{{- end -}}
{{- end -}}
{{- toJson $public -}}
{{- end -}}
