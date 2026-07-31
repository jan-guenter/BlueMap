#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

ssh_public_key="${BLUEMAP_RUNPOD_SSH_PUBLIC_KEY:-}"
if [[ "$ssh_public_key" == *$'\r'* || "$ssh_public_key" == *$'\n'* ]]; then
    fail "BLUEMAP_RUNPOD_SSH_PUBLIC_KEY must not contain CR or LF"
fi
[[ "$ssh_public_key" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+([[:space:]].*)?$ ]] ||
    fail "BLUEMAP_RUNPOD_SSH_PUBLIC_KEY must contain one Ed25519 public key"
[[ "${BLUEMAP_RUNPOD_RUN_ID:-}" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] ||
    fail "BLUEMAP_RUNPOD_RUN_ID is missing or invalid"
[[ "${BLUEMAP_RUNPOD_IMAGE_DIGEST:-}" =~ ^sha256:[a-f0-9]{64}$ ]] ||
    fail "BLUEMAP_RUNPOD_IMAGE_DIGEST must be an immutable sha256 digest"

build_identity=/runner/loadgen-build.json
[[ -f "$build_identity" && ! -L "$build_identity" && -r "$build_identity" ]] ||
    fail "baked load-generator build identity is unavailable"
jq -e '
    type == "object"
    and (keys == ["formatVersion", "sourceRevision"])
    and .formatVersion == 1
    and (.sourceRevision | type == "string"
        and length == 40
        and (gsub("[a-f0-9]"; "") | length == 0)
        and . != "0000000000000000000000000000000000000000")
' "$build_identity" >/dev/null ||
    fail "baked load-generator build identity is malformed"
source_revision="$(jq -r '.sourceRevision' "$build_identity")"

install -m 0600 -o loadgen -g loadgen /dev/null /home/loadgen/.ssh/authorized_keys
printf '%s\n' "$ssh_public_key" > /home/loadgen/.ssh/authorized_keys
unset BLUEMAP_RUNPOD_SSH_PUBLIC_KEY
unset ssh_public_key

if [[ ! -s /etc/ssh/ssh_host_ed25519_key ]]; then
    ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key
fi

install -m 0444 -o root -g root /dev/null /runner/runpod-environment.json
jq -n \
    --arg runId "$BLUEMAP_RUNPOD_RUN_ID" \
    --arg imageDigest "$BLUEMAP_RUNPOD_IMAGE_DIGEST" \
    --arg sourceRevision "$source_revision" \
    --arg podId "${RUNPOD_POD_ID:-unknown}" \
    --arg dataCenterId "${RUNPOD_DC_ID:-unknown}" \
    --arg podHostname "${RUNPOD_POD_HOSTNAME:-unknown}" \
    --arg publicIp "${RUNPOD_PUBLIC_IP:-unknown}" \
    --arg cpuFlavor "${BLUEMAP_RUNPOD_CPU_FLAVOR:-unknown}" \
    --argjson vcpuCount "${RUNPOD_CPU_COUNT:-0}" \
    --argjson configuredVcpuCount "${BLUEMAP_RUNPOD_VCPU_COUNT:-0}" \
    --arg startedAt "$(date -u +'%Y-%m-%dT%H:%M:%S.%3NZ')" \
    '{
        formatVersion: 1,
        runId: $runId,
        imageDigest: $imageDigest,
        sourceRevision: $sourceRevision,
        runpod: {
            podId: $podId,
            dataCenterId: $dataCenterId,
            podHostname: $podHostname,
            publicIp: $publicIp,
            cpuFlavor: $cpuFlavor,
            vcpuCount: $vcpuCount,
            configuredVcpuCount: $configuredVcpuCount
        },
        startedAt: $startedAt
    }' > /runner/runpod-environment.json

chmod 0444 /runner/runpod-environment.json
unset RUNPOD_API_KEY

haproxy_pid=""
sshd_pid=""

stop_services() {
    local pid

    trap - TERM INT
    for pid in "$haproxy_pid" "$sshd_pid"; do
        [[ -n "$pid" ]] || continue
        kill -TERM "$pid" 2>/dev/null || true
    done
    for pid in "$haproxy_pid" "$sshd_pid"; do
        [[ -n "$pid" ]] || continue
        wait "$pid" 2>/dev/null || true
    done
}

trap 'stop_services; exit 143' TERM
trap 'stop_services; exit 130' INT

/usr/sbin/haproxy -c -f /etc/haproxy/haproxy.cfg >/dev/null ||
    fail "HAProxy configuration is invalid"
/usr/sbin/haproxy -W -db -f /etc/haproxy/haproxy.cfg &
haproxy_pid="$!"
/usr/sbin/sshd -D -e &
sshd_pid="$!"

set +e
wait -n "$haproxy_pid" "$sshd_pid"
service_status="$?"
set -e

if kill -0 "$haproxy_pid" 2>/dev/null; then
    stopped_service="sshd"
else
    stopped_service="haproxy"
fi
stop_services
fail "$stopped_service exited unexpectedly with status $service_status"
