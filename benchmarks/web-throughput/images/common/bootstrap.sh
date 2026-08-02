#!/bin/sh

# Shared RunPod bootstrap contract. This file is sourced by each role
# entrypoint; it intentionally contains no credentials or private keys.

bootstrap_root=/bootstrap
bootstrap_authorized_keys=/bootstrap/authorized_keys
bootstrap_start_marker=/bootstrap/start
bootstrap_timeout_seconds="${BENCHMARK_BOOTSTRAP_TIMEOUT_SECONDS:-3600}"

bootstrap_fail() {
    echo "benchmark bootstrap: $*" >&2
    exit 78
}

bootstrap_validate_timeout() {
    case "$bootstrap_timeout_seconds" in
        "" | *[!0-9]*) bootstrap_fail "timeout must be a positive integer" ;;
    esac
    [ "$bootstrap_timeout_seconds" -gt 0 ] || bootstrap_fail "timeout must be positive"
}

bootstrap_install_public_key() {
    mkdir -p "$bootstrap_root" /run/sshd /root/.ssh
    chmod 0700 /root/.ssh

    if [ -n "${BENCHMARK_SSH_PUBLIC_KEY:-}" ]; then
        case "$BENCHMARK_SSH_PUBLIC_KEY" in
            *"
"*) bootstrap_fail "BENCHMARK_SSH_PUBLIC_KEY must contain exactly one key" ;;
        esac
        printf '%s\n' "$BENCHMARK_SSH_PUBLIC_KEY" > "$bootstrap_authorized_keys"
    fi

    [ -s "$bootstrap_authorized_keys" ] || \
        bootstrap_fail "provide one public key via BENCHMARK_SSH_PUBLIC_KEY or $bootstrap_authorized_keys"

    key_lines="$(awk 'NF { count++ } END { print count + 0 }' "$bootstrap_authorized_keys")"
    [ "$key_lines" -eq 1 ] || bootstrap_fail "exactly one non-empty SSH public key is required"
    awk '
        NF && $1 !~ /^(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|ssh-rsa)$/ { exit 1 }
    ' "$bootstrap_authorized_keys" || bootstrap_fail "SSH key options and unsupported key types are rejected"
    ssh-keygen -l -f "$bootstrap_authorized_keys" >/dev/null 2>&1 || \
        bootstrap_fail "authorized key is malformed"

    chown root:root "$bootstrap_authorized_keys"
    chmod 0600 "$bootstrap_authorized_keys"
}

bootstrap_start_ssh() {
    bootstrap_validate_timeout
    bootstrap_install_public_key
    ssh-keygen -A >/dev/null
    /usr/sbin/sshd -f /etc/ssh/sshd_config
    [ -s /run/sshd.pid ] || bootstrap_fail "sshd did not create its pid file"
    echo "benchmark bootstrap: SSH ready; waiting for explicit start marker" >&2
}

bootstrap_assert_ssh_alive() {
    sshd_pid="$(cat /run/sshd.pid 2>/dev/null || true)"
    if [ -z "$sshd_pid" ] || ! kill -0 "$sshd_pid" 2>/dev/null; then
        bootstrap_fail "sshd exited during bootstrap"
    fi
}

bootstrap_wait_for_path() {
    required_path="$1"
    description="$2"
    waited=0
    while [ ! -e "$required_path" ]; do
        bootstrap_assert_ssh_alive
        [ "$waited" -lt "$bootstrap_timeout_seconds" ] || \
            bootstrap_fail "timed out waiting for $description at $required_path"
        sleep 1
        waited=$((waited + 1))
    done
}

bootstrap_wait_for_start() {
    bootstrap_wait_for_path "$bootstrap_start_marker" "explicit start marker"
    [ -f "$bootstrap_start_marker" ] || bootstrap_fail "start marker must be a regular file"
    [ ! -L "$bootstrap_start_marker" ] || bootstrap_fail "start marker must not be a symlink"
    bootstrap_assert_ssh_alive
    echo "benchmark bootstrap: explicit start authorized" >&2
}

bootstrap_validate_java_webserver_config() {
    webserver_config="$1"
    if [ ! -f "$webserver_config" ] || [ -L "$webserver_config" ]; then
        bootstrap_fail "webserver config must be a regular non-symlink file: $webserver_config"
    fi

    if awk '
        {
            sub(/#.*/, "")
            if ($0 ~ /^[[:space:]]*file[[:space:]]*[:=]/) found = 1
        }
        END { exit found ? 0 : 1 }
    ' "$webserver_config"; then
        bootstrap_fail "per-request webserver file logging must be disabled"
    fi

    configured_ports="$(awk '
        {
            sub(/#.*/, "")
            if ($0 ~ /^[[:space:]]*port[[:space:]]*[:=]/) {
                sub(/^[[:space:]]*port[[:space:]]*[:=][[:space:]]*/, "")
                sub(/[[:space:]].*$/, "")
                print
            }
        }
    ' "$webserver_config")"
    [ "$configured_ports" = "8100" ] || \
        bootstrap_fail "webserver config must contain exactly one active port: 8100 setting"
}
