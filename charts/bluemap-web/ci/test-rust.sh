#!/bin/sh
set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
chart_directory=$(CDPATH= cd -- "$script_directory/.." && pwd)
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

extract_toml() {
    awk '
        found && /^    / {
            line = $0
            sub(/^    /, "", line)
            print line
            next
        }
        found && /^$/ {
            print
            next
        }
        found {
            exit
        }
        $0 == "  config.toml: |" {
            found = 1
        }
    ' "$1" >"$2"
}

helm lint "$chart_directory"

helm template test "$chart_directory" --namespace bluemap \
    >"$temporary/java.yaml"
assert_contains "$temporary/java.yaml" \
    'image: "ghcr.io/bluemap-minecraft/bluemap-web:dev"'
assert_absent "$temporary/java.yaml" "config.toml: |"
assert_absent "$temporary/java.yaml" "bluemap-web-rust:"

helm template test "$chart_directory" --namespace bluemap \
    --values "$chart_directory/examples/rust-file-values.yaml" \
    >"$temporary/rust-file.yaml"
assert_contains "$temporary/rust-file.yaml" \
    'image: "ghcr.io/bluemap-minecraft/bluemap-web-rust:dev"'
assert_contains "$temporary/rust-file.yaml" "replicas: 2"
assert_contains "$temporary/rust-file.yaml" "claimName: bluemap-maps-rwx"
assert_contains "$temporary/rust-file.yaml" "tcpSocket:"
assert_contains "$temporary/rust-file.yaml" "path: /health/ready"
assert_contains "$temporary/rust-file.yaml" "cpu: 50m"
assert_contains "$temporary/rust-file.yaml" "memory: 256Mi"
assert_absent "$temporary/rust-file.yaml" "download-jdbc-driver"
assert_absent "$temporary/rust-file.yaml" "jdbc-driver"
assert_absent "$temporary/rust-file.yaml" "BLUEMAP_SQL_"
assert_absent "$temporary/rust-file.yaml" "storage.conf: |"
assert_absent "$temporary/rust-file.yaml" "core.conf: |"
assert_absent "$temporary/rust-file.yaml" "webserver.conf: |"
assert_absent "$temporary/rust-file.yaml" \
    'image: "ghcr.io/bluemap-minecraft/bluemap-web:dev"'
extract_toml "$temporary/rust-file.yaml" "$temporary/rust-file.toml"

python3 - "$temporary/rust-file.toml" <<'PY'
import pathlib
import sys
import tomllib

with pathlib.Path(sys.argv[1]).open("rb") as source:
    config = tomllib.load(source)
assert config["tile_cache_max_age_seconds"] == 60
assert isinstance(config["webapp"]["resolution_default"], float)
assert [entry["id"] for entry in config["maps"]] == ["world", "nether"]
assert config["storage"] == {
    "type": "file",
    "root": "/data/maps",
    "compression": "gzip",
}
PY

helm template test "$chart_directory" --namespace bluemap \
    --values "$chart_directory/examples/rust-file-values.yaml" \
    --set storage.compression=lz4 \
    >"$temporary/rust-lz4.yaml"
extract_toml "$temporary/rust-lz4.yaml" "$temporary/rust-lz4.toml"
python3 - "$temporary/rust-lz4.toml" <<'PY'
import pathlib
import sys
import tomllib

with pathlib.Path(sys.argv[1]).open("rb") as source:
    assert tomllib.load(source)["storage"]["compression"] == "lz4"
PY

for database in mariadb postgresql; do
    helm template test "$chart_directory" --namespace bluemap \
        --values "$chart_directory/examples/rust-${database}-values.yaml" \
        >"$temporary/rust-${database}.yaml"
    assert_contains "$temporary/rust-${database}.yaml" \
        "name: BLUEMAP_DATABASE_USERNAME"
    assert_contains "$temporary/rust-${database}.yaml" \
        "name: BLUEMAP_DATABASE_PASSWORD"
    assert_contains "$temporary/rust-${database}.yaml" \
        "mountPath: /run/secrets/database-ca"
    assert_absent "$temporary/rust-${database}.yaml" "download-jdbc-driver"
    assert_absent "$temporary/rust-${database}.yaml" "kind: Secret"
    extract_toml "$temporary/rust-${database}.yaml" \
        "$temporary/rust-${database}.toml"
done

python3 - "$temporary/rust-mariadb.toml" "$temporary/rust-postgresql.toml" <<'PY'
import pathlib
import sys
import tomllib

for path, expected_type, expected_port in (
    (sys.argv[1], "mariadb", 3306),
    (sys.argv[2], "postgresql", 5432),
):
    with pathlib.Path(path).open("rb") as source:
        storage = tomllib.load(source)["storage"]
    assert storage["type"] == expected_type
    assert storage["port"] == expected_port
    assert storage["username_env"] == "BLUEMAP_DATABASE_USERNAME"
    assert storage["password_env"] == "BLUEMAP_DATABASE_PASSWORD"
    assert storage["tls"]["ca"] == "/run/secrets/database-ca/ca.crt"
PY

for values in "$script_directory"/invalid-rust-*-values.yaml; do
    if helm template test "$chart_directory" --namespace bluemap \
        --values "$values" >"$temporary/invalid.yaml" 2>&1; then
        echo "expected Helm to reject $values" >&2
        exit 1
    fi
done
