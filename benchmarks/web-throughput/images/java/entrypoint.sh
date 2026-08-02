#!/bin/sh
set -eu

# shellcheck source=benchmarks/web-throughput/images/common/bootstrap.sh
. /usr/local/libexec/bluemap-bootstrap.sh

bootstrap_start_ssh
bootstrap_wait_for_path /bootstrap/config "uploaded BlueMap configuration directory"
bootstrap_wait_for_path /bootstrap/jdbc/mariadb-java-client.jar "uploaded MariaDB JDBC driver"
bootstrap_wait_for_path /bootstrap/tls/ca.crt "uploaded MariaDB CA certificate"
bootstrap_wait_for_start

[ -d /bootstrap/config ] || bootstrap_fail "BlueMap configuration path is not a directory"
[ ! -L /bootstrap/config ] || bootstrap_fail "BlueMap configuration path must not be a symlink"
[ -f /bootstrap/jdbc/mariadb-java-client.jar ] || bootstrap_fail "JDBC driver is not a regular file"
[ ! -L /bootstrap/jdbc/mariadb-java-client.jar ] || bootstrap_fail "JDBC driver must not be a symlink"
[ -f /bootstrap/tls/ca.crt ] || bootstrap_fail "MariaDB CA certificate is not a regular file"
[ ! -L /bootstrap/tls/ca.crt ] || bootstrap_fail "MariaDB CA certificate must not be a symlink"
bootstrap_validate_java_webserver_config /bootstrap/config/webserver.conf
chown -R 10001:10001 /bootstrap/config /bootstrap/jdbc
chown 10001:10001 /bootstrap/tls/ca.crt
chmod 0750 /bootstrap/config /bootstrap/jdbc
chmod 0440 /bootstrap/tls/ca.crt

exec setpriv \
    --reuid=10001 \
    --regid=10001 \
    --init-groups \
    --reset-env \
    --inh-caps=-all \
    /opt/java/openjdk/bin/java -jar /opt/bluemap/bluemap-web.jar --config /bootstrap/config
