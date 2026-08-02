#!/bin/sh
set -eu

# shellcheck source=benchmarks/web-throughput/images/common/bootstrap.sh
. /usr/local/libexec/bluemap-bootstrap.sh

readonly upstream_sql=/opt/bluemap/sql.php.upstream
readonly generated_sql=/srv/bluemap/sql.php
readonly database_json=/bootstrap/php/database.json
readonly ca_file=/bootstrap/tls/ca.crt
readonly stunnel_config=/run/stunnel/mariadb.conf
readonly expected_sql_sha256=e160a9ecbd996b5c701f172a7b22bf73eec96670cf6508034e18251067bebb6b

bootstrap_start_ssh
bootstrap_wait_for_path "$database_json" "uploaded PHP database configuration"
bootstrap_wait_for_path "$ca_file" "uploaded MariaDB CA certificate"
bootstrap_wait_for_start

if [ ! -f "$database_json" ] || [ -L "$database_json" ]; then
    bootstrap_fail "PHP database configuration must be a regular non-symlink file"
fi
if [ ! -f "$ca_file" ] || [ -L "$ca_file" ]; then
    bootstrap_fail "MariaDB CA certificate must be a regular non-symlink file"
fi
chmod 0600 "$database_json"
chmod 0444 "$ca_file"

actual_sql_sha256="$(sha256sum "$upstream_sql" | awk '{print $1}')"
[ "$actual_sql_sha256" = "$expected_sql_sha256" ] || \
    bootstrap_fail "upstream sql.php digest mismatch"

# The single-quoted program is intentionally PHP, not shell expansion.
# shellcheck disable=SC2016
php -d display_errors=stderr -r '
    function fail(string $message): never {
        fwrite(STDERR, "PHP benchmark bootstrap: " . $message . "\n");
        exit(1);
    }

    $source = file_get_contents($argv[1]);
    $settingsJson = file_get_contents($argv[3]);
    if ($source === false || $settingsJson === false) fail("failed to read inputs");
    $settings = json_decode($settingsJson, true, flags: JSON_THROW_ON_ERROR);

    foreach (["host", "port", "tlsServerName", "username", "password", "database"] as $key) {
        if (!array_key_exists($key, $settings)) fail("missing database field " . $key);
    }
    foreach (["host", "tlsServerName"] as $key) {
        if (!is_string($settings[$key]) || $settings[$key] === "") fail("invalid " . $key);
        $isIp = filter_var($settings[$key], FILTER_VALIDATE_IP) !== false;
        $isDns = preg_match(
            "/\\A(?=.{1,253}\\z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\.)*" .
            "[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\z/",
            $settings[$key]
        ) === 1;
        if (!$isIp && !$isDns) fail("invalid " . $key);
    }
    foreach (["username", "password", "database"] as $key) {
        if (!is_string($settings[$key]) || $settings[$key] === "") fail("invalid " . $key);
    }
    $port = filter_var($settings["port"], FILTER_VALIDATE_INT,
        ["options" => ["min_range" => 1, "max_range" => 65535]]);
    $localPort = filter_var($settings["localPort"] ?? 13306, FILTER_VALIDATE_INT,
        ["options" => ["min_range" => 1024, "max_range" => 65535]]);
    if ($port === false || $localPort === false) fail("invalid database port");

    $marker = "// !!! END - DONT CHANGE ANYTHING AFTER THIS LINE !!!";
    $markerOffset = strpos($source, $marker);
    if ($markerOffset === false) fail("upstream configuration marker is missing");
    $tail = substr($source, $markerOffset + strlen($marker));

    $phpSettings = [
        "driver" => "mysql",
        "hostname" => "127.0.0.1",
        "port" => $localPort,
        "username" => $settings["username"],
        "password" => $settings["password"],
        "database" => $settings["database"],
    ];
    $header = "<?php\n\n// !!! SET YOUR SQL-CONNECTION SETTINGS HERE: !!!\n\n";
    foreach ($phpSettings as $name => $value) {
        $header .= "$" . $name . " = " . var_export($value, true) . ";\n";
    }
    $header .= "\n// !!! END - DONT CHANGE ANYTHING AFTER THIS LINE !!!";
    if (file_put_contents($argv[2], $header . $tail) === false) fail("failed to write sql.php");

    $connectHost = filter_var($settings["host"], FILTER_VALIDATE_IP, FILTER_FLAG_IPV6) !== false
        ? "[" . $settings["host"] . "]"
        : $settings["host"];
    $tlsIdentityIsIp = filter_var($settings["tlsServerName"], FILTER_VALIDATE_IP) !== false;
    $tlsIdentityCheck = $tlsIdentityIsIp ? "checkIP" : "checkHost";
    $stunnel = "foreground = no\n" .
        "pid = /run/stunnel/mariadb.pid\n" .
        "client = yes\n\n" .
        "[mariadb]\n" .
        "accept = 127.0.0.1:" . $localPort . "\n" .
        "connect = " . $connectHost . ":" . $port . "\n" .
        "CAfile = /bootstrap/tls/ca.crt\n" .
        "verifyChain = yes\n" .
        $tlsIdentityCheck . " = " . $settings["tlsServerName"] . "\n";
    if (!$tlsIdentityIsIp) $stunnel .= "sni = " . $settings["tlsServerName"] . "\n";
    if (file_put_contents($argv[4], $stunnel) === false) fail("failed to write stunnel config");
' "$upstream_sql" "$generated_sql" "$database_json" "$stunnel_config"

chown www-data:www-data "$generated_sql"
chmod 0400 "$generated_sql"
chmod 0600 "$stunnel_config"

stunnel "$stunnel_config"
php-fpm --daemonize
exec nginx -g 'daemon off;'
