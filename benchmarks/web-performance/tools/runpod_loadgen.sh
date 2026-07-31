#!/usr/bin/env bash
set -Eeuo pipefail

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage:
  runpod_loadgen.sh --identity FILE --identity-key FILE validate
  runpod_loadgen.sh --identity FILE --identity-key FILE exec COMMAND [ARG...]
  runpod_loadgen.sh --identity FILE --identity-key FILE exec-traefik-forward COMMAND [ARG...]
  runpod_loadgen.sh --identity FILE --identity-key FILE copy-to LOCAL REMOTE
  runpod_loadgen.sh --identity FILE --identity-key FILE copy-from REMOTE LOCAL

The identity file is non-secret. The Ed25519 private key must be a regular
owner-readable file that is not group- or world-accessible.
EOF
}

IDENTITY_FILE=""
IDENTITY_KEY=""

while (($# > 0)); do
    case "$1" in
        --identity)
            IDENTITY_FILE="${2:-}"
            shift 2
            ;;
        --identity-key)
            IDENTITY_KEY="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

[[ -n "$IDENTITY_FILE" && -f "$IDENTITY_FILE" && ! -L "$IDENTITY_FILE" ]] ||
    die "--identity must name a regular, non-symlink file"
[[ -n "$IDENTITY_KEY" && -f "$IDENTITY_KEY" && ! -L "$IDENTITY_KEY" ]] ||
    die "--identity-key must name a regular, non-symlink file"

key_mode="$(stat -c '%a' "$IDENTITY_KEY")"
(( (8#$key_mode & 8#077) == 0 )) ||
    die "--identity-key must not be accessible by group or other users"
key_owner="$(stat -c '%u' "$IDENTITY_KEY")"
[[ "$key_owner" == "$(id -u)" ]] ||
    die "--identity-key must be owned by the current user"

jq -e '
    . as $root
    | .formatVersion == 1
    and .backend == "runpod-ssh"
    and (.runId | type == "string" and test("^[a-z0-9][a-z0-9-]{0,62}$"))
    and (.sourceRevision | type == "string"
        and length == 40
        and (gsub("[a-f0-9]"; "") | length == 0)
        and . != "0000000000000000000000000000000000000000")
    and (.runpod.podId | type == "string" and length > 0)
    and (.runpod.machineId | type == "string" and length > 0)
    and (.runpod.dataCenterId | type == "string" and length > 0)
    and .runpod.cpuFlavorId == "cpu5c"
    and .runpod.vcpuCount == 8
    and .runpod.minDownloadMbps == 500
    and .runpod.minUploadMbps == 100
    and (.runpod.maxDownloadMbps | type == "number" and . >= 500)
    and (.runpod.maxUploadMbps | type == "number" and . >= 100)
    and .runpod.secureCloud == true
    and (.runpod.publicIp | type == "string" and length > 0)
    and (.runpod.imageDigest | type == "string"
        and length == 71
        and startswith("sha256:")
        and (.[7:] | length == 64
            and (gsub("[a-f0-9]"; "") | length == 0)
            and . != "0000000000000000000000000000000000000000000000000000000000000000"))
    and (.runpod.image | type == "string"
        and length == 112
        and . == ("ghcr.io/jan-guenter/bluemap-perf-loadgen@"
            + $root.runpod.imageDigest))
    and (.ssh.host | type == "string"
        and test("^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$"))
    and .ssh.host == .runpod.publicIp
    and (.ssh.port | type == "number" and . >= 1 and . <= 65535)
    and .ssh.user == "loadgen"
    and (.ssh.hostKey | type == "string"
        and test("^ssh-ed25519 [A-Za-z0-9+/=]+$"))
    and .remoteRoot == "/artifacts"
' "$IDENTITY_FILE" >/dev/null ||
    die "RunPod load-generator identity is malformed"

run_id="$(jq -r '.runId' "$IDENTITY_FILE")"
host="$(jq -r '.ssh.host' "$IDENTITY_FILE")"
port="$(jq -r '.ssh.port' "$IDENTITY_FILE")"
user="$(jq -r '.ssh.user' "$IDENTITY_FILE")"
host_key="$(jq -r '.ssh.hostKey' "$IDENTITY_FILE")"
expected_image_digest="$(jq -r '.runpod.imageDigest' "$IDENTITY_FILE")"
expected_source_revision="$(jq -r '.sourceRevision' "$IDENTITY_FILE")"
expected_pod_id="$(jq -r '.runpod.podId' "$IDENTITY_FILE")"
expected_data_center_id="$(jq -r '.runpod.dataCenterId' "$IDENTITY_FILE")"
expected_cpu_flavor="$(jq -r '.runpod.cpuFlavorId' "$IDENTITY_FILE")"
expected_vcpu_count="$(jq -r '.runpod.vcpuCount' "$IDENTITY_FILE")"

known_hosts="$(mktemp)"
cleanup() {
    rm -f -- "$known_hosts"
}
trap cleanup EXIT
printf '[%s]:%s %s\n' "$host" "$port" "$host_key" > "$known_hosts"
chmod 0600 "$known_hosts"

ssh_options=(
    -F /dev/null
    -i "$IDENTITY_KEY"
    -o BatchMode=yes
    -o ConnectTimeout=15
    -o IdentitiesOnly=yes
    -o PasswordAuthentication=no
    -o ServerAliveCountMax=3
    -o ServerAliveInterval=15
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$known_hosts"
    -p "$port"
)
scp_options=(
    -F /dev/null
    -i "$IDENTITY_KEY"
    -o BatchMode=yes
    -o ConnectTimeout=15
    -o IdentitiesOnly=yes
    -o PasswordAuthentication=no
    -o ServerAliveCountMax=3
    -o ServerAliveInterval=15
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$known_hosts"
    -P "$port"
)

remote_exec() {
    (($# > 0)) || die "Remote command is empty"
    local quoted=""
    local argument
    for argument in "$@"; do
        printf -v quoted '%s %q' "$quoted" "$argument"
    done
    # The command is intentionally quoted by printf %q for the remote shell.
    # shellcheck disable=SC2029
    ssh "${ssh_options[@]}" "$user@$host" "${quoted# }"
}

remote_exec_traefik_forward() {
    (($# > 0)) || die "Remote command is empty"
    local quoted=""
    local argument
    for argument in "$@"; do
        printf -v quoted '%s %q' "$quoted" "$argument"
    done
    # This purpose-built reverse forward has no user-controlled listen or target.
    # ExitOnForwardFailure prevents k6 from starting without the intended path.
    # shellcheck disable=SC2029
    ssh \
        "${ssh_options[@]}" \
        -o ExitOnForwardFailure=yes \
        -R \
        127.0.0.1:18080:rke2-traefik.kube-system.svc.cluster.local:80 \
        "$user@$host" \
        "${quoted# }"
}

validate_remote_identity() {
    local actual
    actual="$(remote_exec bluemap-runpod-identity)" ||
        die "Could not query the RunPod load-generator identity"
    jq -e \
        --arg runId "$run_id" \
        --arg imageDigest "$expected_image_digest" \
        --arg sourceRevision "$expected_source_revision" \
        --arg podId "$expected_pod_id" \
        --arg dataCenterId "$expected_data_center_id" \
        --arg cpuFlavor "$expected_cpu_flavor" \
        --argjson vcpuCount "$expected_vcpu_count" \
        '
        .formatVersion == 1
        and .runId == $runId
        and .imageDigest == $imageDigest
        and .sourceRevision == $sourceRevision
        and .runpod.podId == $podId
        and .runpod.dataCenterId == $dataCenterId
        and .runpod.cpuFlavor == $cpuFlavor
        and .runpod.vcpuCount == $vcpuCount
        and .runpod.configuredVcpuCount == $vcpuCount
        and .runtime.onlineProcessors >= $vcpuCount
        and (
            .runtime.cgroupVersion == 1
            or .runtime.cgroupVersion == 2
        )
        and (.runtime.cpu | type == "object")
        and (.runtime.cpu | keys | sort) == ([
            "affinity",
            "affinityCount",
            "cgroupCpuMax",
            "cpusetEffective",
            "cpusetEffectiveCount",
            "effectiveVcpuCount",
            "periodMicros",
            "quotaMicros",
            "quotaVcpuCount"
        ] | sort)
        and (.runtime.cpu.cgroupCpuMax | type == "string" and length > 0)
        and (.runtime.cpu.cpusetEffective | type == "string" and length > 0)
        and (.runtime.cpu.affinity | type == "string" and length > 0)
        and .runtime.cpu.cpusetEffectiveCount >= $vcpuCount
        and .runtime.cpu.affinityCount >= $vcpuCount
        and .runtime.cpu.effectiveVcpuCount == $vcpuCount
        and (.runtime.cpu.periodMicros | type == "number" and . > 0)
        and (
            if .runtime.cpu.quotaMicros == null
            then .runtime.cpu.quotaVcpuCount == null
            else
                (.runtime.cpu.quotaMicros | type == "number" and . > 0)
                and (.runtime.cpu.quotaVcpuCount | type == "number"
                    and . >= $vcpuCount)
                and (
                    .runtime.cpu.quotaMicros
                    / .runtime.cpu.periodMicros
                    == .runtime.cpu.quotaVcpuCount
                )
            end
        )
        and (.runtime.memoryCapacityBytes | type == "number" and . > 0)
        and (.runtime.k6Version | startswith("k6 v2.1.0 "))
        ' <<<"$actual" >/dev/null ||
        die "Live RunPod identity differs from the frozen identity"
    jq -S . <<<"$actual"
}

validate_remote_path() {
    local path="$1"
    [[ "$path" =~ ^/artifacts/[A-Za-z0-9._/-]+$ ]] ||
        die "Remote path '$path' must be an absolute child of /artifacts"
    [[ "$path" != *"/../"* && "$path" != */.. && "$path" != *"//"* ]] ||
        die "Remote path '$path' contains an unsafe segment"
}

command_name="${1:-}"
[[ -n "$command_name" ]] || {
    usage >&2
    exit 1
}
shift

case "$command_name" in
    validate)
        (($# == 0)) || die "validate accepts no arguments"
        validate_remote_identity
        ;;
    exec)
        validate_remote_identity >/dev/null
        remote_exec "$@"
        ;;
    exec-traefik-forward)
        validate_remote_identity >/dev/null
        remote_exec_traefik_forward "$@"
        ;;
    copy-to)
        (($# == 2)) || die "copy-to requires LOCAL and REMOTE"
        local_file="$1"
        remote_file="$2"
        [[ -f "$local_file" && ! -L "$local_file" ]] ||
            die "Local source must be a regular, non-symlink file"
        validate_remote_path "$remote_file"
        validate_remote_identity >/dev/null
        scp "${scp_options[@]}" -- "$local_file" "$user@$host:$remote_file"
        ;;
    copy-from)
        (($# == 2)) || die "copy-from requires REMOTE and LOCAL"
        remote_file="$1"
        local_file="$2"
        validate_remote_path "$remote_file"
        [[ ! -e "$local_file" ]] ||
            die "Local destination already exists: $local_file"
        [[ -d "$(dirname -- "$local_file")" ]] ||
            die "Local destination directory does not exist"
        validate_remote_identity >/dev/null
        scp "${scp_options[@]}" -- "$user@$host:$remote_file" "$local_file"
        ;;
    *)
        die "Unknown command '$command_name'"
        ;;
esac
