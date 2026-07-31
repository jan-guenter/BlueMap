#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly REPOSITORY=/opt/bluemap
readonly BENCHMARK_ROOT="$REPOSITORY/benchmarks/web-performance"
readonly FORMAL_ROOT="$BENCHMARK_ROOT/artifacts"
readonly CONTROLLER_ROOT="$BENCHMARK_ROOT/controller"
readonly ORCHESTRATOR="$CONTROLLER_ROOT/formal/orchestrate.py"
readonly ANALYZER="$CONTROLLER_ROOT/formal/analyze.py"
readonly FROZEN_ROOT="$CONTROLLER_ROOT/frozen"
readonly MATRIX="$FROZEN_ROOT/formal-inputs/matrix.json"
readonly SCHEDULE="$FROZEN_ROOT/formal-inputs/schedule.json"
readonly ADMISSION_IDENTITIES="$FROZEN_ROOT/formal-inputs/runtime-admission-identities.json"
readonly BUNDLE_MANIFEST="$FROZEN_ROOT/formal-inputs/bundle-manifest.json"
readonly SNAPSHOT_MANIFEST="$FROZEN_ROOT/manifest.json"
readonly RUNPOD_IDENTITY=/runpod-identity/identity.json
readonly RUNPOD_IDENTITY_KEY=/opt/bluemap-runtime/credentials/id_ed25519
readonly PUBLIC_SERVICE=bluemap-perf-public
readonly PUBLIC_SERVICE_PORT=8100

child_pid=""
termination_requested=false

fail() {
    printf 'FORMAL CONTROLLER REFUSED: %s\n' "$*" >&2
    exit 2
}

# Invoked indirectly by the TERM/INT trap installed below.
# shellcheck disable=SC2317,SC2329
forward_termination() {
    termination_requested=true
    if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
        kill -INT "$child_pid" 2>/dev/null || true
    fi
}

run_child() {
    local status
    [[ "$termination_requested" == false ]] || return 143
    "$@" &
    child_pid=$!
    if [[ "$termination_requested" == true ]]; then
        kill -INT "$child_pid" 2>/dev/null || true
    fi
    wait "$child_pid"
    status=$?
    if kill -0 "$child_pid" 2>/dev/null; then
        wait "$child_pid"
        status=$?
    fi
    child_pid=""
    return "$status"
}

trap forward_termination TERM INT

[[ $# -eq 0 ]] ||
    fail "runtime arguments are forbidden; the frozen Job controls every option"
[[ "${BLUEMAP_BENCHMARK_REVISION:-}" =~ ^[0-9a-f]{40}$ ]] ||
    fail "BLUEMAP_BENCHMARK_REVISION must be a full lowercase Git SHA"
[[ "${BLUEMAP_FORMAL_RUN_ID:-}" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] ||
    fail "BLUEMAP_FORMAL_RUN_ID is invalid"
[[ "${BLUEMAP_TRAFFIC_BASE_URL:-}" == "https://bluemap-test.guenter.cloud" ]] ||
    fail "BLUEMAP_TRAFFIC_BASE_URL must be https://bluemap-test.guenter.cloud"
[[ "${BLUEMAP_CONTROLLER_IMAGE_DIGEST:-}" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    fail "BLUEMAP_CONTROLLER_IMAGE_DIGEST must be an immutable digest"
[[ "${BLUEMAP_CONTROLLER_POD_NAME:-}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
    fail "BLUEMAP_CONTROLLER_POD_NAME is invalid"
[[ "${BLUEMAP_PROMETHEUS_URL:-}" == \
    "http://rancher-monitoring-prometheus.cattle-monitoring-system.svc:9090" ]] ||
    fail "BLUEMAP_PROMETHEUS_URL must use the frozen in-cluster Service URL"
[[ -r "$RUNPOD_IDENTITY" && ! -L "$RUNPOD_IDENTITY" ]] ||
    fail "frozen RunPod identity is unavailable"
[[ -r "$RUNPOD_IDENTITY_KEY" && ! -L "$RUNPOD_IDENTITY_KEY" ]] ||
    fail "RunPod SSH identity is unavailable"
key_mode="$(stat -c '%a' "$RUNPOD_IDENTITY_KEY")"
(( (8#$key_mode & 8#077) == 0 )) ||
    fail "RunPod SSH identity must not be group- or world-accessible"

for required in \
    "$ORCHESTRATOR" \
    "$ANALYZER" \
    "$MATRIX" \
    "$SCHEDULE" \
    "$ADMISSION_IDENTITIES" \
    "$BUNDLE_MANIFEST" \
    "$SNAPSHOT_MANIFEST"; do
    [[ -f "$required" && ! -L "$required" ]] ||
        fail "frozen input is unavailable: $required"
done

actual_revision="$(git -C "$REPOSITORY" rev-parse HEAD)"
[[ "$actual_revision" == "$BLUEMAP_BENCHMARK_REVISION" ]] ||
    fail "controller checkout revision differs from runtime configuration"
[[ -z "$(git -C "$REPOSITORY" status --porcelain --untracked-files=no)" ]] ||
    fail "controller checkout has tracked changes"
pod_json="$(
    kubectl \
        --kubeconfig /etc/bluemap-controller/kubeconfig \
        --namespace minecraft \
        get "pod/$BLUEMAP_CONTROLLER_POD_NAME" \
        -o json
)" || fail "controller Pod identity is unavailable"
init_image_reference="$(
    jq -er '
        [
            .spec.initContainers[]?
            | select(.name == "prepare-runpod-credentials")
            | .image
        ]
        | if length == 1 then .[0]
          else error("init container image reference missing")
          end
    ' <<<"$pod_json"
)" || fail "init container image reference is unavailable from its Pod manifest"
controller_image_reference="$(
    jq -er '
        [
            .spec.containers[]?
            | select(.name == "controller")
            | .image
        ]
        | if length == 1 then .[0]
          else error("controller image reference missing")
          end
    ' <<<"$pod_json"
)" || fail "controller image reference is unavailable from its Pod manifest"
init_image_id="$(
    jq -er '
        [
            .status.initContainerStatuses[]?
            | select(.name == "prepare-runpod-credentials")
            | .imageID
        ]
        | if length == 1 then .[0]
          else error("init container imageID missing")
          end
    ' <<<"$pod_json"
)" || fail "init container image identity is unavailable from its Pod status"
controller_image_id="$(
    jq -er '
        [
            .status.containerStatuses[]?
            | select(.name == "controller")
            | .imageID
        ]
        | if length == 1 then .[0]
          else error("controller imageID missing")
          end
    ' <<<"$pod_json"
)" || fail "controller image identity is unavailable from its Pod status"
[[ "$init_image_reference" == "$controller_image_reference" ]] ||
    fail "init and controller Pod manifest image references differ"
[[ "$controller_image_reference" == *"@$BLUEMAP_CONTROLLER_IMAGE_DIGEST" ]] ||
    fail "Pod manifest image reference differs from BLUEMAP_CONTROLLER_IMAGE_DIGEST"
[[ "$init_image_id" == *"@$BLUEMAP_CONTROLLER_IMAGE_DIGEST" ]] ||
    fail "running init image differs from BLUEMAP_CONTROLLER_IMAGE_DIGEST"
[[ "$controller_image_id" == *"@$BLUEMAP_CONTROLLER_IMAGE_DIGEST" ]] ||
    fail "running controller image differs from BLUEMAP_CONTROLLER_IMAGE_DIGEST"

identity_run_id="$(jq -er '.runId' "$RUNPOD_IDENTITY")" ||
    fail "RunPod identity has no runId"
[[ "$identity_run_id" == "$BLUEMAP_FORMAL_RUN_ID" ]] ||
    fail "RunPod identity and formal run ID differ"

readonly RUN_ROOT="$FORMAL_ROOT/formal-runs/$BLUEMAP_FORMAL_RUN_ID"
[[ ! -e "$RUN_ROOT" ]] ||
    fail "formal run root already exists; automatic resume is forbidden"
mkdir -p -- "$(dirname -- "$RUN_ROOT")"
test_file="$(dirname -- "$RUN_ROOT")/.controller-write-test-$$"
(umask 077; : > "$test_file") ||
    fail "formal artifact PVC is not writable"
rm -f -- "$test_file"

python /usr/local/lib/bluemap-controller/validate_frozen_bundle.py \
    --repository "$REPOSITORY" \
    --revision "$BLUEMAP_BENCHMARK_REVISION"
python "$ORCHESTRATOR" validate

"$BENCHMARK_ROOT/tools/runpod_loadgen.sh" \
    --identity "$RUNPOD_IDENTITY" \
    --identity-key "$RUNPOD_IDENTITY_KEY" \
    validate > /tmp/runpod-live-identity.json

orchestrator_command=(
    python "$ORCHESTRATOR" run
    --run-root "$RUN_ROOT"
    --confirm RUN-FROZEN-80-ENTRY-MATRIX
    --benchmark-python /usr/local/bin/python
    --kubeconfig /etc/bluemap-controller/kubeconfig
    --prometheus-url "$BLUEMAP_PROMETHEUS_URL"
    --load-generator-backend runpod-ssh
    --load-generator-identity "$RUNPOD_IDENTITY"
    --load-generator-identity-key "$RUNPOD_IDENTITY_KEY"
    --traffic-base-url "$BLUEMAP_TRAFFIC_BASE_URL"
    --traffic-service "$PUBLIC_SERVICE"
    --traffic-service-port "$PUBLIC_SERVICE_PORT"
    --formal-run-id "$BLUEMAP_FORMAL_RUN_ID"
    --require-edge-bypass
)

[[ "$termination_requested" == false ]] || exit 143
set +e
run_child "${orchestrator_command[@]}"
status=$?
set -e
if ((status != 0)); then
    if [[ "$termination_requested" == true ]]; then
        exit 143
    fi
    exit "$status"
fi
[[ "$termination_requested" == false ]] || exit 143

analysis_dir="$RUN_ROOT/analysis"
set +e
python "$ANALYZER" \
    --matrix "$MATRIX" \
    --schedule "$SCHEDULE" \
    --runtime-admission-identities "$ADMISSION_IDENTITIES" \
    --bundle-manifest "$BUNDLE_MANIFEST" \
    --run-root "$RUN_ROOT" \
    --output-dir "$analysis_dir"
analysis_status=$?
set -e

mkdir -p -- "$analysis_dir"
printf '%s\n' "$analysis_status" > "$analysis_dir/exit-status.txt"
(
    cd -- "$analysis_dir"
    sha256sum -- report.json report.md exit-status.txt > SHA256SUMS
)
exit "$analysis_status"
