#!/usr/bin/env bash
set -Eeuo pipefail

environment_file=/runner/runpod-environment.json
[[ -f "$environment_file" && ! -L "$environment_file" && -r "$environment_file" ]] || {
    printf 'ERROR: %s is unavailable\n' "$environment_file" >&2
    exit 1
}
jq -e '
    type == "object"
    and .formatVersion == 1
    and (.sourceRevision | type == "string"
        and length == 40
        and (gsub("[a-f0-9]"; "") | length == 0)
        and . != "0000000000000000000000000000000000000000")
' "$environment_file" >/dev/null || {
    printf 'ERROR: %s has invalid build provenance\n' "$environment_file" >&2
    exit 1
}

cpu_list_count() {
    local cpu_list="$1"
    [[ -n "$cpu_list" ]] || return 1
    awk -v cpu_list="$cpu_list" '
        BEGIN {
            part_count = split(cpu_list, parts, ",")
            total = 0
            for (part_index = 1; part_index <= part_count; part_index++) {
                range_count = split(parts[part_index], bounds, "-")
                if (range_count < 1 || range_count > 2 ||
                    bounds[1] !~ /^[0-9]+$/ ||
                    (range_count == 2 && bounds[2] !~ /^[0-9]+$/)) {
                    exit 1
                }
                first = bounds[1] + 0
                last = range_count == 2 ? bounds[2] + 0 : first
                if (last < first) {
                    exit 1
                }
                for (cpu = first; cpu <= last; cpu++) {
                    if (seen[cpu]++) {
                        exit 1
                    }
                    total++
                }
            }
            if (total < 1) {
                exit 1
            }
            print total
        }
    '
}

memory_capacity_bytes() {
    local cgroup_limit=""
    local memory_total
    memory_total="$(awk '/^MemTotal:/ {printf "%.0f\n", $2 * 1024}' /proc/meminfo)"
    if [[ -r /sys/fs/cgroup/memory.max ]]; then
        cgroup_limit="$(< /sys/fs/cgroup/memory.max)"
    elif [[ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]]; then
        cgroup_limit="$(< /sys/fs/cgroup/memory/memory.limit_in_bytes)"
    fi
    if [[ "$cgroup_limit" =~ ^[1-9][0-9]*$ ]] &&
        ((cgroup_limit <= memory_total)); then
        printf '%s\n' "$cgroup_limit"
    else
        printf '%s\n' "$memory_total"
    fi
}

cgroup_version=0
cpu_quota=""
cpu_period=""
cpuset_effective=""
if [[ -r /sys/fs/cgroup/cgroup.controllers ]]; then
    [[ -r /sys/fs/cgroup/cpu.max &&
        -r /sys/fs/cgroup/cpuset.cpus.effective ]] || {
        printf 'ERROR: cgroup v2 CPU controls are incomplete\n' >&2
        exit 1
    }
    cgroup_version=2
    cpu_max="$(< /sys/fs/cgroup/cpu.max)"
    read -r cpu_quota cpu_period cpu_max_extra <<<"$cpu_max"
    [[ -z "${cpu_max_extra:-}" ]] || {
        printf 'ERROR: cgroup v2 cpu.max is malformed\n' >&2
        exit 1
    }
    cpuset_effective="$(< /sys/fs/cgroup/cpuset.cpus.effective)"
else
    cpu_v1_root=""
    for candidate in \
        /sys/fs/cgroup/cpu,cpuacct \
        /sys/fs/cgroup/cpuacct,cpu \
        /sys/fs/cgroup/cpu; do
        if [[ -r "$candidate/cpu.cfs_quota_us" &&
            -r "$candidate/cpu.cfs_period_us" ]]; then
            cpu_v1_root="$candidate"
            break
        fi
    done
    cpuset_v1_root=/sys/fs/cgroup/cpuset
    if [[ -n "$cpu_v1_root" &&
        -r "$cpuset_v1_root/cpuset.cpus" ]]; then
        cgroup_version=1
        cpu_quota="$(< "$cpu_v1_root/cpu.cfs_quota_us")"
        cpu_period="$(< "$cpu_v1_root/cpu.cfs_period_us")"
        [[ "$cpu_quota" == "-1" ]] && cpu_quota=max
        if [[ -r "$cpuset_v1_root/cpuset.effective_cpus" ]]; then
            cpuset_effective="$(< "$cpuset_v1_root/cpuset.effective_cpus")"
        else
            cpuset_effective="$(< "$cpuset_v1_root/cpuset.cpus")"
        fi
    fi
fi
((cgroup_version > 0)) || {
    printf 'ERROR: supported cgroup CPU controls are unavailable\n' >&2
    exit 1
}
[[ "$cpu_period" =~ ^[1-9][0-9]*$ ]] || {
    printf 'ERROR: cgroup CPU period is malformed\n' >&2
    exit 1
}
cpu_max="$cpu_quota $cpu_period"

cpu_quota_json=null
cpu_quota_vcpu_json=null
if [[ "$cpu_quota" != "max" ]]; then
    [[ "$cpu_quota" =~ ^[1-9][0-9]*$ ]] || {
        printf 'ERROR: cgroup CPU quota is malformed\n' >&2
        exit 1
    }
    ((cpu_quota % cpu_period == 0)) || {
        printf 'ERROR: cgroup CPU quota is not an integral vCPU allocation\n' >&2
        exit 1
    }
    cpu_quota_json="$cpu_quota"
    cpu_quota_vcpu_json="$((cpu_quota / cpu_period))"
fi

cpuset_effective_count="$(cpu_list_count "$cpuset_effective")" || {
    printf 'ERROR: effective cgroup CPU set is malformed\n' >&2
    exit 1
}
affinity="$(
    awk -F ':[[:space:]]*' '$1 == "Cpus_allowed_list" {print $2}' /proc/self/status
)"
affinity_count="$(cpu_list_count "$affinity")" || {
    printf 'ERROR: process CPU affinity is malformed\n' >&2
    exit 1
}

effective_vcpu_count="$cpuset_effective_count"
((affinity_count < effective_vcpu_count)) &&
    effective_vcpu_count="$affinity_count"
if [[ "$cpu_quota_vcpu_json" != "null" ]] &&
    ((cpu_quota_vcpu_json < effective_vcpu_count)); then
    effective_vcpu_count="$cpu_quota_vcpu_json"
fi
((effective_vcpu_count == 8)) || {
    printf 'ERROR: independently observed effective CPU capacity is not 8 vCPU\n' >&2
    exit 1
}

jq \
    --arg capturedAt "$(date -u +'%Y-%m-%dT%H:%M:%S.%3NZ')" \
    --arg hostname "$(hostname)" \
    --arg kernel "$(uname -srmo)" \
    --arg k6Version "$(k6 version | head -n 1)" \
    --arg cgroupCpuMax "$cpu_max" \
    --arg cpusetEffective "$cpuset_effective" \
    --arg affinity "$affinity" \
    --argjson cpuQuotaMicros "$cpu_quota_json" \
    --argjson cpuPeriodMicros "$cpu_period" \
    --argjson cpuQuotaVcpuCount "$cpu_quota_vcpu_json" \
    --argjson cpusetEffectiveCount "$cpuset_effective_count" \
    --argjson affinityCount "$affinity_count" \
    --argjson effectiveVcpuCount "$effective_vcpu_count" \
    --argjson cgroupVersion "$cgroup_version" \
    --argjson onlineProcessors "$(getconf _NPROCESSORS_ONLN)" \
    --argjson memoryBytes "$(awk '/^MemTotal:/ {print $2 * 1024}' /proc/meminfo)" \
    --argjson memoryCapacityBytes "$(memory_capacity_bytes)" \
    '. + {
        capturedAt: $capturedAt,
        runtime: {
            hostname: $hostname,
            kernel: $kernel,
            k6Version: $k6Version,
            cgroupVersion: $cgroupVersion,
            cpu: {
                cgroupCpuMax: $cgroupCpuMax,
                quotaMicros: $cpuQuotaMicros,
                periodMicros: $cpuPeriodMicros,
                quotaVcpuCount: $cpuQuotaVcpuCount,
                cpusetEffective: $cpusetEffective,
                cpusetEffectiveCount: $cpusetEffectiveCount,
                affinity: $affinity,
                affinityCount: $affinityCount,
                effectiveVcpuCount: $effectiveVcpuCount
            },
            onlineProcessors: $onlineProcessors,
            memoryBytes: $memoryBytes,
            memoryCapacityBytes: $memoryCapacityBytes
        }
    }' "$environment_file"
