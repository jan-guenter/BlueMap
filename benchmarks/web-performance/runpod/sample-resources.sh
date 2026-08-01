#!/usr/bin/env bash
set -Eeuo pipefail

output="${1:-}"
stop_file="${2:-}"
ready_file="${3:-}"
[[ "$output" =~ ^/artifacts/[A-Za-z0-9._/-]+$ ]] || exit 1
[[ "$stop_file" == "${output}.stop" ]] || exit 1
[[ "$ready_file" == "${output}.ready" ]] || exit 1
[[ ! -e "$output" && ! -L "$output" ]] || exit 1
[[ ! -e "$ready_file" && ! -L "$ready_file" ]] || exit 1
[[ -r /proc/net/dev ]] || exit 1
readonly HAPROXY_STATS_SOCKET=/run/haproxy/bluemap-stats.sock
readonly SOURCE_PATH=/usr/local/bin/bluemap-runpod-sample-resources
[[ -S "$HAPROXY_STATS_SOCKET" && -r "$SOURCE_PATH" ]] || exit 1
source_sha256="$(sha256sum -- "$SOURCE_PATH" | awk '{print $1}')"
[[ "$source_sha256" =~ ^[a-f0-9]{64}$ ]] || exit 1

read_number() {
    local path="$1"
    local value
    value="$(<"$path")"
    [[ "$value" =~ ^[0-9]+$ ]] || exit 1
    printf '%s\n' "$value"
}

tcp_counter() {
    local path="$1"
    local prefix="$2"
    local counter="$3"
    awk -v prefix="$prefix:" -v counter="$counter" '
        $1 == prefix && !header_seen {
            for (field_index = 2; field_index <= NF; field_index++) {
                if ($field_index == counter) wanted = field_index
            }
            header_seen = 1
            next
        }
        $1 == prefix && header_seen {
            if (wanted == 0 || wanted > NF || $wanted !~ /^[0-9]+$/) exit 1
            print $wanted
            found = 1
            exit
        }
        END { if (!found) exit 1 }
    ' "$path"
}

capture_haproxy() {
    local raw="$1"
    printf 'show stat\n' |
        socat -T 2 - "UNIX-CONNECT:$HAPROXY_STATS_SOCKET" > "$raw" ||
        return 1
    jq -Rsc '
        def exact_uint:
            if type == "string" and test("^[0-9]+$")
            then tonumber else error("invalid HAProxy unsigned integer") end;
        def object($header):
            . as $row
            | reduce range(0; ($header | length)) as $index
                ({}; .[$header[$index]] = ($row[$index] // ""));
        def metric:
            {
                qcur: (.qcur | exact_uint),
                qmax: (.qmax | exact_uint),
                scur: (.scur | exact_uint),
                smax: (.smax | exact_uint),
                stot: (.stot | exact_uint),
                bin: (.bin | exact_uint),
                bout: (.bout | exact_uint),
                econ: (.econ | exact_uint),
                eresp: (.eresp | exact_uint),
                wretr: (.wretr | exact_uint),
                wredis: (.wredis | exact_uint),
                status: (
                    if (.status | type) == "string" and (.status | length) > 0
                    then .status else error("empty HAProxy status") end
                )
            };
        # The HAProxy runtime CSV uses fixed unquoted field names and the
        # selected numeric/status columns cannot contain commas.
        (split("\n") | map(select(length > 0) | split(","))) as $rows
        | if ($rows | length) < 10 then error("incomplete HAProxy stats") else . end
        | ($rows[0] | map(sub("^# "; ""))) as $header
        | [$rows[1:][] | object($header)] as $objects
        | [$objects[] | select(.pxname == "bluemap_ssh_lanes")] as $backend
        | [$backend[] | select(.svname == "BACKEND")] as $aggregate
        | [range(1; 9) as $index
            | [$backend[] | select(.svname == "lane_\($index)")] as $matches
            | if ($matches | length) != 1
              then error("missing or duplicate HAProxy lane")
              else ($matches[0] | metric + {
                  id: "lane-\($index)",
                  serverName: "lane_\($index)"
              })
              end
          ] as $lanes
        | if ($aggregate | length) != 1
          then error("missing or duplicate HAProxy backend aggregate")
          else {
              backend: ($aggregate[0] | metric + {
                  id: "backend",
                  serverName: "BACKEND"
              }),
              lanes: $lanes
          }
          end
    ' "$raw"
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
runtime_dir="$(mktemp -d /tmp/bluemap-runpod-resource-sampler.XXXXXXXX)"
trap 'rm -rf -- "$runtime_dir"' EXIT
sample_count=0

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
    tcp_retrans_segs="$(tcp_counter /proc/net/snmp Tcp RetransSegs)"
    tcp_timeouts="$(tcp_counter /proc/net/netstat TcpExt TCPTimeouts)"
    tcp_syn_retrans="$(tcp_counter /proc/net/netstat TcpExt TCPSynRetrans)"
    haproxy_json="$(capture_haproxy "$runtime_dir/haproxy.csv")"

    jq -nc \
        --arg capturedAt "$(date -u +'%Y-%m-%dT%H:%M:%S.%3NZ')" \
        --arg sourceSha256 "$source_sha256" \
        --argjson cpuUsageUsec "${cpu_usage_usec:-0}" \
        --argjson cpuThrottledUsec "${cpu_throttled_usec:-0}" \
        --argjson memoryCurrentBytes "${memory_current:-0}" \
        --argjson rxBytes "${rx_bytes:-0}" \
        --argjson txBytes "${tx_bytes:-0}" \
        --argjson tcpRetransSegs "$tcp_retrans_segs" \
        --argjson tcpTimeouts "$tcp_timeouts" \
        --argjson tcpSynRetrans "$tcp_syn_retrans" \
        --argjson haproxy "$haproxy_json" \
        '{
            formatVersion: 2,
            kind: "runpod-resource-transport-sample",
            capturedAt: $capturedAt,
            sourceSha256: $sourceSha256,
            cpuUsageUsec: $cpuUsageUsec,
            cpuThrottledUsec: $cpuThrottledUsec,
            memoryCurrentBytes: $memoryCurrentBytes,
            network: {
                rxBytes: $rxBytes,
                txBytes: $txBytes
            },
            transport: {
                tcp: {
                    retransSegs: $tcpRetransSegs,
                    tcpTimeouts: $tcpTimeouts,
                    tcpSynRetrans: $tcpSynRetrans
                },
                haproxy: $haproxy
            }
        }' >> "$output"
    ((sample_count += 1))
    if ((sample_count == 1)); then
        ready_temp="${ready_file}.tmp.$$"
        jq -nc \
            --arg sourceSha256 "$source_sha256" \
            '{
                formatVersion: 1,
                kind: "runpod-resource-transport-ready",
                sampleCount: 1,
                sourceSha256: $sourceSha256
            }' > "$ready_temp"
        mv -- "$ready_temp" "$ready_file"
    fi
    sleep 1
done
