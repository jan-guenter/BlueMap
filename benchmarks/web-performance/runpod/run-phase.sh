#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

timestamp() {
    date -u +'%Y-%m-%dT%H:%M:%S.%3NZ'
}

[[ "${BLUEMAP_PHASE_TIMEOUT_SECONDS:-}" =~ ^[1-9][0-9]*$ ]] ||
    fail "BLUEMAP_PHASE_TIMEOUT_SECONDS must be a positive integer"
[[ "${BLUEMAP_PHASE_SESSION_ID:-}" =~ ^[a-f0-9]{64}$ ]] ||
    fail "BLUEMAP_PHASE_SESSION_ID must be a lowercase SHA-256 value"
[[ "${BLUEMAP_PHASE_SESSION_OUTPUT:-}" =~ ^/artifacts/[A-Za-z0-9._/-]+$ ]] ||
    fail "BLUEMAP_PHASE_SESSION_OUTPUT must be below /artifacts"
[[ "$BLUEMAP_PHASE_SESSION_OUTPUT" != *"/../"* &&
    "$BLUEMAP_PHASE_SESSION_OUTPUT" != */.. &&
    "$BLUEMAP_PHASE_SESSION_OUTPUT" != *"//"* ]] ||
    fail "BLUEMAP_PHASE_SESSION_OUTPUT contains an unsafe segment"
[[ "${1:-}" == "--resource-output" && -n "${2:-}" ]] || {
    printf 'Usage: bluemap-runpod-run-phase --resource-output FILE -- COMMAND [ARG...]\n' >&2
    exit 1
}

resource_output="$2"
shift 2
[[ "${1:-}" == "--" ]] || fail "missing command separator"
shift
(($# > 0)) || fail "phase command is empty"
[[ "$resource_output" =~ ^/artifacts/[A-Za-z0-9._/-]+$ ]] ||
    fail "resource output must be below /artifacts"
[[ "$resource_output" != *"/../"* &&
    "$resource_output" != */.. &&
    "$resource_output" != *"//"* ]] ||
    fail "resource output contains an unsafe segment"
[[ "$BLUEMAP_PHASE_SESSION_OUTPUT" != "$resource_output" ]] ||
    fail "session and resource outputs must differ"
[[ ! -e "$BLUEMAP_PHASE_SESSION_OUTPUT" &&
    ! -L "$BLUEMAP_PHASE_SESSION_OUTPUT" ]] ||
    fail "phase session output already exists"
command -v setsid >/dev/null || fail "setsid is unavailable"

readonly ACTIVE_PHASE_LOCK=/tmp/bluemap-runpod-active-phase.lock
session_id="$BLUEMAP_PHASE_SESSION_ID"
session_output="$BLUEMAP_PHASE_SESSION_OUTPUT"
session_temp="${session_output}.tmp.${session_id}.$$"
runtime_dir=""
stop_file="${resource_output}.stop"
sampler_pid=""
command_pid=""
command_pgid=""
watcher_pid=""
watcher_exit_status=""
watcher_exit_valid=false
lease_input_fd=""
command_status=""
started_at="$(timestamp)"
completed_at=""
lease_eof_observed=false
lease_protocol_violation=false
lease_observed_at=""
termination_requested=false
kill_escalated=false
process_group_empty=false
watcher_reaped=false
sampler_reaped=false
receipt_written=false
signal_status=""

process_group_alive() {
    [[ "$command_pgid" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -0 -- "-$command_pgid" 2>/dev/null
}

request_termination() {
    [[ "$command_pgid" =~ ^[1-9][0-9]*$ ]] || return 0
    termination_requested=true
    [[ -z "$runtime_dir" || ! -d "$runtime_dir" ]] ||
        : > "$runtime_dir/termination-requested"
    kill -TERM -- "-$command_pgid" 2>/dev/null || true
    for _ in {1..100}; do
        reap_command_if_exited || true
        process_group_alive || return 0
        sleep 0.1
    done
    kill_escalated=true
    [[ ! -d "$runtime_dir" ]] || : > "$runtime_dir/kill-escalated"
    kill -KILL -- "-$command_pgid" 2>/dev/null || true
    for _ in {1..100}; do
        reap_command_if_exited || true
        process_group_alive || return 0
        sleep 0.1
    done
}

# shellcheck disable=SC2329
reap_command_if_exited() {
    local state=""
    [[ "$command_pid" =~ ^[1-9][0-9]*$ ]] || return 0
    if [[ -r "/proc/$command_pid/stat" ]]; then
        read -r _ _ state _ < "/proc/$command_pid/stat" || true
        [[ "$state" == Z* ]] || return 1
    fi
    if wait "$command_pid" 2>/dev/null; then
        command_status=0
    else
        command_status=$?
    fi
    command_pid=""
}

# shellcheck disable=SC2329
handle_lease_signal() {
    termination_requested=true
    [[ -z "$runtime_dir" || ! -d "$runtime_dir" ]] ||
        : > "$runtime_dir/termination-requested"
    [[ ! "$command_pgid" =~ ^[1-9][0-9]*$ ]] ||
        kill -TERM -- "-$command_pgid" 2>/dev/null || true
}

# shellcheck disable=SC2329
handle_signal() {
    local status="$1"
    signal_status="$status"
    handle_lease_signal
    exit "$status"
}

watcher_process_alive() {
    local state=""
    [[ "$watcher_pid" =~ ^[1-9][0-9]*$ ]] || return 1
    [[ -r "/proc/$watcher_pid/stat" ]] || return 1
    read -r _ _ state _ < "/proc/$watcher_pid/stat" || return 1
    [[ "$state" != Z* ]]
}

reap_watcher() {
    [[ "$watcher_pid" =~ ^[1-9][0-9]*$ ]] || return 0
    if wait "$watcher_pid" 2>/dev/null; then
        watcher_exit_status=0
    else
        watcher_exit_status=$?
    fi
    watcher_pid=""
    watcher_reaped=true
}

stop_watcher() {
    [[ "$watcher_pid" =~ ^[1-9][0-9]*$ ]] || return 0
    if ! watcher_process_alive; then
        reap_watcher
        printf 'ERROR: command lease watcher exited unexpectedly with status %s\n' \
            "$watcher_exit_status" >&2
        return 1
    fi
    if ! kill -TERM "$watcher_pid" 2>/dev/null; then
        reap_watcher
        printf 'ERROR: command lease watcher could not be stopped safely\n' >&2
        return 1
    fi
    reap_watcher
    if [[ "$watcher_exit_status" == 143 &&
        ! -e "$runtime_dir/lease-eof" &&
        ! -e "$runtime_dir/lease-protocol-violation" ]]; then
        watcher_exit_valid=true
        return 0
    fi
    printf 'ERROR: command lease watcher stopped with unexpected status %s\n' \
        "$watcher_exit_status" >&2
    return 1
}

stop_sampler() {
    [[ "$sampler_pid" =~ ^[1-9][0-9]*$ ]] || return 0
    : > "$stop_file"
    if wait "$sampler_pid"; then
        sampler_reaped=true
    fi
    sampler_pid=""
    rm -f -- "$stop_file"
}

# Invoked by the EXIT trap below.
# shellcheck disable=SC2329
emergency_cleanup() {
    local status=$?
    if [[ "$receipt_written" != true ]]; then
        set +e
        # Cleanup is the final termination owner. Ignore further signals until
        # the process group has been killed and reaped or left fail-closed.
        trap '' HUP INT TERM USR1
        if [[ -n "$signal_status" ]]; then
            printf 'ERROR: remote phase interrupted with status %s\n' \
                "$signal_status" >&2
        fi
        if [[ -z "$command_pgid" && "$command_pid" =~ ^[1-9][0-9]*$ ]]; then
            # The launch barrier guarantees there are no descendants until the
            # parent has established and recorded the process-group identity.
            kill -TERM "$command_pid" 2>/dev/null || true
            for _ in {1..100}; do
                reap_command_if_exited && break
                sleep 0.1
            done
            if [[ "$command_pid" =~ ^[1-9][0-9]*$ ]]; then
                kill -KILL "$command_pid" 2>/dev/null || true
                for _ in {1..100}; do
                    reap_command_if_exited && break
                    sleep 0.1
                done
            fi
        elif [[ -n "$runtime_dir" ]]; then
            request_termination
            reap_command_if_exited || true
        fi
        [[ -z "$watcher_pid" ]] || stop_watcher
        if [[ "$lease_input_fd" =~ ^[0-9]+$ ]]; then
            exec {lease_input_fd}<&-
            lease_input_fd=""
        fi
        [[ -z "$sampler_pid" ]] || stop_sampler
        rm -f -- "$session_temp"
        # Keep the global lock after any path that did not publish a terminal
        # receipt. A later phase must fail closed instead of risking overlap.
    fi
    exit "$status"
}
trap emergency_cleanup EXIT
trap 'handle_signal 129' HUP
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM
trap handle_lease_signal USR1

if ! mkdir -m 0700 -- "$ACTIVE_PHASE_LOCK"; then
    fail "another or unconfirmed RunPod phase owns the active-phase lock"
fi
runtime_dir="$(mktemp -d /tmp/bluemap-runpod-phase.XXXXXXXX)"
chmod 0700 "$runtime_dir"
jq -n \
    --arg sessionId "$session_id" \
    --arg sessionOutput "$session_output" \
    --arg startedAt "$started_at" \
    '{
        formatVersion: 1,
        sessionId: $sessionId,
        sessionOutput: $sessionOutput,
        startedAt: $startedAt
    }' > "$ACTIVE_PHASE_LOCK/session.json"
chmod 0600 "$ACTIVE_PHASE_LOCK/session.json"

# Bash redirects stdin of an asynchronous command from /dev/null when job
# control is disabled. Preserve the SSH channel explicitly for the watcher.
exec {lease_input_fd}<&0
lease_handshake=""
if ! IFS= read -r -t 15 lease_handshake <&"$lease_input_fd"; then
    fail "command lease handshake was not received"
fi
[[ "$lease_handshake" == "bluemap-phase-lease-v1:$session_id" ]] ||
    fail "command lease handshake is invalid"
lease_handshake=""

rm -f -- "$stop_file"
bluemap-runpod-sample-resources "$resource_output" "$stop_file" &
sampler_pid=$!

command_ready="$runtime_dir/command-ready"
command_go="$runtime_dir/command-go"
# The launch script is expanded only by Bash in the new session.
# shellcheck disable=SC2016
setsid bash -ceu '
    ready="$1"
    go="$2"
    timeout_seconds="$3"
    shift 3
    ready_temp="${ready}.tmp.$$"
    printf "%s\n" "$$" > "$ready_temp"
    mv -- "$ready_temp" "$ready"
    while [[ ! -e "$go" ]]; do
        sleep 0.01
    done
    exec timeout \
        --signal=TERM \
        --kill-after=30s \
        "${timeout_seconds}s" \
        "$@"
' bash "$command_ready" "$command_go" \
    "$BLUEMAP_PHASE_TIMEOUT_SECONDS" "$@" </dev/null &
command_pid=$!

for _ in {1..500}; do
    [[ -s "$command_ready" ]] && break
    kill -0 "$command_pid" 2>/dev/null || break
    sleep 0.01
done
[[ -s "$command_ready" ]] || fail "command launch barrier was not reached"
ready_pid="$(<"$command_ready")"
[[ "$ready_pid" == "$command_pid" ]] ||
    fail "command launch barrier reported an unexpected PID"
command_stat="$(<"/proc/$command_pid/stat")"
command_stat_tail="${command_stat##*) }"
read -r _ _ ready_pgid _ <<<"$command_stat_tail"
[[ "$ready_pgid" == "$command_pid" ]] ||
    fail "command process group was not established"
command_pgid="$ready_pgid"

# The command SSH channel is fed by a local held-open pipe. EOF is therefore
# an explicit session-loss signal, not ordinary command input.
phase_parent_pid="$$"
watcher_live="$runtime_dir/lease-watcher-live"
(
    initial_status=""
    if IFS= read -r -t 0.05 -n 1 _ <&"$lease_input_fd"; then
        : > "$runtime_dir/lease-protocol-violation"
    else
        initial_status=$?
        if [[ "$initial_status" == 1 ]]; then
            : > "$runtime_dir/lease-eof"
        else
            : > "$watcher_live"
            if IFS= read -r -n 1 _ <&"$lease_input_fd"; then
                : > "$runtime_dir/lease-protocol-violation"
            else
                : > "$runtime_dir/lease-eof"
            fi
        fi
    fi
    timestamp > "$runtime_dir/lease-observed-at"
    kill -USR1 "$phase_parent_pid" 2>/dev/null || true
) &
watcher_pid=$!
printf '%s\n' "$watcher_pid" > "$runtime_dir/lease-watcher-pid"
for _ in {1..500}; do
    [[ -e "$watcher_live" ||
        -e "$runtime_dir/lease-eof" ||
        -e "$runtime_dir/lease-protocol-violation" ]] && break
    kill -0 "$watcher_pid" 2>/dev/null || break
    sleep 0.01
done
[[ ! -e "$runtime_dir/lease-eof" &&
    ! -e "$runtime_dir/lease-protocol-violation" ]] ||
    fail "command lease ended before workload release"
[[ -e "$watcher_live" ]] || fail "command lease watcher was not armed"
: > "$command_go"

while [[ "$command_pid" =~ ^[1-9][0-9]*$ ]]; do
    if reap_command_if_exited; then
        break
    fi
    if [[ "$watcher_pid" =~ ^[1-9][0-9]*$ ]] &&
        ! watcher_process_alive; then
        reap_watcher
        if [[ "$watcher_exit_status" == 0 &&
            ( -e "$runtime_dir/lease-eof" ||
              -e "$runtime_dir/lease-protocol-violation" ) ]]; then
            watcher_exit_valid=true
        else
            printf 'ERROR: command lease watcher exited unexpectedly with status %s\n' \
                "$watcher_exit_status" >&2
        fi
        # A watcher exit, expected or otherwise, invalidates the live lease.
        # Signal the workload even if the watcher's own USR1 raced with exit.
        handle_lease_signal
    fi
    if [[ -e "$runtime_dir/termination-requested" ]]; then
        request_termination
    fi
    sleep 0.05 || true
done

if [[ -e "$runtime_dir/lease-eof" ||
    -e "$runtime_dir/lease-protocol-violation" ]]; then
    if [[ "$watcher_pid" =~ ^[1-9][0-9]*$ ]]; then
        reap_watcher
    fi
    if [[ "$watcher_exit_status" == 0 ]]; then
        watcher_exit_valid=true
    else
        printf 'ERROR: command lease watcher exited unexpectedly with status %s\n' \
            "$watcher_exit_status" >&2
    fi
else
    stop_watcher || true
fi
exec {lease_input_fd}<&-
lease_input_fd=""

[[ ! -e "$runtime_dir/lease-eof" ]] || lease_eof_observed=true
[[ ! -e "$runtime_dir/lease-protocol-violation" ]] ||
    lease_protocol_violation=true
if [[ -s "$runtime_dir/lease-observed-at" ]]; then
    lease_observed_at="$(<"$runtime_dir/lease-observed-at")"
fi
[[ ! -e "$runtime_dir/termination-requested" ]] ||
    termination_requested=true
[[ ! -e "$runtime_dir/kill-escalated" ]] || kill_escalated=true

if process_group_alive; then
    request_termination
fi
if process_group_alive; then
    process_group_empty=false
else
    process_group_empty=true
fi

if ! kill -0 "$sampler_pid" 2>/dev/null; then
    printf 'ERROR: load-generator resource sampler exited during the phase\n' >&2
else
    stop_sampler
fi

completed_at="$(timestamp)"
session_passed=false
if [[ "$process_group_empty" == true &&
    "$watcher_reaped" == true &&
    "$watcher_exit_valid" == true &&
    "$sampler_reaped" == true &&
    "$lease_protocol_violation" == false ]]; then
    session_passed=true
fi

jq -n \
    --arg sessionId "$session_id" \
    --arg sessionOutput "$session_output" \
    --arg startedAt "$started_at" \
    --arg completedAt "$completed_at" \
    --arg leaseObservedAt "$lease_observed_at" \
    --argjson leaseEofObserved "$lease_eof_observed" \
    --argjson leaseProtocolViolation "$lease_protocol_violation" \
    --argjson terminationRequested "$termination_requested" \
    --argjson killEscalated "$kill_escalated" \
    --argjson commandExitStatus "$command_status" \
    --argjson processGroupId "$command_pgid" \
    --argjson processGroupEmpty "$process_group_empty" \
    --argjson watcherReaped "$watcher_reaped" \
    --argjson samplerReaped "$sampler_reaped" \
    --argjson passed "$session_passed" \
    '
    def nullable: if . == "" then null else . end;
    {
        kind: "runpod-command-session",
        formatVersion: 1,
        sessionId: $sessionId,
        sessionOutput: $sessionOutput,
        activeLock: "/tmp/bluemap-runpod-active-phase.lock",
        startedAt: $startedAt,
        completedAt: $completedAt,
        lease: {
            required: true,
            eofObserved: $leaseEofObserved,
            protocolViolation: $leaseProtocolViolation,
            observedAt: ($leaseObservedAt | nullable)
        },
        termination: {
            requested: $terminationRequested,
            termSignal: (if $terminationRequested then "TERM" else null end),
            killEscalated: $killEscalated,
            commandExitStatus: $commandExitStatus,
            processGroupId: $processGroupId,
            processGroupEmpty: $processGroupEmpty,
            watcherReaped: $watcherReaped,
            samplerReaped: $samplerReaped
        },
        passed: $passed
    }
    ' > "$session_temp"
chmod 0600 "$session_temp"

# Publish the terminal receipt only after the process group is confirmed empty.
# The lock is deliberately retained forever on every unconfirmed path.
if [[ "$session_passed" != true ]]; then
    printf 'ERROR: remote phase termination could not be confirmed\n' >&2
    exit 125
fi
mv -- "$session_temp" "$session_output"
receipt_written=true
rm -f -- "$ACTIVE_PHASE_LOCK/session.json"
rmdir -- "$ACTIVE_PHASE_LOCK"
# No signal handler may reference runtime state after this point.
trap - HUP INT TERM
rm -rf -- "$runtime_dir"
runtime_dir=""

exit "$command_status"
