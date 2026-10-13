{{- define "core.image" -}}
{{- if or (contains ":" .service.image) (contains "@" .service.image) -}}
{{ .service.image }}
{{- else -}}
{{ .service.image }}:{{ .service.tag | default (index .tags .name) | default "latest" }}
{{- end -}}
{{- end -}}

{{- define "core.megabytes" -}}
{{- $amount := regexFind "^[0-9]+" . | atoi -}}
{{- if hasSuffix "G" . }}{{ mul $amount 1024 }}{{ else }}{{ $amount }}{{ end -}}
{{- end -}}

{{- define "core.database" -}}
{{- .Values.postgres.database | default (replace "-" "_" .Values.slug) -}}
{{- end -}}

{{- define "core.postgres" -}}
{{- $postgres := .Values.postgres -}}
{{- $database := include "core.database" . -}}
{{- $user := $postgres.user | default $database -}}
image: {{ $postgres.image }}
{{- with $postgres.settings }}
command:
  - postgres
  {{- range $key, $value := . }}
  - -c
  - {{ printf "%s=%v" $key $value | toRawJson }}
  {{- end }}
{{- end }}
env:
  {{- toYaml (merge (dict "POSTGRES_DB" $database "POSTGRES_USER" $user "POSTGRES_PASSWORD" "${POSTGRES_PASSWORD}") $postgres.env) | nindent 2 }}
volumes: [postgres-data:/var/lib/postgresql]
health:
  test: [CMD-SHELL, {{ printf "pg_isready -U %s -d %s" $user $database | toRawJson }}]
  interval: 10s
  retries: 10
stopGracePeriod: 30s
memory: {{ $postgres.memory }}
cpus: {{ $postgres.cpus | quote }}
{{- with $postgres.reserve }}
reserve: {{ . }}
{{- end }}
{{- end -}}

{{- define "core.redis" -}}
{{- $redis := .Values.redis -}}
{{- $maxmemory := $redis.maxmemory | default (printf "%dmb" (div (mul (include "core.megabytes" $redis.memory | atoi) 3) 4)) -}}
{{- $flags := list "--appendonly" "yes" "--maxmemory" $maxmemory "--maxmemory-policy" (ternary "allkeys-lru" "noeviction" (ne (toString $redis.evict) "false")) -}}
image: {{ $redis.image }}
{{- if $redis.password }}
command: [sh, -c, {{ printf "exec redis-server %s --requirepass \"$$REDIS_PASSWORD\"" (join " " $flags) | toRawJson }}]
env:
  REDIS_PASSWORD: ${REDIS_PASSWORD}
health:
  test: [CMD-SHELL, "REDISCLI_AUTH=\"$$REDIS_PASSWORD\" redis-cli ping | grep -q PONG"]
  interval: 10s
  retries: 10
{{- else }}
command: {{ toRawJson (prepend $flags "redis-server") }}
health:
  test: [CMD, redis-cli, ping]
  interval: 10s
  retries: 10
{{- end }}
volumes: [redis-data:/data]
memory: {{ $redis.memory }}
cpus: {{ $redis.cpus | quote }}
{{- with $redis.reserve }}
reserve: {{ . }}
{{- end }}
{{- end -}}

{{- define "core.backup" -}}
{{- $backup := .Values.backup -}}
{{- $database := include "core.database" . -}}
image: {{ .Values.postgres.image }}
command: [sh, /scripts/backup.sh]
env:
  PGHOST: postgres
  PGUSER: {{ .Values.postgres.user | default $database }}
  PGDATABASE: {{ $database }}
  PGPASSWORD: ${POSTGRES_PASSWORD}
  BACKUP_KEEP_DAYS: {{ $backup.keepDays | quote }}
dependsOn: {postgres: healthy}
volumes:
  - backups:/backups
  {{- range $backup.include }}
  - {{ printf "%s:/data/%s:ro" . . }}
  {{- end }}
files:
  /scripts/backup.sh: {{ $.Files.Get "files/backup.sh" | toRawJson }}
memory: {{ $backup.memory }}
cpus: {{ $backup.cpus | quote }}
{{- with $backup.reserve }}
reserve: {{ . }}
{{- end }}
{{- end -}}

{{- define "core.services" -}}
{{- $values := .Values -}}
{{- $components := dict -}}
{{- if $values.postgres.enabled }}{{ $_ := set $components "postgres" (include "core.postgres" . | fromYaml) }}{{ end -}}
{{- if $values.redis.enabled }}{{ $_ := set $components "redis" (include "core.redis" . | fromYaml) }}{{ end -}}
{{- if $values.backup.enabled }}{{ $_ := set $components "backup" (include "core.backup" . | fromYaml) }}{{ end -}}
{{- $shared := dict -}}
{{- $waits := dict -}}
{{- if $values.postgres.enabled -}}
{{- $database := include "core.database" . -}}
{{- $user := $values.postgres.user | default $database -}}
{{- $_ := set $shared "DATABASE_URL" (printf "postgresql://%s:${POSTGRES_PASSWORD}@postgres:5432/%s" $user $database) -}}
{{- $_ := set $shared "POSTGRES_HOST" "postgres" -}}
{{- $_ := set $shared "POSTGRES_PORT" "5432" -}}
{{- $_ := set $shared "POSTGRES_DB" $database -}}
{{- $_ := set $shared "POSTGRES_USER" $user -}}
{{- $_ := set $shared "POSTGRES_PASSWORD" "${POSTGRES_PASSWORD}" -}}
{{- $_ := set $waits "postgres" "healthy" -}}
{{- end -}}
{{- if $values.redis.enabled -}}
{{- $_ := set $shared "REDIS_URL" (ternary "redis://:${REDIS_PASSWORD}@redis:6379" "redis://redis:6379" (eq (toString $values.redis.password) "true")) -}}
{{- $_ := set $waits "redis" "healthy" -}}
{{- end -}}
{{- $all := dict -}}
{{- range $name, $service := $values.services -}}
{{- $env := deepCopy $shared -}}
{{- range $service.optional }}{{ $_ := set $env . (printf "${%s:-}" .) }}{{ end -}}
{{- range $service.secrets }}{{ $_ := set $env . (printf "${%s}" .) }}{{ end -}}
{{- range $key, $value := $service.env }}{{ $_ := set $env $key (toString $value) }}{{ end -}}
{{- $_ := set $all $name (merge (dict "env" $env "dependsOn" (merge (deepCopy ($service.dependsOn | default dict)) $waits)) (omit $service "env" "dependsOn" "secrets" "optional")) -}}
{{- end -}}
{{- range $name, $component := $components }}{{ $_ := set $all $name $component }}{{ end -}}
{{- toJson $all -}}
{{- end -}}

{{- define "core.public" -}}
{{- $services := include "core.services" . | fromJson -}}
{{- $marked := list -}}
{{- $listening := list -}}
{{- range $name, $service := $services -}}
{{- if $service.public }}{{ $marked = append $marked $name }}{{ end -}}
{{- if $service.port }}{{ $listening = append $listening $name }}{{ end -}}
{{- end -}}
{{- if gt (len $marked) 1 }}{{ fail (printf "only one service can set public: true, not %s" (join ", " $marked)) }}{{ end -}}
{{- if $marked }}{{ first $marked }}{{ else if eq (len $listening) 1 }}{{ first $listening }}{{ end -}}
{{- end -}}
