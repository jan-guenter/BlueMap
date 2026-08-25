{{- define "bluemap-web.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "bluemap-web.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "bluemap-web.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" | quote }}
{{ include "bluemap-web.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "bluemap-web.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bluemap-web.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "bluemap-web.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "bluemap-web.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "bluemap-web.configMapName" -}}
{{- default (printf "%s-config" (include "bluemap-web.fullname" .)) .Values.config.existingConfigMap }}
{{- end }}

{{- define "bluemap-web.secretName" -}}
{{- default (printf "%s-secret" (include "bluemap-web.fullname" .)) .Values.secretConfig.existingSecret }}
{{- end }}

{{- define "bluemap-web.storageConfigName" -}}
{{- printf "%s-storage" (include "bluemap-web.fullname" .) }}
{{- end }}

{{- define "bluemap-web.sqlCredentialsSecretName" -}}
{{- default (printf "%s-sql" (include "bluemap-web.fullname" .)) .Values.storage.sql.credentials.existingSecret }}
{{- end }}

{{- define "bluemap-web.webSelectorLabels" -}}
{{ include "bluemap-web.selectorLabels" . }}
app.kubernetes.io/component: web
{{- end }}

{{- define "bluemap-web.sqlDriverEnabled" -}}
{{- if or .Values.storage.sql.driver.existingConfigMap.name .Values.storage.sql.driver.download.url -}}true{{- end -}}
{{- end }}

{{- define "bluemap-web.metricsEnabled" -}}
{{- if or .Values.metrics.enabled .Values.autoscaling.enabled -}}true{{- end -}}
{{- end }}

{{- define "bluemap-web.metricsServiceName" -}}
{{- printf "%s-metrics" (include "bluemap-web.fullname" . | trunc 55 | trimSuffix "-") -}}
{{- end }}

{{- define "bluemap-web.sqlConnectionUrl" -}}
{{- if .Values.storage.sql.connectionUrl -}}
{{- .Values.storage.sql.connectionUrl -}}
{{- else if eq .Values.storage.sql.databaseType "sqlite" -}}
{{- printf "jdbc:sqlite:%s" .Values.storage.sql.database -}}
{{- else -}}
{{- printf "jdbc:%s://%s:%v/%s" .Values.storage.sql.databaseType .Values.storage.sql.host .Values.storage.sql.port .Values.storage.sql.database -}}
{{- end -}}
{{- end }}

{{- define "bluemap-web.validate" -}}
{{- $configMapName := .Values.storage.sql.driver.existingConfigMap.name -}}
{{- $configMapKey := .Values.storage.sql.driver.existingConfigMap.key -}}
{{- $downloadUrl := .Values.storage.sql.driver.download.url -}}
{{- $checksum := .Values.storage.sql.driver.download.sha256 -}}
{{- $storagePath := printf "storages/%s.conf" .Values.storage.id -}}
{{- $driverEnabled := include "bluemap-web.sqlDriverEnabled" . -}}
{{- $horizontallyScaled := or .Values.autoscaling.enabled (gt (int .Values.replicaCount) 1) -}}
{{- if lt (int .Values.shutdownGracePeriodSeconds) 0 -}}
{{- fail "shutdownGracePeriodSeconds must not be negative" -}}
{{- end -}}
{{- if le (int .Values.terminationGracePeriodSeconds) (int .Values.shutdownGracePeriodSeconds) -}}
{{- fail "terminationGracePeriodSeconds must be greater than shutdownGracePeriodSeconds" -}}
{{- end -}}
{{- if and $configMapName $downloadUrl -}}
{{- fail "storage.sql.driver.existingConfigMap and storage.sql.driver.download.url are mutually exclusive" -}}
{{- end -}}
{{- if ne (not (empty $configMapName)) (not (empty $configMapKey)) -}}
{{- fail "storage.sql.driver.existingConfigMap.name and key must be set together" -}}
{{- end -}}
{{- if and $checksum (not $downloadUrl) -}}
{{- fail "storage.sql.driver.download.sha256 requires a download URL" -}}
{{- end -}}
{{- if and $downloadUrl (empty $checksum) -}}
{{- fail "storage.sql.driver.download.url requires a sha256 checksum" -}}
{{- end -}}
{{- if and $checksum (not (regexMatch "^[A-Fa-f0-9]{64}$" $checksum)) -}}
{{- fail "storage.sql.driver.download.sha256 must contain exactly 64 hexadecimal characters" -}}
{{- end -}}
{{- if and $driverEnabled (ne .Values.storage.type "sql") -}}
{{- fail "storage.sql.driver can only be configured when storage.type is sql" -}}
{{- end -}}
{{- if and (eq .Values.storage.type "sql") (not $driverEnabled) -}}
{{- fail "SQL storage requires a JDBC driver from storage.sql.driver.existingConfigMap or storage.sql.driver.download" -}}
{{- end -}}
{{- if and $driverEnabled (empty .Values.storage.sql.driver.className) -}}
{{- fail "storage.sql.driver.className is required when a JDBC driver is configured" -}}
{{- end -}}
{{- if or (hasKey .Values.config.files $storagePath) (hasKey .Values.secretConfig.files $storagePath) -}}
{{- fail (printf "%s is generated from storage values and must not also appear in config.files or secretConfig.files" $storagePath) -}}
{{- end -}}
{{- if and (eq .Values.storage.type "sql") (ne .Values.storage.sql.databaseType "sqlite") (empty .Values.storage.sql.host) -}}
{{- fail "storage.sql.host is required for network SQL databases" -}}
{{- end -}}
{{- if and (eq .Values.storage.type "sql") (empty .Values.storage.sql.database) -}}
{{- fail "storage.sql.database is required when storage.type is sql" -}}
{{- end -}}
{{- if and (eq .Values.storage.type "sql") (ne .Values.storage.sql.databaseType "sqlite") (empty .Values.storage.sql.credentials.existingSecret) (empty .Values.storage.sql.credentials.username) -}}
{{- fail "storage.sql.credentials.username or existingSecret is required for network SQL databases" -}}
{{- end -}}
{{- if and .Values.storage.sql.credentials.existingSecret (or .Values.storage.sql.credentials.username .Values.storage.sql.credentials.password) -}}
{{- fail "storage.sql.credentials existingSecret and inline username/password are mutually exclusive" -}}
{{- end -}}
{{- if or (hasKey .Values.storage.sql.properties "user") (hasKey .Values.storage.sql.properties "password") -}}
{{- fail "storage.sql.properties must not contain user or password; use storage.sql.credentials" -}}
{{- end -}}
{{- if and .Values.persistence.enabled (ne .Values.storage.type "file") -}}
{{- fail "persistence is supported only for single-replica file storage; SQL runtime data is pod-local" -}}
{{- end -}}
{{- if and (include "bluemap-web.metricsEnabled" .) (eq (int .Values.metrics.port) 8100) -}}
{{- fail "metrics.port must differ from the public webserver port 8100" -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled (lt (int .Values.autoscaling.minReplicas) 1) -}}
{{- fail "autoscaling.minReplicas must be at least 1" -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled (lt (int .Values.autoscaling.maxReplicas) 2) -}}
{{- fail "autoscaling.maxReplicas must be at least 2" -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled (gt (int .Values.autoscaling.minReplicas) (int .Values.autoscaling.maxReplicas)) -}}
{{- fail "autoscaling.minReplicas must not exceed autoscaling.maxReplicas" -}}
{{- end -}}
{{- if and $horizontallyScaled (eq .Values.storage.type "sql") (eq .Values.storage.sql.databaseType "sqlite") -}}
{{- fail "horizontal scaling requires an external SQL database; SQLite is pod-local" -}}
{{- end -}}
{{- if and $horizontallyScaled (ne .Values.storage.type "sql") -}}
{{- fail "horizontal scaling requires external SQL storage so every replica is stateless and reads the same data" -}}
{{- end -}}
{{- if and $horizontallyScaled .Values.persistence.enabled -}}
{{- fail "horizontal scaling cannot use persistence; generated web files and runtime data must remain pod-local" -}}
{{- end -}}
{{- range .Values.extraVolumes -}}
{{- if eq (default "" .name) "webroot" -}}
{{- fail "extraVolumes must not use the reserved name webroot" -}}
{{- end -}}
{{- end -}}
{{- range .Values.extraVolumeMounts -}}
{{- if eq (default "" .mountPath) "/data/web" -}}
{{- fail "extraVolumeMounts must not use the reserved /data/web mountPath" -}}
{{- end -}}
{{- end -}}
{{- end }}
