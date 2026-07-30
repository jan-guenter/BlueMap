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

{{- define "bluemap-web.rustConfigName" -}}
{{- printf "%s-rust" (include "bluemap-web.fullname" .) }}
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
{{- $isRust := eq .Values.webserver.implementation "rust" -}}
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
{{- if and (not $isRust) (gt (int .Values.replicaCount) 1) (eq .Values.storage.type "sql") (eq .Values.storage.sql.databaseType "sqlite") -}}
{{- fail "Java replicaCount greater than 1 is not supported with SQLite; use one replica or an external SQL database" -}}
{{- end -}}
{{- if and (not $isRust) (gt (int .Values.replicaCount) 1) -}}
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
{{- if $isRust -}}
{{- if and (hasKey .Values.nodeSelector "kubernetes.io/arch") (ne (index .Values.nodeSelector "kubernetes.io/arch") "amd64") -}}
{{- fail "the Rust image supports only linux/amd64; nodeSelector kubernetes.io/arch must be amd64" -}}
{{- end -}}
{{- if and (hasKey .Values.nodeSelector "kubernetes.io/os") (ne (index .Values.nodeSelector "kubernetes.io/os") "linux") -}}
{{- fail "the Rust image supports only Linux; nodeSelector kubernetes.io/os must be linux" -}}
{{- end -}}
{{- if or (ne .Values.webserver.rust.config.webapp.mapDataRoot "maps") (ne .Values.webserver.rust.config.webapp.liveDataRoot "maps") -}}
{{- fail "the Rust webserver currently serves map and live data only below /maps; mapDataRoot and liveDataRoot must both be maps" -}}
{{- end -}}
{{- if .Values.phpFpm.enabled -}}
{{- fail "phpFpm.enabled is Java-only and cannot be used with webserver.implementation=rust" -}}
{{- end -}}
{{- if and (ne .Values.storage.type "file") (ne .Values.storage.type "sql") -}}
{{- fail "the Rust webserver supports only file or sql storage; custom storage is not supported" -}}
{{- end -}}
{{- if and (eq .Values.storage.type "sql") (ne .Values.storage.sql.databaseType "mariadb") (ne .Values.storage.sql.databaseType "postgresql") -}}
{{- fail "the Rust webserver supports only MariaDB or PostgreSQL SQL storage; MySQL and SQLite are not supported" -}}
{{- end -}}
{{- if and (eq .Values.storage.type "sql") (empty .Values.storage.sql.credentials.existingSecret) -}}
{{- fail "the Rust webserver requires storage.sql.credentials.existingSecret for SQL credentials" -}}
{{- end -}}
{{- if or .Values.storage.sql.credentials.username .Values.storage.sql.credentials.password -}}
{{- fail "inline storage.sql.credentials username/password are Java-only and cannot be used with the Rust webserver" -}}
{{- end -}}
{{- if .Values.storage.sql.connectionUrl -}}
{{- fail "storage.sql.connectionUrl is JDBC-only and cannot be used with the Rust webserver" -}}
{{- end -}}
{{- if .Values.storage.sql.properties -}}
{{- fail "storage.sql.properties are JDBC-only and cannot be used with the Rust webserver" -}}
{{- end -}}
{{- if or .Values.storage.sql.driver.className .Values.storage.sql.driver.existingConfigMap.name .Values.storage.sql.driver.existingConfigMap.key .Values.storage.sql.driver.download.url .Values.storage.sql.driver.download.sha256 -}}
{{- fail "storage.sql.driver settings are JDBC-only and cannot be used with the Rust webserver" -}}
{{- end -}}
{{- if and (eq .Values.storage.type "sql") .Values.persistence.enabled -}}
{{- fail "persistence is unused for Rust SQL storage; mount database TLS Secrets or use extraVolumes instead" -}}
{{- end -}}
{{- if and (eq .Values.storage.type "file") (or .Values.webserver.rust.databaseTls.ca.existingSecret .Values.webserver.rust.databaseTls.clientCertificate.existingSecret) -}}
{{- fail "webserver.rust.databaseTls is only valid with SQL storage" -}}
{{- end -}}
{{- if and (eq .Values.webserver.rust.databaseTls.mode "disable") (or .Values.webserver.rust.databaseTls.ca.existingSecret .Values.webserver.rust.databaseTls.clientCertificate.existingSecret) -}}
{{- fail "webserver.rust.databaseTls.mode=disable cannot be combined with CA or client-certificate Secrets" -}}
{{- end -}}
{{- if or .Values.config.existingConfigMap .Values.config.items .Values.secretConfig.existingSecret .Values.secretConfig.items .Values.secretConfig.files -}}
{{- fail "external Java config/secretConfig sources cannot be used with the Rust webserver; use webserver.rust values" -}}
{{- end -}}
{{- $seenMaps := dict -}}
{{- range .Values.webserver.rust.maps -}}
{{- if hasKey $seenMaps .id -}}
{{- fail (printf "duplicate webserver.rust.maps id %q" .id) -}}
{{- end -}}
{{- $_ := set $seenMaps .id true -}}
{{- end -}}
{{- range .Values.extraEnv -}}
{{- $name := default "" .name -}}
{{- if has $name (list "BLUEMAP_DATABASE_USERNAME" "BLUEMAP_DATABASE_PASSWORD" "BLUEMAP_SQL_USERNAME" "BLUEMAP_SQL_PASSWORD" "PGSSLROOTCERT" "PGSSLCERT" "PGSSLKEY" "PGOPTIONS" "PGAPPNAME") -}}
{{- fail (printf "extraEnv variable %s is reserved or forbidden for the Rust webserver" $name) -}}
{{- end -}}
{{- end -}}
{{- range .Values.extraVolumes -}}
{{- if has (default "" .name) (list "rust-config" "data" "database-ca" "database-client") -}}
{{- fail (printf "extraVolumes must not use Rust-reserved volume name %s" .name) -}}
{{- end -}}
{{- end -}}
{{- range .Values.extraVolumeMounts -}}
{{- $mountPath := clean (default "" .mountPath) -}}
{{- if or (eq $mountPath "/data") (hasPrefix "/data/" $mountPath) (eq $mountPath "/etc/bluemap-web") (hasPrefix "/etc/bluemap-web/" $mountPath) (eq $mountPath "/run/secrets/database-ca") (hasPrefix "/run/secrets/database-ca/" $mountPath) (eq $mountPath "/run/secrets/database-client") (hasPrefix "/run/secrets/database-client/" $mountPath) -}}
{{- fail (printf "extraVolumeMounts must not use or shadow Rust-reserved mountPath %s" $mountPath) -}}
{{- end -}}
{{- end -}}
{{- if eq .Values.storage.type "file" -}}
{{- $fileRoot := clean .Values.storage.file.root -}}
{{- if not (hasPrefix "/" $fileRoot) -}}
{{- fail "Rust file storage root must be an absolute path" -}}
{{- end -}}
{{- $extraVolumeNames := dict -}}
{{- range .Values.extraVolumes -}}
{{- $_ := set $extraVolumeNames (default "" .name) true -}}
{{- end -}}
{{- $externalSource := false -}}
{{- range .Values.extraVolumeMounts -}}
{{- $mountPath := clean (default "" .mountPath) -}}
{{- $coversRoot := or (eq $mountPath $fileRoot) (and (ne $mountPath "/") (hasPrefix (printf "%s/" $mountPath) $fileRoot)) -}}
{{- if $coversRoot -}}
{{- if not (hasKey $extraVolumeNames (default "" .name)) -}}
{{- fail (printf "extraVolumeMount %s covers Rust file storage but has no matching extraVolume" (default "" .name)) -}}
{{- end -}}
{{- if not (default false .readOnly) -}}
{{- fail (printf "extraVolumeMount %s covering Rust file storage must be readOnly" (default "" .name)) -}}
{{- end -}}
{{- $externalSource = true -}}
{{- end -}}
{{- end -}}
{{- $persistenceSource := and .Values.persistence.enabled (or (eq $fileRoot "/data") (hasPrefix "/data/" $fileRoot)) -}}
{{- if not (or $persistenceSource $externalSource) -}}
{{- fail "Rust file storage requires persistence under /data or a read-only extraVolumeMount covering storage.file.root" -}}
{{- end -}}
{{- if and (gt (int .Values.replicaCount) 1) (not (or (has "ReadWriteMany" .Values.persistence.accessModes) (has "ReadOnlyMany" .Values.persistence.accessModes))) -}}
{{- fail "Rust file storage with multiple replicas requires ReadWriteMany or ReadOnlyMany in persistence.accessModes" -}}
{{- end -}}
{{- if and (gt (int .Values.replicaCount) 1) (or (has "ReadWriteOnce" .Values.persistence.accessModes) (has "ReadWriteOncePod" .Values.persistence.accessModes)) -}}
{{- fail "Rust file storage with multiple replicas cannot declare ReadWriteOnce or ReadWriteOncePod in persistence.accessModes" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}
