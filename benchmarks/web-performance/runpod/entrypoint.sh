#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

[[ "${BLUEMAP_RUNPOD_SSH_PUBLIC_KEY:-}" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+([[:space:]].*)?$ ]] ||
    fail "BLUEMAP_RUNPOD_SSH_PUBLIC_KEY must contain one Ed25519 public key"
[[ "${BLUEMAP_RUNPOD_RUN_ID:-}" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] ||
    fail "BLUEMAP_RUNPOD_RUN_ID is missing or invalid"
[[ "${BLUEMAP_RUNPOD_IMAGE_DIGEST:-}" =~ ^sha256:[a-f0-9]{64}$ ]] ||
    fail "BLUEMAP_RUNPOD_IMAGE_DIGEST must be an immutable sha256 digest"

install -m 0600 -o loadgen -g loadgen /dev/null /home/loadgen/.ssh/authorized_keys
printf '%s\n' "$BLUEMAP_RUNPOD_SSH_PUBLIC_KEY" > /home/loadgen/.ssh/authorized_keys
unset BLUEMAP_RUNPOD_SSH_PUBLIC_KEY

if [[ ! -s /etc/ssh/ssh_host_ed25519_key ]]; then
    ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key
fi

install -m 0444 -o root -g root /dev/null /runner/runpod-environment.json
jq -n \
    --arg runId "$BLUEMAP_RUNPOD_RUN_ID" \
    --arg imageDigest "$BLUEMAP_RUNPOD_IMAGE_DIGEST" \
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
exec /usr/sbin/sshd -D -e
