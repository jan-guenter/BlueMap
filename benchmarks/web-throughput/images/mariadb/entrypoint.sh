#!/bin/sh
set -eu

# shellcheck source=benchmarks/web-throughput/images/common/bootstrap.sh
. /usr/local/libexec/bluemap-bootstrap.sh

readonly ca_file=/bootstrap/tls/ca.crt
readonly certificate_file=/bootstrap/tls/server.crt
readonly key_file=/bootstrap/tls/server.key
readonly snapshot_file=/bootstrap/database/snapshot.sql.zst
readonly users_file=/bootstrap/database/020-benchmark-users.sql
readonly root_password_file=/bootstrap/database/root-password

bootstrap_start_ssh
bootstrap_wait_for_path "$ca_file" "uploaded MariaDB CA certificate"
bootstrap_wait_for_path "$certificate_file" "uploaded MariaDB server certificate"
bootstrap_wait_for_path "$key_file" "uploaded MariaDB server key"
bootstrap_wait_for_path "$snapshot_file" "uploaded MariaDB snapshot"
bootstrap_wait_for_path "$users_file" "uploaded benchmark user initialization"
bootstrap_wait_for_path "$root_password_file" "uploaded MariaDB root-password file"
bootstrap_wait_for_start

for required_file in \
    "$ca_file" \
    "$certificate_file" \
    "$key_file" \
    "$snapshot_file" \
    "$users_file" \
    "$root_password_file"
do
    if [ ! -f "$required_file" ] || [ -L "$required_file" ]; then
        bootstrap_fail "required MariaDB input must be a regular non-symlink file: $required_file"
    fi
done

[ -s "$root_password_file" ] || bootstrap_fail "MariaDB root password file is empty"
chown mysql:mysql "$ca_file" "$certificate_file" "$key_file" "$root_password_file"
chmod 0400 "$key_file" "$root_password_file" "$users_file"
chmod 0444 "$ca_file" "$certificate_file" "$snapshot_file"

openssl verify -CAfile "$ca_file" "$certificate_file" >/dev/null || \
    bootstrap_fail "MariaDB server certificate does not verify against uploaded CA"
certificate_public_key="$(openssl x509 -in "$certificate_file" -pubkey -noout | openssl sha256)"
private_public_key="$(openssl pkey -in "$key_file" -pubout | openssl sha256)"
[ "$certificate_public_key" = "$private_public_key" ] || \
    bootstrap_fail "MariaDB server certificate and private key do not match"
zstd --test "$snapshot_file" >/dev/null || bootstrap_fail "MariaDB snapshot zstd stream is invalid"

if find /var/lib/mysql -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    bootstrap_fail "MariaDB data directory is not empty; benchmark initialization refuses reuse"
fi

cp "$snapshot_file" /docker-entrypoint-initdb.d/010-snapshot.sql.zst
cp "$users_file" /docker-entrypoint-initdb.d/020-benchmark-users.sql
chown mysql:mysql \
    /docker-entrypoint-initdb.d/010-snapshot.sql.zst \
    /docker-entrypoint-initdb.d/020-benchmark-users.sql
chmod 0400 /docker-entrypoint-initdb.d/010-snapshot.sql.zst \
    /docker-entrypoint-initdb.d/020-benchmark-users.sql

export MARIADB_ROOT_PASSWORD_FILE="$root_password_file"
# The sanitized fixture deliberately contains only table/data statements. Pin
# the database here so the official entrypoint creates and selects it before
# processing 010-snapshot.sql.zst; never inherit a caller-supplied database.
export MARIADB_DATABASE=bluemap

# No process has bound 3306 before this exec. The exact official entrypoint
# initializes the empty data directory and imports the uploaded snapshot.
exec docker-entrypoint.sh mariadbd \
    --bind-address=0.0.0.0 \
    --port=3306 \
    --require-secure-transport=ON \
    --ssl-ca="$ca_file" \
    --ssl-cert="$certificate_file" \
    --ssl-key="$key_file" \
    --connect-timeout=5 \
    --max-connections=64 \
    --skip-name-resolve
