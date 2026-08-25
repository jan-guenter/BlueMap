#!/bin/sh
set -eu

script_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
chart_directory=$(CDPATH='' cd -- "$script_directory/.." && pwd)
temporary=$(mktemp -d)

cleanup() {
    rm -rf -- "$temporary"
}
trap cleanup EXIT INT TERM

assert_contains() {
    grep -F -- "$2" "$1" >/dev/null || {
        echo "expected $1 to contain: $2" >&2
        exit 1
    }
}

assert_absent() {
    if grep -F -- "$2" "$1" >/dev/null; then
        echo "expected $1 not to contain: $2" >&2
        exit 1
    fi
}

expect_failure() {
    values=$1
    expected=$2
    if helm template test "$chart_directory" \
        --namespace bluemap \
        --values "$script_directory/$values" \
        >"$temporary/$values.out" 2>"$temporary/$values.err"; then
        echo "expected Helm rendering to reject $values" >&2
        exit 1
    fi
    assert_contains "$temporary/$values.err" "$expected"
}

helm lint "$chart_directory"

helm template test "$chart_directory" \
    --namespace bluemap \
    >"$temporary/default.yaml"
assert_contains "$temporary/default.yaml" "name: prepare-file-storage"
assert_contains "$temporary/default.yaml" "type: Recreate"
assert_contains "$temporary/default.yaml" "terminationGracePeriodSeconds: 30"
assert_contains "$temporary/default.yaml" "shutdown-grace-period-seconds: 20"
assert_contains "$temporary/default.yaml" "mountPath: /data/web"
assert_contains "$temporary/default.yaml" "name: webroot"
assert_contains "$temporary/default.yaml" "emptyDir: {}"
assert_absent "$temporary/default.yaml" "kind: PersistentVolumeClaim"
assert_absent "$temporary/default.yaml" "kind: HorizontalPodAutoscaler"
assert_absent "$temporary/default.yaml" "name: test-bluemap-web-metrics"
assert_absent "$temporary/default.yaml" "--metrics-port"

helm template test "$chart_directory" \
    --namespace bluemap \
    --values "$script_directory/metrics-values.yaml" \
    >"$temporary/metrics.yaml"
assert_contains "$temporary/metrics.yaml" "name: test-bluemap-web-metrics"
assert_contains "$temporary/metrics.yaml" "type: ClusterIP"
assert_contains "$temporary/metrics.yaml" "port: 9191"
assert_contains "$temporary/metrics.yaml" "example.com/scrape: enabled"
assert_contains "$temporary/metrics.yaml" "prometheus.io/path: /metrics"
assert_contains "$temporary/metrics.yaml" "--metrics-port"
assert_contains "$temporary/metrics.yaml" "--metrics-ip"
assert_contains "$temporary/metrics.yaml" '"0.0.0.0"'
assert_contains "$temporary/metrics.yaml" '"9191"'
assert_absent "$temporary/metrics.yaml" "kind: HorizontalPodAutoscaler"

helm template test "$chart_directory" \
    --namespace bluemap \
    --set fullnameOverride=abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijk \
    --set metrics.enabled=true \
    --show-only templates/metrics-service.yaml \
    >"$temporary/metrics-long-name.yaml"
assert_contains "$temporary/metrics-long-name.yaml" \
    "name: abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabc-metrics"

helm template test "$chart_directory" \
    --namespace bluemap \
    --values "$script_directory/autoscaling-values.yaml" \
    >"$temporary/autoscaling.yaml"
assert_contains "$temporary/autoscaling.yaml" "kind: HorizontalPodAutoscaler"
assert_contains "$temporary/autoscaling.yaml" "apiVersion: autoscaling/v2"
assert_contains "$temporary/autoscaling.yaml" "minReplicas: 2"
assert_contains "$temporary/autoscaling.yaml" "maxReplicas: 6"
assert_contains "$temporary/autoscaling.yaml" "type: Pods"
assert_contains "$temporary/autoscaling.yaml" "bluemap_web_http_connection_utilization_average_1m_ratio"
assert_contains "$temporary/autoscaling.yaml" 'averageValue: "650m"'
assert_contains "$temporary/autoscaling.yaml" "stabilizationWindowSeconds: 300"
assert_contains "$temporary/autoscaling.yaml" "name: test-bluemap-web-metrics"
assert_contains "$temporary/autoscaling.yaml" "type: RollingUpdate"
assert_absent "$temporary/autoscaling.yaml" "replicas:"
assert_absent "$temporary/autoscaling.yaml" "kind: PersistentVolumeClaim"

helm template test "$chart_directory" \
    --namespace bluemap \
    --values "$script_directory/autoscaling-values.yaml" \
    --show-only templates/hpa.yaml \
    >"$temporary/autoscaling-hpa.yaml"
assert_contains "$temporary/autoscaling-hpa.yaml" "apiVersion: apps/v1"
assert_contains "$temporary/autoscaling-hpa.yaml" "kind: Deployment"
assert_contains "$temporary/autoscaling-hpa.yaml" "name: test-bluemap-web"
assert_contains "$temporary/autoscaling-hpa.yaml" "type: AverageValue"

helm template test "$chart_directory" \
    --namespace bluemap \
    --values "$script_directory/autoscaling-values.yaml" \
    --show-only templates/deployment.yaml \
    >"$temporary/autoscaling-deployment.yaml"
assert_contains "$temporary/autoscaling-deployment.yaml" "containerPort: 9090"
assert_contains "$temporary/autoscaling-deployment.yaml" "--metrics-ip"
assert_contains "$temporary/autoscaling-deployment.yaml" '"0.0.0.0"'

helm template test "$chart_directory" \
    --namespace bluemap \
    --values "$script_directory/autoscaling-values.yaml" \
    --show-only templates/metrics-service.yaml \
    >"$temporary/autoscaling-metrics-service.yaml"
assert_contains "$temporary/autoscaling-metrics-service.yaml" "port: 9090"
assert_contains "$temporary/autoscaling-metrics-service.yaml" "targetPort: metrics"

helm template test "$chart_directory" \
    --namespace bluemap \
    --values "$script_directory/autoscaling-values.yaml" \
    --show-only templates/service.yaml \
    >"$temporary/autoscaling-public-service.yaml"
assert_contains "$temporary/autoscaling-public-service.yaml" "name: http"
assert_absent "$temporary/autoscaling-public-service.yaml" "name: metrics"
assert_absent "$temporary/autoscaling-public-service.yaml" "port: 9090"

helm template test "$chart_directory" \
    --namespace bluemap \
    --values "$script_directory/download-driver-values.yaml" \
    >"$temporary/download-driver.yaml"
assert_contains "$temporary/download-driver.yaml" "name: download-jdbc-driver"
assert_contains "$temporary/download-driver.yaml" "read-only: true"
assert_contains "$temporary/download-driver.yaml" "sha256sum -c -"
assert_absent "$temporary/download-driver.yaml" "kind: PersistentVolumeClaim"

helm template test "$chart_directory" \
    --namespace bluemap \
    --values "$script_directory/configmap-driver-values.yaml" \
    >"$temporary/configmap-driver.yaml"
assert_contains "$temporary/configmap-driver.yaml" "name: bluemap-jdbc-driver"
assert_contains "$temporary/configmap-driver.yaml" "readOnly: true"

helm template test "$chart_directory" \
    --namespace bluemap \
    --values "$script_directory/multi-replica-values.yaml" \
    >"$temporary/multi-replica.yaml"
assert_contains "$temporary/multi-replica.yaml" "replicas: 3"
assert_contains "$temporary/multi-replica.yaml" "type: RollingUpdate"
assert_contains "$temporary/multi-replica.yaml" "maxUnavailable: 0"
assert_contains "$temporary/multi-replica.yaml" "read-only: true"
assert_contains "$temporary/multi-replica.yaml" "compression: zstd"
assert_contains "$temporary/multi-replica.yaml" "max-connections: 12"
assert_contains "$temporary/multi-replica.yaml" "mountPath: /data/web"
assert_contains "$temporary/multi-replica.yaml" "name: webroot"
assert_absent "$temporary/multi-replica.yaml" "kind: PersistentVolumeClaim"
assert_absent "$temporary/multi-replica.yaml" "persistentVolumeClaim:"
assert_absent "$temporary/multi-replica.yaml" "sessionAffinity:"

expect_failure invalid-multi-replica-file-values.yaml "/storage/type"
expect_failure invalid-multi-replica-sqlite-values.yaml "/storage/sql/databaseType"
expect_failure invalid-multi-replica-persistence-values.yaml "/persistence/enabled"
expect_failure invalid-sql-persistence-values.yaml "/persistence/enabled"
expect_failure invalid-sql-no-driver-values.yaml \
    "SQL storage requires a JDBC driver"
expect_failure invalid-driver-download-checksum-values.yaml \
    "storage.sql.driver.download.url requires a sha256 checksum"
expect_failure invalid-multi-replica-volume-values.yaml \
    "extraVolumes must not use the reserved name webroot"
expect_failure invalid-multi-replica-mount-values.yaml \
    "extraVolumeMounts must not use the reserved /data/web mountPath"
expect_failure invalid-grace-period-values.yaml \
    "terminationGracePeriodSeconds must be greater than shutdownGracePeriodSeconds"
expect_failure invalid-autoscaling-file-values.yaml "/storage/type"
expect_failure invalid-autoscaling-sqlite-values.yaml "/storage/sql/databaseType"
expect_failure invalid-autoscaling-persistence-values.yaml "/persistence/enabled"
expect_failure invalid-autoscaling-range-values.yaml \
    "autoscaling.minReplicas must not exceed autoscaling.maxReplicas"
expect_failure invalid-autoscaling-target-values.yaml \
    "/autoscaling/targetAverageConnectionUtilizationPercentage"
expect_failure invalid-metrics-port-values.yaml \
    "metrics.port must differ from the public webserver port 8100"
expect_failure invalid-low-metrics-port-values.yaml "/metrics/port"
