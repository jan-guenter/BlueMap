#!/usr/bin/env bash
set -Eeuo pipefail

output="${1:-}"
stop_file="${2:-}"
[[ "$output" =~ ^/artifacts/[A-Za-z0-9._/-]+$ ]] || exit 1
[[ "$stop_file" == "${output}.stop" ]] || exit 1
[[ -r /sys/fs/cgroup/cpu.stat ]] || exit 1
[[ -r /sys/fs/cgroup/memory.current ]] || exit 1
[[ -r /proc/net/dev ]] || exit 1
grep -q '^usage_usec [0-9][0-9]*$' /sys/fs/cgroup/cpu.stat || exit 1
grep -q '^throttled_usec [0-9][0-9]*$' /sys/fs/cgroup/cpu.stat || exit 1
: > "$output"

read_number() {
    local path="$1"
    local value
    value="$(<"$path")"
    [[ "$value" =~ ^[0-9]+$ ]] || exit 1
    printf '%s\n' "$value"
}

while [[ ! -e "$stop_file" ]]; do
    cpu_usage_usec="$(awk '$1 == "usage_usec" {print $2}' /sys/fs/cgroup/cpu.stat)"
    cpu_throttled_usec="$(
        awk '$1 == "throttled_usec" {print $2}' /sys/fs/cgroup/cpu.stat
    )"
    memory_current="$(read_number /sys/fs/cgroup/memory.current)"
    rx_bytes="$(
        awk -F '[: ]+' '$2 != "lo" && NF >= 11 {sum += $3} END {print sum + 0}' \
            /proc/net/dev
    )"
    tx_bytes="$(
        awk -F '[: ]+' '$2 != "lo" && NF >= 11 {sum += $11} END {print sum + 0}' \
            /proc/net/dev
    )"

    jq -nc \
        --arg capturedAt "$(date -u +'%Y-%m-%dT%H:%M:%S.%3NZ')" \
        --argjson cpuUsageUsec "${cpu_usage_usec:-0}" \
        --argjson cpuThrottledUsec "${cpu_throttled_usec:-0}" \
        --argjson memoryCurrentBytes "${memory_current:-0}" \
        --argjson rxBytes "${rx_bytes:-0}" \
        --argjson txBytes "${tx_bytes:-0}" \
        '{
            capturedAt: $capturedAt,
            cpuUsageUsec: $cpuUsageUsec,
            cpuThrottledUsec: $cpuThrottledUsec,
            memoryCurrentBytes: $memoryCurrentBytes,
            network: {
                rxBytes: $rxBytes,
                txBytes: $txBytes
            }
        }' >> "$output"
    sleep 1
done
