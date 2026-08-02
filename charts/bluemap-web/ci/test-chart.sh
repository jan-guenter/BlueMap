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
