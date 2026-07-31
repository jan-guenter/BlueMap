#!/usr/bin/env bash
set -Eeuo pipefail

output="${1:-}"
stop_file="${2:-}"
[[ "$output" =~ ^/artifacts/[A-Za-z0-9._/-]+$ ]] || exit 1
[[ "$stop_file" == "${output}.stop" ]] || exit 1
[[ -r /proc/net/dev ]] || exit 1

read_number() {
    local path="$1"
    local value
    value="$(<"$path")"
    [[ "$value" =~ ^[0-9]+$ ]] || exit 1
    printf '%s\n' "$value"
}

cgroup_version=0
if [[ -r /sys/fs/cgroup/cgroup.controllers ]]; then
    [[ -r /sys/fs/cgroup/cpu.stat &&
        -r /sys/fs/cgroup/memory.current ]] &&
        grep -q '^usage_usec [0-9][0-9]*$' /sys/fs/cgroup/cpu.stat &&
        grep -q '^throttled_usec [0-9][0-9]*$' /sys/fs/cgroup/cpu.stat ||
        exit 1
    cgroup_version=2
    cpu_stat_path=/sys/fs/cgroup/cpu.stat
    memory_current_path=/sys/fs/cgroup/memory.current
else
    cpu_control_root=""
    cpu_accounting_root=""
    for candidate in \
        /sys/fs/cgroup/cpu,cpuacct \
        /sys/fs/cgroup/cpuacct,cpu \
        /sys/fs/cgroup/cpu; do
        if [[ -r "$candidate/cpu.stat" ]]; then
            cpu_control_root="$candidate"
            break
        fi
    done
    for candidate in \
        /sys/fs/cgroup/cpu,cpuacct \
        /sys/fs/cgroup/cpuacct,cpu \
        /sys/fs/cgroup/cpuacct; do
        if [[ -r "$candidate/cpuacct.usage" ]]; then
            cpu_accounting_root="$candidate"
            break
        fi
    done
    if [[ -n "$cpu_control_root" &&
        -n "$cpu_accounting_root" &&
        -r /sys/fs/cgroup/memory/memory.usage_in_bytes ]] &&
        grep -q '^throttled_time [0-9][0-9]*$' "$cpu_control_root/cpu.stat"; then
        cgroup_version=1
        cpu_usage_path="$cpu_accounting_root/cpuacct.usage"
        cpu_stat_path="$cpu_control_root/cpu.stat"
        memory_current_path=/sys/fs/cgroup/memory/memory.usage_in_bytes
    fi
fi
((cgroup_version > 0)) || exit 1
: > "$output"

while [[ ! -e "$stop_file" ]]; do
    if ((cgroup_version == 2)); then
        cpu_usage_usec="$(awk '$1 == "usage_usec" {print $2}' "$cpu_stat_path")"
        cpu_throttled_usec="$(
            awk '$1 == "throttled_usec" {print $2}' "$cpu_stat_path"
        )"
    else
        cpu_usage_nanoseconds="$(read_number "$cpu_usage_path")"
        cpu_throttled_nanoseconds="$(
            awk '$1 == "throttled_time" {print $2}' "$cpu_stat_path"
        )"
        [[ "$cpu_throttled_nanoseconds" =~ ^[0-9]+$ ]] || exit 1
        cpu_usage_usec="$((cpu_usage_nanoseconds / 1000))"
        cpu_throttled_usec="$((cpu_throttled_nanoseconds / 1000))"
    fi
    memory_current="$(read_number "$memory_current_path")"
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
