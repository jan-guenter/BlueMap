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
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
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

{{- define "bluemap-web.phpFullname" -}}
{{- printf "%s-php" (include "bluemap-web.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "bluemap-web.webSelectorLabels" -}}
{{ include "bluemap-web.selectorLabels" . }}
app.kubernetes.io/component: web
{{- end }}

{{- define "bluemap-web.phpSelectorLabels" -}}
{{ include "bluemap-web.selectorLabels" . }}
app.kubernetes.io/component: sql-data
{{- end }}

{{- define "bluemap-web.phpLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{ include "bluemap-web.phpSelectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "bluemap-web.sqlDriverEnabled" -}}
{{- if or .Values.storage.sql.driver.existingConfigMap.name .Values.storage.sql.driver.download.url -}}true{{- end -}}
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

{{- define "bluemap-web.sqlPdoDriver" -}}
{{- if eq .Values.storage.sql.databaseType "postgresql" -}}pgsql{{- else -}}mysql{{- end -}}
{{- end }}

{{- define "bluemap-web.validate" -}}
{{- $configMapName := .Values.storage.sql.driver.existingConfigMap.name -}}
{{- $configMapKey := .Values.storage.sql.driver.existingConfigMap.key -}}
{{- $downloadUrl := .Values.storage.sql.driver.download.url -}}
{{- $checksum := .Values.storage.sql.driver.download.sha256 -}}
{{- $storagePath := printf "storages/%s.conf" .Values.storage.id -}}
{{- $driverEnabled := include "bluemap-web.sqlDriverEnabled" . -}}
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
{{- if and $checksum (not (regexMatch "^[A-Fa-f0-9]{64}$" $checksum)) -}}
{{- fail "storage.sql.driver.download.sha256 must contain exactly 64 hexadecimal characters" -}}
{{- end -}}
{{- if and $driverEnabled (ne .Values.storage.type "sql") -}}
{{- fail "storage.sql.driver can only be configured when storage.type is sql" -}}
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
{{- if and .Values.phpFpm.enabled (ne .Values.storage.type "sql") -}}
{{- fail "phpFpm.enabled requires storage.type=sql" -}}
{{- end -}}
{{- if and .Values.phpFpm.enabled (eq .Values.storage.sql.databaseType "sqlite") -}}
{{- fail "phpFpm does not support SQLite; BlueMap's sql.php supports MySQL/MariaDB and PostgreSQL" -}}
{{- end -}}
{{- if and (gt (int .Values.replicaCount) 1) (eq .Values.storage.type "sql") (eq .Values.storage.sql.databaseType "sqlite") -}}
{{- fail "Java replicaCount greater than 1 is not supported with SQLite; use one replica or an external SQL database" -}}
{{- end -}}
{{- if gt (int .Values.replicaCount) 1 -}}
{{- range .Values.extraVolumes -}}
{{- if eq (default "" .name) "webroot" -}}
{{- fail "extraVolumes must not use the reserved name webroot when replicaCount is greater than 1" -}}
{{- end -}}
{{- end -}}
{{- range .Values.extraVolumeMounts -}}
{{- if eq (default "" .mountPath) "/data/web" -}}
{{- fail "extraVolumeMounts must not use the reserved /data/web mountPath when replicaCount is greater than 1" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}
