#!/usr/bin/env bash
set -Eeuo pipefail

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

timestamp_utc() {
    date -u +'%Y-%m-%dT%H:%M:%S.%3NZ'
}

usage() {
    cat <<'EOF'
Usage:
  runpod_loadgen.sh --identity FILE --identity-key FILE validate
  runpod_loadgen.sh --identity FILE --identity-key FILE exec COMMAND [ARG...]
  runpod_loadgen.sh --identity FILE --identity-key FILE exec-traefik-forward \
    --transport-output /artifacts/PATH.json -- COMMAND [ARG...]
  runpod_loadgen.sh --identity FILE --identity-key FILE copy-to LOCAL REMOTE
  runpod_loadgen.sh --identity FILE --identity-key FILE copy-from REMOTE LOCAL

The identity file is non-secret. The Ed25519 private key must be a regular
owner-readable file that is not group- or world-accessible.
EOF
}

IDENTITY_FILE=""
IDENTITY_KEY=""
TRANSPORT_FAILURE_EXIT=86
TRANSPORT_EVIDENCE_FAILURE_EXIT=87
FORWARD_TARGET_HOST="rke2-traefik.kube-system.svc.cluster.local"
FORWARD_TARGET_PORT=80
FORWARD_LISTEN_HOST="127.0.0.1"
FORWARD_PORTS=(18081 18082 18083 18084 18085 18086 18087 18088)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONTROLLER_TRANSPORT_SAMPLER="$SCRIPT_DIR/sample_ssh_transport.py"
RUNPOD_RESOURCE_SAMPLER_SOURCE="$SCRIPT_DIR/runpod-sample-resources.sh"
if [[ ! -f "$RUNPOD_RESOURCE_SAMPLER_SOURCE" ]]; then
    RUNPOD_RESOURCE_SAMPLER_SOURCE="$SCRIPT_DIR/../runpod/sample-resources.sh"
fi
[[ -f "$CONTROLLER_TRANSPORT_SAMPLER" &&
    ! -L "$CONTROLLER_TRANSPORT_SAMPLER" ]] ||
    die "controller SSH transport sampler is unavailable"
[[ -f "$RUNPOD_RESOURCE_SAMPLER_SOURCE" &&
    ! -L "$RUNPOD_RESOURCE_SAMPLER_SOURCE" ]] ||
    die "RunPod resource sampler source is unavailable"
controller_sampler_sha256="$(sha256sum -- "$CONTROLLER_TRANSPORT_SAMPLER" | awk '{print $1}')"
runpod_sampler_sha256="$(sha256sum -- "$RUNPOD_RESOURCE_SAMPLER_SOURCE" | awk '{print $1}')"
[[ "$controller_sampler_sha256" =~ ^[a-f0-9]{64}$ &&
    "$runpod_sampler_sha256" =~ ^[a-f0-9]{64}$ ]] ||
    die "transport sampler source fingerprint is malformed"
transport_temp=""
lane_state_temp=""
command_session_temp=""
command_lease_dir=""
command_lease_fd=""
command_pid=""
controller_sampler_pid=""
controller_sampler_runtime=""
controller_sampler_output=""
controller_sampler_ready=""
controller_sampler_stop=""
forward_pids=()

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
command -v setpriv >/dev/null || die "setpriv is required for child supervision"

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
helper_pid="$$"
parent_bound_exec() {
    # The supervision wrapper is expanded only by its child Bash.
    # shellcheck disable=SC2016
    setpriv --pdeathsig KILL bash -ceu '
        expected_parent="$1"
        shift
        [[ "$PPID" == "$expected_parent" ]] || exit 125
        exec "$@"
    ' bash "$helper_pid" "$@"
}

terminate_child() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    if kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.05
        done
        kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
}

monitor_pause() {
    if [[ "$command_lease_fd" =~ ^[0-9]+$ ]]; then
        sleep 0.1 {command_lease_fd}>&-
    else
        sleep 0.1
    fi
}

cleanup() {
    local pid
    # Closing the dedicated stdin lease is the cancellation request. Give the
    # remote phase wrapper time to terminate and reap its complete process
    # group before falling back to stopping only the local SSH client.
    if [[ "$command_lease_fd" =~ ^[0-9]+$ ]]; then
        exec {command_lease_fd}>&-
        command_lease_fd=""
    fi
    if [[ "$command_pid" =~ ^[0-9]+$ ]] && kill -0 "$command_pid" 2>/dev/null; then
        for _ in {1..350}; do
            kill -0 "$command_pid" 2>/dev/null || break
            sleep 0.1
        done
    fi
    terminate_child "$command_pid"
    if [[ -n "$controller_sampler_stop" ]]; then
        : > "$controller_sampler_stop" 2>/dev/null || true
    fi
    terminate_child "$controller_sampler_pid"
    for pid in "${forward_pids[@]}"; do
        terminate_child "$pid"
    done
    rm -f -- "$known_hosts"
    [[ -z "$transport_temp" ]] || rm -f -- "$transport_temp"
    [[ -z "$lane_state_temp" ]] || rm -f -- "$lane_state_temp"
    [[ -z "$command_session_temp" ]] || rm -f -- "$command_session_temp"
    [[ -z "$controller_sampler_runtime" ]] ||
        rm -rf -- "$controller_sampler_runtime"
    if [[ -n "$command_lease_dir" ]]; then
        rm -f -- "$command_lease_dir/stdin"
        rmdir -- "$command_lease_dir" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
printf '[%s]:%s %s\n' "$host" "$port" "$host_key" > "$known_hosts"
chmod 0600 "$known_hosts"

ssh_options=(
    -F /dev/null
    -i "$IDENTITY_KEY"
    -o BatchMode=yes
    -o ConnectTimeout=15
    -o ControlMaster=no
    -o ControlPath=none
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
    parent_bound_exec ssh \
        "${ssh_options[@]}" "$user@$host" "${quoted# }"
}

remote_exec_traefik_forward() {
    (($# > 1)) || die "Transport output and remote command are required"
    local transport_output="$1"
    shift
    validate_remote_path "$transport_output"

    local command_session_id
    command_session_id="$(head -c 64 /dev/urandom | sha256sum | cut -d ' ' -f 1)"
    [[ "$command_session_id" =~ ^[a-f0-9]{64}$ ]] ||
        die "Could not generate a unique command-session identity"
    local command_session_output="${transport_output}.command-session.${command_session_id}.json"
    local controller_telemetry_output="${transport_output%.json}.controller.ndjson"
    validate_remote_path "$command_session_output"
    validate_remote_path "$controller_telemetry_output"

    local resource_output=""
    local resource_output_count=0
    local previous_argument=""
    local current_argument
    for current_argument in "$@"; do
        if [[ "$previous_argument" == "--resource-output" ]]; then
            resource_output="$current_argument"
            ((resource_output_count += 1))
        fi
        previous_argument="$current_argument"
    done
    [[ "$resource_output_count" == 1 ]] ||
        die "the remote phase command must contain one --resource-output path"
    validate_remote_path "$resource_output"
    [[ "$resource_output" != "$transport_output" &&
        "$resource_output" != "$command_session_output" &&
        "$resource_output" != "$controller_telemetry_output" ]] ||
        die "transport telemetry paths must be distinct"

    # Outputs are immutable per invocation. Refuse an existing path before
    # opening any tunnel so a prior receipt can never be replayed as proof for
    # this command session.
    # shellcheck disable=SC2016
    remote_exec bash -ceu '
        [[ ! -e /tmp/bluemap-runpod-active-phase.lock &&
            ! -L /tmp/bluemap-runpod-active-phase.lock ]]
        for path do
            [[ ! -e "$path" && ! -L "$path" ]]
        done
    ' bash "$transport_output" "$command_session_output" \
        "$controller_telemetry_output" "$resource_output" ||
        die "Transport or command-session output already exists"

    local phase_timeout_seconds=""
    local phase_timeout_count=0
    local phase_argument
    for phase_argument in "$@"; do
        if [[ "$phase_argument" =~ ^BLUEMAP_PHASE_TIMEOUT_SECONDS=([1-9][0-9]*)$ ]]; then
            phase_timeout_seconds="${BASH_REMATCH[1]}"
            ((phase_timeout_count += 1))
        fi
    done
    [[ "$phase_timeout_count" == 1 && "$phase_timeout_seconds" -le 86400 ]] ||
        die "The remote phase command must contain one bounded BLUEMAP_PHASE_TIMEOUT_SECONDS assignment"

    local quoted=""
    local argument
    local -a session_command=(
        env
        "BLUEMAP_PHASE_SESSION_ID=$command_session_id"
        "BLUEMAP_PHASE_SESSION_OUTPUT=$command_session_output"
        "$@"
    )
    for argument in "${session_command[@]}"; do
        printf -v quoted '%s %q' "$quoted" "$argument"
    done

    local started_at finished_at failure=""
    local command_exit_status=""
    local command_terminated_for_lane_failure=false
    local command_started=false
    local command_lease_closed=false
    local command_lease_close_reason=""
    local command_session_confirmation_attempted=false
    local command_session_confirmed=false
    local command_session_receipt_json=null
    local transport_passed=false
    local controller_sampler_attempted=false
    local controller_sampler_ready_before_command=false
    local controller_sampler_ready_at=""
    local controller_sampler_reaped=false
    local controller_sampler_status=""
    local controller_telemetry_sha256=""
    local controller_telemetry_count=0
    local controller_telemetry_persisted=false
    local runpod_telemetry_sha256=""
    local runpod_telemetry_count=0
    local runpod_telemetry_source_sha256=""
    local runpod_telemetry_completed=false
    local lane_failure_index=""
    local command_deadline_expired=false
    local command_deadline_epoch=""
    local lane_count="${#FORWARD_PORTS[@]}"
    local i
    local -a start_attempted=()
    local -a started=()
    local -a lane_started_at=()
    local -a pre_attempted=()
    local -a pre_passed=()
    local -a pre_status=()
    local -a pre_at=()
    local -a post_attempted=()
    local -a post_passed=()
    local -a post_status=()
    local -a post_at=()
    local -a exited_early=()
    local -a exit_status=()
    local -a stopped_by_helper=()
    local -a lane_process_id=()
    local -a lane_process_start_ticks=()

    forward_pids=()
    for ((i = 0; i < lane_count; i++)); do
        forward_pids[i]=""
        start_attempted[i]=false
        started[i]=false
        lane_started_at[i]=""
        pre_attempted[i]=false
        pre_passed[i]=false
        pre_status[i]=""
        pre_at[i]=""
        post_attempted[i]=false
        post_passed[i]=false
        post_status[i]=""
        post_at[i]=""
        exited_early[i]=false
        exit_status[i]=""
        stopped_by_helper[i]=false
        lane_process_id[i]=""
        lane_process_start_ticks[i]=""
    done

    process_start_time_ticks() {
        local pid="$1"
        local stat tail
        local -a fields
        stat="$(<"/proc/$pid/stat")" || return 1
        tail="${stat##*) }"
        read -r -a fields <<<"$tail"
        [[ "${fields[19]:-}" =~ ^[1-9][0-9]*$ ]] || return 1
        printf '%s\n' "${fields[19]}"
    }

    stop_controller_sampler() {
        [[ "$controller_sampler_pid" =~ ^[1-9][0-9]*$ ]] || return 0
        : > "$controller_sampler_stop"
        for _ in {1..100}; do
            kill -0 "$controller_sampler_pid" 2>/dev/null || break
            sleep 0.05
        done
        if kill -0 "$controller_sampler_pid" 2>/dev/null; then
            terminate_child "$controller_sampler_pid"
            controller_sampler_status=143
            controller_sampler_pid=""
            controller_sampler_reaped=true
            return 1
        fi
        if wait "$controller_sampler_pid"; then
            controller_sampler_status=0
        else
            controller_sampler_status=$?
        fi
        controller_sampler_pid=""
        controller_sampler_reaped=true
        [[ "$controller_sampler_status" == 0 ]]
    }

    controller_sampler_ready_valid() {
        [[ -f "$controller_sampler_ready" &&
            ! -L "$controller_sampler_ready" &&
            -f "$controller_sampler_output" &&
            ! -L "$controller_sampler_output" ]] || return 1
        local ready_source sample_source
        ready_source="$(jq -er \
            --arg sourceSha256 "$controller_sampler_sha256" \
            '
            if (keys | sort) != ([
                "formatVersion", "kind", "sampleCount", "sourceSha256"
            ] | sort)
            or .formatVersion != 1
            or .kind != "controller-ssh-transport-ready"
            or .sampleCount != 1
            or .sourceSha256 != $sourceSha256
            then error("invalid controller telemetry readiness receipt")
            else .sourceSha256
            end
            ' "$controller_sampler_ready")" || return 1
        sample_source="$(head -n 1 -- "$controller_sampler_output" | jq -er \
            --arg sourceSha256 "$controller_sampler_sha256" \
            --arg remoteAddress "$host" \
            --argjson remotePort "$port" \
            '
            if .formatVersion != 2
            or .kind != "controller-ssh-transport-sample"
            or .sourceSha256 != $sourceSha256
            or (.lanes | length) != 8
            or (all(.lanes[];
                .socket.remote.address == $remoteAddress
                and .socket.remote.port == $remotePort) | not)
            then error("invalid first controller telemetry sample")
            else .sourceSha256
            end
            ')" || return 1
        [[ "$ready_source" == "$sample_source" ]]
    }

    started_at="$(timestamp_utc)"

    observe_lane_exit() {
        local index="$1"
        local pid="${forward_pids[$index]}"
        [[ "$pid" =~ ^[0-9]+$ ]] || return 0
        if wait "$pid"; then
            exit_status[index]=0
        else
            exit_status[index]=$?
        fi
        forward_pids[index]=""
        exited_early[index]=true
    }

    stop_lane() {
        local index="$1"
        local pid="${forward_pids[$index]}"
        [[ "$pid" =~ ^[0-9]+$ ]] || return 0
        if kill -0 "$pid" 2>/dev/null; then
            stopped_by_helper[index]=true
            kill -TERM "$pid" 2>/dev/null || true
            for _ in {1..20}; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.05
            done
            kill -KILL "$pid" 2>/dev/null || true
        else
            exited_early[index]=true
            [[ -n "$failure" ]] ||
                failure="lane-$((index + 1))-exited-before-helper-stop"
        fi
        if wait "$pid"; then
            exit_status[index]=0
        else
            exit_status[index]=$?
        fi
        forward_pids[index]=""
    }

    probe_lane() {
        local index="$1"
        local phase="$2"
        local attempts="$3"
        local status=""
        local attempt
        local probe_output
        probe_output="$(mktemp)"
        if [[ "$phase" == "pre" ]]; then
            pre_attempted[index]=true
        else
            post_attempted[index]=true
        fi
        for ((attempt = 1; attempt <= attempts; attempt++)); do
            if ! kill -0 "${forward_pids[$index]}" 2>/dev/null; then
                observe_lane_exit "$index"
                break
            fi
            : > "$probe_output"
            if remote_exec \
                curl --silent --show-error --connect-timeout 3 --max-time 10 \
                --output /dev/null --write-out '%{http_code}' \
                --header 'Host: bluemap-test.guenter.cloud' \
                "http://${FORWARD_LISTEN_HOST}:${FORWARD_PORTS[$index]}/" \
                >"$probe_output" 2>/dev/null; then
                status="$(<"$probe_output")"
            else
                status=""
            fi
            [[ "$status" =~ ^[1-5][0-9][0-9]$ ]] || status=""
            if [[ "$status" == "200" ]]; then
                break
            fi
            ((attempt == attempts)) || sleep 0.25
        done
        if [[ "$phase" == "pre" ]]; then
            pre_at[index]="$(timestamp_utc)"
            pre_status[index]="$status"
            [[ "$status" == "200" ]] && pre_passed[index]=true
        else
            post_at[index]="$(timestamp_utc)"
            post_status[index]="$status"
            [[ "$status" == "200" ]] && post_passed[index]=true
        fi
        rm -f -- "$probe_output"
        [[ "$status" == "200" ]]
    }

    close_command_lease() {
        local reason="$1"
        if [[ "$command_lease_fd" =~ ^[0-9]+$ ]]; then
            exec {command_lease_fd}>&-
            command_lease_fd=""
        fi
        command_lease_closed=true
        command_lease_close_reason="$reason"
    }

    await_command_exit() {
        local observed_exit=false
        local state=""
        local _pid="${command_pid:-}"
        [[ "$_pid" =~ ^[0-9]+$ ]] || return 1
        for _ in {1..350}; do
            if [[ ! -r "/proc/$_pid/stat" ]]; then
                observed_exit=true
                break
            fi
            read -r _ _ state _ < "/proc/$_pid/stat" || true
            if [[ "$state" == Z* ]]; then
                observed_exit=true
                break
            fi
            monitor_pause
        done
        [[ "$observed_exit" == true ]] || return 1
        if wait "$_pid"; then
            command_exit_status=0
        else
            command_exit_status=$?
        fi
        command_pid=""
        return 0
    }

    confirm_command_session() {
        command_session_confirmation_attempted=true
        command_session_temp="$(mktemp)"
        # The script is intentionally expanded only by Bash on RunPod.
        # shellcheck disable=SC2016
        if ! remote_exec \
            bash -ceu '
                path="$1"
                session_id="$2"
                for _ in $(seq 1 350); do
                    if [[ -s "$path" &&
                        ! -e /tmp/bluemap-runpod-active-phase.lock &&
                        ! -L /tmp/bluemap-runpod-active-phase.lock ]]; then
                        break
                    fi
                    sleep 0.1
                done
                [[ -s "$path" && ! -L "$path" ]]
                [[ ! -e /tmp/bluemap-runpod-active-phase.lock &&
                    ! -L /tmp/bluemap-runpod-active-phase.lock ]]
                pgid="$(jq -er ".termination.processGroupId" "$path")"
                [[ "$pgid" =~ ^[1-9][0-9]*$ ]]
                if kill -0 -- "-$pgid" 2>/dev/null; then
                    exit 1
                fi
                jq -e --arg sessionId "$session_id" \
                    ".sessionId == \$sessionId and .termination.processGroupEmpty == true" \
                    "$path" >/dev/null
                cat -- "$path"
            ' bash "$command_session_output" "$command_session_id" \
            > "$command_session_temp"; then
            return 1
        fi
        if ! jq -e \
            --arg sessionId "$command_session_id" \
            --arg sessionOutput "$command_session_output" \
            --arg resourceOutput "$resource_output" \
            --arg helperStatus "$command_exit_status" \
            '
            def exact_keys($expected):
                (keys | sort) == ($expected | sort);
            def nullable_int: if . == "" then null else tonumber end;
            exact_keys([
                "kind", "formatVersion", "sessionId", "sessionOutput",
                "activeLock", "startedAt", "completedAt", "telemetry", "lease",
                "termination", "passed"
            ])
            and .kind == "runpod-command-session"
            and .formatVersion == 2
            and .sessionId == $sessionId
            and .sessionOutput == $sessionOutput
            and .activeLock == "/tmp/bluemap-runpod-active-phase.lock"
            and ((.startedAt | type) == "string" and (.startedAt | length) > 0)
            and ((.completedAt | type) == "string" and (.completedAt | length) > 0)
            and .startedAt <= .completedAt
            and (.telemetry | exact_keys([
                "resourceOutput", "readyBeforeWorkload", "readyAt",
                "workloadReleasedAt", "samplerExitStatus"
            ]))
            and .telemetry.resourceOutput == $resourceOutput
            and .telemetry.readyBeforeWorkload == true
            and ((.telemetry.readyAt | type) == "string"
                and (.telemetry.readyAt | length) > 0)
            and ((.telemetry.workloadReleasedAt | type) == "string"
                and (.telemetry.workloadReleasedAt | length) > 0)
            and .telemetry.readyAt <= .telemetry.workloadReleasedAt
            and .telemetry.samplerExitStatus == 0
            and (.lease | exact_keys([
                "required", "eofObserved", "protocolViolation", "observedAt"
            ]))
            and .lease.required == true
            and (.lease.eofObserved | type) == "boolean"
            and .lease.protocolViolation == false
            and (
                if .lease.eofObserved
                then ((.lease.observedAt | type) == "string"
                    and (.lease.observedAt | length) > 0)
                else .lease.observedAt == null
                end
            )
            and (.termination | exact_keys([
                "requested", "termSignal", "killEscalated",
                "commandExitStatus", "processGroupId", "processGroupEmpty",
                "watcherReaped", "samplerReaped"
            ]))
            and (.termination.requested | type) == "boolean"
            and .termination.termSignal == (
                if .termination.requested then "TERM" else null end
            )
            and (.termination.killEscalated | type) == "boolean"
            and (if .termination.killEscalated
                then .termination.requested else true end)
            and ((.termination.commandExitStatus | type) == "number"
                and .termination.commandExitStatus
                    == (.termination.commandExitStatus | floor)
                and .termination.commandExitStatus >= 0
                and .termination.commandExitStatus <= 255)
            and ((.termination.processGroupId | type) == "number"
                and .termination.processGroupId
                    == (.termination.processGroupId | floor)
                and .termination.processGroupId > 0)
            and .termination.processGroupEmpty == true
            and .termination.watcherReaped == true
            and .termination.samplerReaped == true
            and .passed == true
            and (if .lease.eofObserved
                then .termination.requested == true else true end)
            and (
                ($helperStatus | nullable_int) == null
                or .lease.eofObserved
                or .termination.commandExitStatus
                    == ($helperStatus | nullable_int)
            )
            ' "$command_session_temp" >/dev/null; then
            return 1
        fi
        command_session_receipt_json="$(jq -c . "$command_session_temp")"
        command_session_confirmed=true
        if jq -e '.lease.eofObserved == true' "$command_session_temp" >/dev/null &&
            [[ -z "$failure" ]]; then
            failure="command-session-disconnected"
        elif jq -e '.termination.requested == true' \
            "$command_session_temp" >/dev/null && [[ -z "$failure" ]]; then
            failure="command-session-termination-requested"
        fi
        rm -f -- "$command_session_temp"
        command_session_temp=""
        return 0
    }

    # Each lane is a separate SSH transport. There is deliberately no SSH
    # multiplexing, dynamic lane count, caller-selected port, or fallback.
    for ((i = 0; i < lane_count; i++)); do
        start_attempted[i]=true
        # The supervision wrapper is expanded only by its child Bash.
        # shellcheck disable=SC2016
        setpriv --pdeathsig KILL bash -ceu '
            expected_parent="$1"
            shift
            [[ "$PPID" == "$expected_parent" ]] || exit 125
            exec "$@"
        ' bash "$helper_pid" \
            ssh \
                "${ssh_options[@]}" \
                -o ExitOnForwardFailure=yes \
                -N -T \
                -R "${FORWARD_LISTEN_HOST}:${FORWARD_PORTS[$i]}:${FORWARD_TARGET_HOST}:${FORWARD_TARGET_PORT}" \
                "$user@$host" &
        forward_pids[i]=$!
        if kill -0 "${forward_pids[$i]}" 2>/dev/null; then
            started[i]=true
            lane_started_at[i]="$(timestamp_utc)"
            lane_process_id[i]="${forward_pids[$i]}"
            if ! lane_process_start_ticks[i]="$(
                process_start_time_ticks "${forward_pids[$i]}"
            )"; then
                failure="lane-$((i + 1))-process-identity-unavailable"
                break
            fi
        else
            observe_lane_exit "$i"
            failure="lane-$((i + 1))-start-failed"
            break
        fi
        if ! probe_lane "$i" pre 20; then
            failure="lane-$((i + 1))-pre-probe-failed"
            break
        fi
    done

    if [[ -z "$failure" ]]; then
        controller_sampler_runtime="$(mktemp -d)"
        chmod 0700 "$controller_sampler_runtime"
        controller_sampler_output="$controller_sampler_runtime/controller.ndjson"
        controller_sampler_ready="$controller_sampler_runtime/ready"
        controller_sampler_stop="$controller_sampler_runtime/stop"
        controller_sampler_attempted=true
        sampler_arguments=(
            "$CONTROLLER_TRANSPORT_SAMPLER"
            --output "$controller_sampler_output"
            --ready-file "$controller_sampler_ready"
            --stop-file "$controller_sampler_stop"
            --source-sha256 "$controller_sampler_sha256"
            --expected-remote-address "$host"
            --expected-remote-port "$port"
            --interval-seconds 1
        )
        for ((i = 0; i < lane_count; i++)); do
            sampler_arguments+=(
                --lane "lane-$((i + 1))=${forward_pids[$i]}"
            )
        done
        # A backgrounded shell function introduces an intermediate Bash PID,
        # which would invalidate the explicit parent identity. Launch the
        # sampler with the same direct parent-bound wrapper as every SSH lane.
        # shellcheck disable=SC2016
        setpriv --pdeathsig KILL bash -ceu '
            expected_parent="$1"
            shift
            [[ "$PPID" == "$expected_parent" ]] || exit 125
            exec "$@"
        ' bash "$helper_pid" python3 "${sampler_arguments[@]}" &
        controller_sampler_pid=$!
        for _ in {1..100}; do
            controller_sampler_ready_valid && break
            kill -0 "$controller_sampler_pid" 2>/dev/null || break
            sleep 0.05
        done
        if controller_sampler_ready_valid; then
            controller_sampler_ready_before_command=true
            controller_sampler_ready_at="$(timestamp_utc)"
        else
            if wait "$controller_sampler_pid"; then
                controller_sampler_status=0
            else
                controller_sampler_status=$?
            fi
            controller_sampler_pid=""
            controller_sampler_reaped=true
            failure="controller-telemetry-not-ready-before-command"
        fi
    fi

    if [[ -z "$failure" ]]; then
        # The helper itself owns the only write-capable descriptor for this
        # FIFO. The command SSH child explicitly closes that inherited
        # descriptor and opens stdin read-only. Therefore helper death closes
        # the lease even after SIGKILL and forces EOF on the remote wrapper.
        command_lease_dir="$(mktemp -d)"
        local command_lease_fifo="$command_lease_dir/stdin"
        mkfifo -m 0600 -- "$command_lease_fifo"
        exec {command_lease_fd}<>"$command_lease_fifo"
        # shellcheck disable=SC2029
        # The supervision wrapper is expanded only by its child Bash.
        # shellcheck disable=SC2016
        setpriv --pdeathsig KILL bash -ceu '
            expected_parent="$1"
            shift
            [[ "$PPID" == "$expected_parent" ]] || exit 125
            exec "$@"
        ' bash "$helper_pid" \
            ssh "${ssh_options[@]}" "$user@$host" "${quoted# }" \
            < "$command_lease_fifo" {command_lease_fd}>&- &
        command_pid=$!
        command_started=true
        if ! printf 'bluemap-phase-lease-v1:%s\n' "$command_session_id" \
            >&"$command_lease_fd"; then
            die "Could not send the command lease handshake"
        fi
        command_deadline_epoch="$((EPOCHSECONDS + phase_timeout_seconds + 60))"

        while kill -0 "$command_pid" 2>/dev/null; do
            lane_failure_index=""
            if ((EPOCHSECONDS >= command_deadline_epoch)); then
                command_deadline_expired=true
                failure="command-session-helper-deadline"
                close_command_lease "helper-deadline"
                break
            fi
            if ! kill -0 "$controller_sampler_pid" 2>/dev/null; then
                failure="controller-telemetry-exited-during-command"
                close_command_lease "transport-telemetry-failure"
                break
            fi
            for ((i = 0; i < lane_count; i++)); do
                if ! kill -0 "${forward_pids[$i]}" 2>/dev/null; then
                    lane_failure_index="$i"
                    break
                fi
            done
            [[ -z "$lane_failure_index" ]] || break
            monitor_pause
        done

        if [[ "$command_deadline_expired" == true ]]; then
            if ! await_command_exit; then
                failure="command-session-helper-deadline-termination-unconfirmed"
            fi
        elif [[ "$failure" == "controller-telemetry-exited-during-command" ]]; then
            if ! await_command_exit; then
                failure="controller-telemetry-remote-termination-unconfirmed"
                terminate_child "$command_pid"
                command_pid=""
            fi
        elif [[ -n "$lane_failure_index" ]]; then
            observe_lane_exit "$lane_failure_index"
            failure="lane-$((lane_failure_index + 1))-exited-during-command"
            command_terminated_for_lane_failure=true
            close_command_lease "lane-failure"
            if ! await_command_exit; then
                failure="lane-$((lane_failure_index + 1))-remote-termination-unconfirmed"
                terminate_child "$command_pid"
                command_pid=""
            fi
        else
            if ! await_command_exit; then
                failure="command-session-local-exit-unconfirmed"
                close_command_lease "local-exit-timeout"
            else
                close_command_lease "after-command-exit"
            fi
        fi

        # Bound the controller-side capture to the workload command itself.
        # Independent receipt confirmation and eight post-probes can take
        # several seconds; including them would move the final sample away
        # from the workload edge and pollute diagnostic TCP deltas with
        # control-plane SSH connections.
        if [[ "$controller_sampler_pid" =~ ^[1-9][0-9]*$ ]]; then
            if ! stop_controller_sampler && [[ -z "$failure" ]]; then
                failure="controller-telemetry-sampler-failed"
            fi
        fi

        # Confirmation is an independent SSH query and is attempted even when
        # the command channel itself cannot be reaped. Only the signed-by-path,
        # nonce-bound receipt plus an empty process group can establish safety.
        if ! confirm_command_session; then
            [[ -n "$failure" ]] || failure="command-session-unconfirmed"
        fi
        terminate_child "$command_pid"
        command_pid=""
        rm -f -- "$command_lease_fifo"
        rmdir -- "$command_lease_dir"
        command_lease_dir=""

        if [[ -z "$failure" ]]; then
            for ((i = 0; i < lane_count; i++)); do
                if ! kill -0 "${forward_pids[$i]}" 2>/dev/null; then
                    observe_lane_exit "$i"
                    failure="lane-$((i + 1))-exited-before-post-probe"
                fi
            done
            if [[ -z "$failure" ]]; then
                for ((i = 0; i < lane_count; i++)); do
                    if ! probe_lane "$i" post 1; then
                        failure="lane-$((i + 1))-post-probe-failed"
                    fi
                done
            fi
        fi
    fi

    for ((i = 0; i < lane_count; i++)); do
        stop_lane "$i"
    done

    if [[ "$controller_sampler_attempted" == true &&
        "$controller_sampler_ready_before_command" == true &&
        "$controller_sampler_reaped" == true &&
        "$controller_sampler_status" == 0 &&
        -s "$controller_sampler_output" ]]; then
        controller_telemetry_sha256="$(
            sha256sum -- "$controller_sampler_output" | awk '{print $1}'
        )"
        controller_telemetry_count="$(wc -l < "$controller_sampler_output")"
        local controller_remote_temp="${controller_telemetry_output}.tmp.${run_id}.$$"
        if [[ "$controller_telemetry_sha256" =~ ^[a-f0-9]{64}$ &&
            "$controller_telemetry_count" =~ ^([2-9]|[1-9][0-9]+)$ ]] &&
            parent_bound_exec scp \
                "${scp_options[@]}" -- "$controller_sampler_output" \
                "$user@$host:$controller_remote_temp" &&
            remote_exec chmod 0600 -- "$controller_remote_temp" &&
            remote_exec mv -- "$controller_remote_temp" \
                "$controller_telemetry_output"; then
            controller_telemetry_persisted=true
        else
            remote_exec rm -f -- "$controller_remote_temp" >/dev/null 2>&1 || true
            [[ -n "$failure" ]] || failure="controller-telemetry-persist-failed"
        fi
    elif [[ "$controller_sampler_attempted" == true && -z "$failure" ]]; then
        failure="controller-telemetry-incomplete"
    fi

    if [[ "$command_started" == true &&
        "$command_session_confirmed" == true ]]; then
        local runpod_metadata
        # The command receipt proves the sampler was valid before workload
        # release and reaped. Re-read the immutable output independently.
        # shellcheck disable=SC2016
        if runpod_metadata="$(remote_exec bash -ceu '
            path="$1"
            [[ -f "$path" && ! -L "$path" && -s "$path" ]]
            sha="$(sha256sum -- "$path" | awk "{print \$1}")"
            count="$(wc -l < "$path")"
            source="$(head -n 1 -- "$path" | jq -er .sourceSha256)"
            [[ "$sha" =~ ^[a-f0-9]{64}$ &&
                "$count" =~ ^([2-9]|[1-9][0-9]+)$ &&
                "$source" =~ ^[a-f0-9]{64}$ ]]
            jq -nc --arg sha256 "$sha" --argjson count "$count" \
                --arg sourceSha256 "$source" \
                "{sha256:\$sha256,count:\$count,sourceSha256:\$sourceSha256}"
        ' bash "$resource_output")"; then
            runpod_telemetry_sha256="$(jq -r .sha256 <<<"$runpod_metadata")"
            runpod_telemetry_count="$(jq -r .count <<<"$runpod_metadata")"
            runpod_telemetry_source_sha256="$(
                jq -r .sourceSha256 <<<"$runpod_metadata"
            )"
            if jq -e \
                --arg output "$resource_output" \
                '
                .formatVersion == 2
                and .telemetry.resourceOutput == $output
                and .telemetry.readyBeforeWorkload == true
                and (.telemetry.readyAt | type) == "string"
                and (.telemetry.workloadReleasedAt | type) == "string"
                and .telemetry.readyAt <= .telemetry.workloadReleasedAt
                and .telemetry.samplerExitStatus == 0
                and .termination.samplerReaped == true
                ' <<<"$command_session_receipt_json" >/dev/null 2>&1; then
                runpod_telemetry_completed=true
            else
                [[ -n "$failure" ]] ||
                    failure="runpod-telemetry-receipt-invalid"
            fi
        else
            [[ -n "$failure" ]] || failure="runpod-telemetry-missing"
        fi
    fi

    if [[ "$command_started" == true &&
        ( "$controller_telemetry_persisted" != true ||
          "$runpod_telemetry_completed" != true ) &&
        -z "$failure" ]]; then
        failure="transport-telemetry-incomplete"
    fi
    [[ -n "$failure" ]] || transport_passed=true
    finished_at="$(timestamp_utc)"

    lane_state_temp="$(mktemp)"
    for ((i = 0; i < lane_count; i++)); do
        jq -cn \
            --arg id "lane-$((i + 1))" \
            --argjson listenPort "${FORWARD_PORTS[i]}" \
            --argjson startAttempted "${start_attempted[i]}" \
            --argjson started "${started[i]}" \
            --arg startedAt "${lane_started_at[i]}" \
            --argjson preAttempted "${pre_attempted[i]}" \
            --argjson prePassed "${pre_passed[i]}" \
            --arg preStatus "${pre_status[i]}" \
            --arg preAt "${pre_at[i]}" \
            --argjson postAttempted "${post_attempted[i]}" \
            --argjson postPassed "${post_passed[i]}" \
            --arg postStatus "${post_status[i]}" \
            --arg postAt "${post_at[i]}" \
            --argjson exitedEarly "${exited_early[i]}" \
            --arg exitStatus "${exit_status[i]}" \
            --argjson stoppedByHelper "${stopped_by_helper[i]}" \
            --arg processId "${lane_process_id[i]}" \
            --arg processStartTimeTicks "${lane_process_start_ticks[i]}" \
            '
            def nullable: if . == "" then null else . end;
            def nullable_int: if . == "" then null else tonumber end;
            {
                id: $id,
                listenPort: $listenPort,
                startAttempted: $startAttempted,
                started: $started,
                startedAt: ($startedAt | nullable),
                process: {
                    pid: ($processId | nullable_int),
                    startTimeTicks: ($processStartTimeTicks | nullable_int)
                },
                preProbe: {
                    attempted: $preAttempted,
                    passed: $prePassed,
                    httpStatus: ($preStatus | nullable_int),
                    at: ($preAt | nullable)
                },
                postProbe: {
                    attempted: $postAttempted,
                    passed: $postPassed,
                    httpStatus: ($postStatus | nullable_int),
                    at: ($postAt | nullable)
                },
                exitedEarly: $exitedEarly,
                exitStatus: ($exitStatus | nullable_int),
                stoppedByHelper: $stoppedByHelper
            }
            ' >>"$lane_state_temp"
    done

    transport_temp="$(mktemp)"
    jq -s \
        --arg startedAt "$started_at" \
        --arg finishedAt "$finished_at" \
        --arg commandExitStatus "$command_exit_status" \
        --argjson commandTerminated "$command_terminated_for_lane_failure" \
        --argjson commandStarted "$command_started" \
        --arg commandSessionId "$command_session_id" \
        --arg commandSessionOutput "$command_session_output" \
        --argjson commandLeaseClosed "$command_lease_closed" \
        --arg commandLeaseCloseReason "$command_lease_close_reason" \
        --argjson commandSessionConfirmationAttempted \
            "$command_session_confirmation_attempted" \
        --argjson commandSessionConfirmed "$command_session_confirmed" \
        --argjson commandSessionReceipt "$command_session_receipt_json" \
        --arg controllerTelemetryOutput "$controller_telemetry_output" \
        --arg controllerTelemetrySha256 "$controller_telemetry_sha256" \
        --argjson controllerTelemetryCount "$controller_telemetry_count" \
        --argjson controllerSamplerAttempted "$controller_sampler_attempted" \
        --argjson controllerSamplerReadyBeforeCommand \
            "$controller_sampler_ready_before_command" \
        --arg controllerSamplerReadyAt "$controller_sampler_ready_at" \
        --argjson controllerSamplerReaped "$controller_sampler_reaped" \
        --arg controllerSamplerStatus "$controller_sampler_status" \
        --argjson controllerTelemetryPersisted \
            "$controller_telemetry_persisted" \
        --arg controllerSamplerSha256 "$controller_sampler_sha256" \
        --arg controllerRemoteAddress "$host" \
        --argjson controllerRemotePort "$port" \
        --arg runpodTelemetryOutput "$resource_output" \
        --arg runpodTelemetrySha256 "$runpod_telemetry_sha256" \
        --argjson runpodTelemetryCount "$runpod_telemetry_count" \
        --arg runpodTelemetrySourceSha256 \
            "$runpod_telemetry_source_sha256" \
        --argjson runpodTelemetryCompleted "$runpod_telemetry_completed" \
        --arg runpodSamplerSha256 "$runpod_sampler_sha256" \
        --arg runpodImageDigest "$expected_image_digest" \
        --arg failure "$failure" \
        --argjson passed "$transport_passed" \
        --argjson tunnelCount "$lane_count" \
        '
        def nullable: if . == "" then null else . end;
        def nullable_int: if . == "" then null else tonumber end;
        . as $lanes
        | if ($lanes | length) != $tunnelCount
        then error("lane-state count differs from the fixed tunnel count")
        else
            {
                kind: "ssh-l4-traefik-transport",
                formatVersion: 2,
                mode: "ssh-l4-traefik",
                startedAt: $startedAt,
                finishedAt: $finishedAt,
                topology: {
                    formatVersion: 1,
                    balancer: "haproxy-tcp-static-rr",
                    frontend: {host: "127.0.0.1", port: 18080},
                    tunnelCount: $tunnelCount,
                    backends: ($lanes | map({
                        id,
                        listenHost: "127.0.0.1",
                        listenPort,
                        targetHost: "rke2-traefik.kube-system.svc.cluster.local",
                        targetPort: 80
                    })),
                    healthPolicy: "all-required"
                },
                allRequired: true,
                commandExitStatus: ($commandExitStatus | nullable_int),
                commandTerminatedForLaneFailure: $commandTerminated,
                commandSession: {
                    required: $commandStarted,
                    id: $commandSessionId,
                    outputPath: $commandSessionOutput,
                    leaseClosedByHelper: $commandLeaseClosed,
                    leaseCloseReason: ($commandLeaseCloseReason | nullable),
                    confirmationAttempted: $commandSessionConfirmationAttempted,
                    confirmed: $commandSessionConfirmed,
                    receipt: $commandSessionReceipt
                },
                telemetry: {
                    formatVersion: 2,
                    required: true,
                    intervalSeconds: 1,
                    controller: {
                        path: $controllerTelemetryOutput,
                        sha256: ($controllerTelemetrySha256 | nullable),
                        sampleCount: $controllerTelemetryCount,
                        source: {
                            kind: "controller-procfs-ssh-lanes-v2",
                            samplerSha256: $controllerSamplerSha256,
                            remoteAddress: $controllerRemoteAddress,
                            remotePort: $controllerRemotePort
                        },
                        capture: {
                            attempted: $controllerSamplerAttempted,
                            validBeforeWorkload: $controllerSamplerReadyBeforeCommand,
                            readyAt: ($controllerSamplerReadyAt | nullable),
                            reaped: $controllerSamplerReaped,
                            exitStatus: ($controllerSamplerStatus | nullable_int),
                            persisted: $controllerTelemetryPersisted
                        }
                    },
                    runpod: {
                        path: $runpodTelemetryOutput,
                        sha256: ($runpodTelemetrySha256 | nullable),
                        sampleCount: $runpodTelemetryCount,
                        source: {
                            kind: "runpod-haproxy-procfs-v1",
                            imageDigest: $runpodImageDigest,
                            samplerSha256: $runpodSamplerSha256,
                            statsSocket: "/run/haproxy/bluemap-stats.sock"
                        },
                        capture: {
                            attempted: $commandStarted,
                            validBeforeWorkload: (
                                $commandSessionReceipt.telemetry.readyBeforeWorkload
                                // false
                            ),
                            readyAt: (
                                $commandSessionReceipt.telemetry.readyAt // null
                            ),
                            workloadReleasedAt: (
                                $commandSessionReceipt.telemetry.workloadReleasedAt
                                // null
                            ),
                            reaped: (
                                $commandSessionReceipt.termination.samplerReaped
                                // false
                            ),
                            exitStatus: (
                                $commandSessionReceipt.telemetry.samplerExitStatus
                                // null
                            ),
                            persisted: $runpodTelemetryCompleted,
                            observedSourceSha256: (
                                $runpodTelemetrySourceSha256 | nullable
                            )
                        }
                    }
                },
                lanes: $lanes,
                failure: ($failure | nullable),
                passed: $passed
            }
        end
        ' "$lane_state_temp" >"$transport_temp"
    rm -f -- "$lane_state_temp"
    lane_state_temp=""

    local remote_dir="${transport_output%/*}"
    local remote_temp="${transport_output}.tmp.${run_id}.$$"
    if ! remote_exec mkdir -p -- "$remote_dir" ||
        ! parent_bound_exec scp \
            "${scp_options[@]}" -- "$transport_temp" "$user@$host:$remote_temp" ||
        ! remote_exec chmod 0600 -- "$remote_temp" ||
        ! remote_exec mv -- "$remote_temp" "$transport_output"; then
        remote_exec rm -f -- "$remote_temp" >/dev/null 2>&1 || true
        printf 'ERROR: Could not persist transport evidence to %s\n' \
            "$transport_output" >&2
        return "$TRANSPORT_EVIDENCE_FAILURE_EXIT"
    fi

    rm -f -- "$transport_temp"
    transport_temp=""
    if [[ "$transport_passed" != true ]]; then
        return "$TRANSPORT_FAILURE_EXIT"
    fi
    return "$command_exit_status"
}

validate_remote_identity() {
    local actual actual_file
    actual_file="$(mktemp)"
    if ! remote_exec bluemap-runpod-identity >"$actual_file"; then
        rm -f -- "$actual_file"
        die "Could not query the RunPod load-generator identity"
    fi
    actual="$(<"$actual_file")"
    rm -f -- "$actual_file"
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
        (($# >= 4)) ||
            die "exec-traefik-forward requires --transport-output PATH -- COMMAND"
        [[ "$1" == "--transport-output" ]] ||
            die "exec-traefik-forward requires --transport-output as its first option"
        transport_output="$2"
        shift 2
        [[ "$1" == "--" ]] ||
            die "exec-traefik-forward requires -- before COMMAND"
        shift
        validate_remote_path "$transport_output"
        validate_remote_identity >/dev/null
        remote_exec_traefik_forward "$transport_output" "$@"
        ;;
    copy-to)
        (($# == 2)) || die "copy-to requires LOCAL and REMOTE"
        local_file="$1"
        remote_file="$2"
        [[ -f "$local_file" && ! -L "$local_file" ]] ||
            die "Local source must be a regular, non-symlink file"
        validate_remote_path "$remote_file"
        validate_remote_identity >/dev/null
        parent_bound_exec scp \
            "${scp_options[@]}" -- "$local_file" "$user@$host:$remote_file"
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
        parent_bound_exec scp \
            "${scp_options[@]}" -- "$user@$host:$remote_file" "$local_file"
        ;;
    *)
        die "Unknown command '$command_name'"
        ;;
esac
