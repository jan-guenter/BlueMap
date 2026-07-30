#!/bin/sh
set -eu

script_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repository_root=$(CDPATH='' cd -- "$script_directory/../../.." && pwd)
chart_directory="$repository_root/charts/bluemap-web"
temporary=$(mktemp -d)
app_version=sha-0123456789abcdef0123456789abcdef01234567
chart_version=0.1.0-test.1

cleanup() {
    rm -rf -- "$temporary"
}
trap cleanup EXIT INT TERM

fail() {
    echo "benchmark Helm values test: $*" >&2
    exit 1
}

assert_contains() {
    grep -F -- "$2" "$1" >/dev/null ||
        fail "expected $1 to contain: $2"
}

assert_absent() {
    if grep -F -- "$2" "$1" >/dev/null; then
        fail "expected $1 not to contain: $2"
    fi
}

assert_count_at_least() {
    count=$(grep -F -c -- "$2" "$1" || true)
    if [ "$count" -lt "$3" ]; then
        fail "expected $1 to contain $2 at least $3 times, found $count"
    fi
}

helm package "$chart_directory" \
    --destination "$temporary" \
    --version "$chart_version" \
    --app-version "$app_version" >/dev/null
chart="$temporary/bluemap-web-$chart_version.tgz"

java_values="$script_directory/java-optimized-postgresql-values.yaml"
java_r3_values="$script_directory/java-optimized-postgresql-r3-values.yaml"
rust_values="$script_directory/rust-postgresql-values.yaml"
rust_r3_values="$script_directory/rust-postgresql-r3-values.yaml"
php_base_values="$script_directory/java-postgresql-values.yaml"
php_values="$script_directory/php-postgresql-baseline-values.yaml"

helm template bluemap-perf-java-new-postgresql "$chart" \
    --namespace minecraft \
    --values "$java_values" >"$temporary/java-r1.yaml"
helm template bluemap-perf-java-new-postgresql-r3 "$chart" \
    --namespace minecraft \
    --values "$java_values" \
    --values "$java_r3_values" >"$temporary/java-r3.yaml"
helm template bluemap-perf-rust-postgresql "$chart" \
    --namespace minecraft \
    --values "$rust_values" >"$temporary/rust-r1.yaml"
helm template bluemap-perf-rust-postgresql-r3 "$chart" \
    --namespace minecraft \
    --values "$rust_values" \
    --values "$rust_r3_values" >"$temporary/rust-r3.yaml"
helm template bluemap-perf-java "$chart" \
    --namespace minecraft \
    --values "$php_base_values" \
    --values "$php_values" >"$temporary/php.yaml"

assert_contains "$temporary/java-r1.yaml" \
    "name: bluemap-perf-java-new-postgresql"
assert_contains "$temporary/java-r1.yaml" \
    "image: \"ghcr.io/jan-guenter/bluemap-web:$app_version\""
assert_contains "$temporary/java-r1.yaml" "replicas: 1"
assert_contains "$temporary/java-r1.yaml" "max-connections: 12"
assert_contains "$temporary/java-r1.yaml" "sslmode=verify-full"
assert_contains "$temporary/java-r1.yaml" \
    "secretName: bluemap-perf-postgres-ca"
assert_contains "$temporary/java-r1.yaml" "name: bluemap-perf-postgres"
assert_count_at_least "$temporary/java-r1.yaml" \
    "app.kubernetes.io/part-of: bluemap-web-performance" 2
assert_count_at_least "$temporary/java-r1.yaml" \
    "bluemap.guenter.cloud/experiment-id: java-new-postgresql" 2
assert_count_at_least "$temporary/java-r1.yaml" "cpu: \"1\"" 2
assert_count_at_least "$temporary/java-r1.yaml" "memory: 1Gi" 2
assert_absent "$temporary/java-r1.yaml" "kind: Secret"

assert_contains "$temporary/java-r3.yaml" \
    "name: bluemap-perf-java-new-postgresql-r3"
assert_contains "$temporary/java-r3.yaml" "replicas: 3"
assert_contains "$temporary/java-r3.yaml" "max-connections: 4"
assert_count_at_least "$temporary/java-r3.yaml" \
    "bluemap.guenter.cloud/experiment-id: java-new-postgresql-r3" 2
assert_contains "$temporary/java-r3.yaml" \
    "image: \"ghcr.io/jan-guenter/bluemap-web:$app_version\""
assert_absent "$temporary/java-r3.yaml" "kind: Secret"

assert_contains "$temporary/rust-r1.yaml" \
    "name: bluemap-perf-rust-postgresql"
assert_contains "$temporary/rust-r1.yaml" \
    "image: \"ghcr.io/jan-guenter/bluemap-web-rust:$app_version\""
assert_contains "$temporary/rust-r1.yaml" "replicas: 1"
assert_contains "$temporary/rust-r1.yaml" "max_connections = 12"
assert_contains "$temporary/rust-r1.yaml" "max_in_flight_requests = 12"
assert_contains "$temporary/rust-r1.yaml" "mode = \"verify-full\""
assert_contains "$temporary/rust-r1.yaml" \
    "ca = \"/run/secrets/database-ca/ca.crt\""
assert_contains "$temporary/rust-r1.yaml" \
    "secretName: \"bluemap-perf-postgres-ca\""
assert_count_at_least "$temporary/rust-r1.yaml" \
    "app.kubernetes.io/part-of: bluemap-web-performance" 2
assert_count_at_least "$temporary/rust-r1.yaml" \
    "bluemap.guenter.cloud/experiment-id: rust-postgresql" 2
assert_count_at_least "$temporary/rust-r1.yaml" "cpu: \"1\"" 2
assert_count_at_least "$temporary/rust-r1.yaml" "memory: 1Gi" 2
assert_absent "$temporary/rust-r1.yaml" "download-jdbc-driver"
assert_absent "$temporary/rust-r1.yaml" "kind: Secret"

assert_contains "$temporary/rust-r3.yaml" \
    "name: bluemap-perf-rust-postgresql-r3"
assert_contains "$temporary/rust-r3.yaml" "replicas: 3"
assert_contains "$temporary/rust-r3.yaml" "max_connections = 4"
assert_contains "$temporary/rust-r3.yaml" "max_in_flight_requests = 4"
assert_count_at_least "$temporary/rust-r3.yaml" \
    "bluemap.guenter.cloud/experiment-id: rust-postgresql-r3" 2
assert_contains "$temporary/rust-r3.yaml" \
    "image: \"ghcr.io/jan-guenter/bluemap-web-rust:$app_version\""
assert_absent "$temporary/rust-r3.yaml" "kind: Secret"

assert_count_at_least "$temporary/php.yaml" \
    "bluemap.guenter.cloud/experiment-id: php-postgresql-baseline" 2

for values in "$java_values" "$rust_values"; do
    assert_contains "$values" 'tag: ""'
done

echo "Benchmark Helm values render with immutable appVersion image tags."
