#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${BLUEMAP_PHASE_TIMEOUT_SECONDS:-}" =~ ^[1-9][0-9]*$ ]] || {
    printf 'ERROR: BLUEMAP_PHASE_TIMEOUT_SECONDS must be a positive integer\n' >&2
    exit 1
}
[[ "${1:-}" == "--resource-output" && -n "${2:-}" ]] || {
    printf 'Usage: bluemap-runpod-run-phase --resource-output FILE -- COMMAND [ARG...]\n' >&2
    exit 1
}

resource_output="$2"
shift 2
[[ "${1:-}" == "--" ]] || {
    printf 'ERROR: missing command separator\n' >&2
    exit 1
}
shift
(($# > 0)) || {
    printf 'ERROR: phase command is empty\n' >&2
    exit 1
}
[[ "$resource_output" =~ ^/artifacts/[A-Za-z0-9._/-]+$ ]] || {
    printf 'ERROR: resource output must be below /artifacts\n' >&2
    exit 1
}

stop_file="${resource_output}.stop"
rm -f -- "$stop_file"
bluemap-runpod-sample-resources "$resource_output" "$stop_file" &
sampler_pid=$!

# Invoked by the EXIT/INT/TERM trap below.
# shellcheck disable=SC2317,SC2329
cleanup() {
    set +e
    : > "$stop_file"
    wait "$sampler_pid"
    rm -f -- "$stop_file"
}
trap cleanup EXIT INT TERM

set +e
timeout \
    --signal=TERM \
    --kill-after=30s \
    "${BLUEMAP_PHASE_TIMEOUT_SECONDS}s" \
    "$@"
command_status=$?
set -e

if ! kill -0 "$sampler_pid" >/dev/null 2>&1; then
    printf 'ERROR: load-generator resource sampler exited during the phase\n' >&2
    exit 125
fi
exit "$command_status"
