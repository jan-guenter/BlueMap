#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BENCHMARK_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "$BENCHMARK_ROOT/../.." && pwd)"

KUBECONFIG_PATH="/root/.kube/guenter-cloud"
NAMESPACE="minecraft"
LOADGEN_POD="bluemap-perf-loadgen"
K6_SCRIPT="$BENCHMARK_ROOT/k6/bluemap.js"
CONTRACT_SCRIPT="$BENCHMARK_ROOT/tools/check_http_contract.py"
ARTIFACT_ROOT="$BENCHMARK_ROOT/artifacts"
PROFILE="map-data-mixed"
RATE="100"
VIEWERS="100"
MARKER_INTERVAL_SECONDS="10"
MIN_ACHIEVED_RATE_RATIO="0.99"
TRACE_SEED="bluemap-web-performance-v1"
LATENCY_P95_MS="500"
LATENCY_P99_MS="1000"
LARGE_OBJECT_LATENCY_P95_MS=""
LARGE_OBJECT_LATENCY_P99_MS=""
PRE_ALLOCATED_VUS="32"
MAX_VUS="512"
ACCEPT_ENCODING="zstd"
STORED_ENCODING="zstd"
CONTRACT_MODE="enhanced"
WARMUP_DURATION="2m"
MEASUREMENT_DURATION="5m"
COOLDOWN_SECONDS="60"
REPETITIONS="1"
METRICS_INTERVAL_SECONDS="5"
PROMETHEUS_URL="${PROMETHEUS_URL:-}"
PROMETHEUS_STEP_SECONDS="${PROMETHEUS_STEP_SECONDS:-15}"
MAX_NON_TARGET_NODE_CPU_RANGE_CORES="0.5"
MAX_NON_TARGET_NODE_CPU_MEAN_CORES="2.0"
MAX_NON_TARGET_NODE_CPU_MAXIMUM_CORES="3.0"
PYTHON_BIN="${BENCHMARK_PYTHON:-python3}"
SERVICE_PORT=""
SERVICE=""
CASE_ID=""
MANIFEST=""
MATRIX=""
SCHEDULE=""
SCHEDULE_ENTRY_ID=""
SCHEDULE_ENTRY_JSON=""
VARIANT_ID=""
IMPLEMENTATION=""
STORAGE_TYPE=""
DATABASE_BACKEND=""

declare -a WEB_DEPLOYMENTS=()
declare -a WEB_PODS=()
declare -a DATABASE_PODS=()
declare -a EXPECTED_MAP_IDS=()
declare -a CONFIGMAPS=()
declare -a DERIVED_CONFIGMAPS=()
declare -a SAMPLE_TARGETS=()
declare -a ALL_PODS=()
declare -a TARGET_NODES=()

SAMPLER_PID=""
PORT_FORWARD_PID=""
ARTIFACT_DIR=""
PHASE_FILE=""
SAMPLER_STOP_FILE=""
SAMPLER_FAILED_FILE=""
ENDPOINT_SAMPLE_FAILED_FILE=""
FAILURES_FILE=""
DERIVED_CONFIGMAPS_FILE=""
PROMETHEUS_INSPECTION=""
CASE_START_EPOCH=""
CASE_START_TIMESTAMP=""
CASE_END_EPOCH=""
CASE_END_TIMESTAMP=""
MANIFEST_MAP_IDS_JSON=""
CONFIGMAPS_JSON=""
EXPECTED_ITERATION_RATE=""
EFFECTIVE_LATENCY_P95_MS=""
EFFECTIVE_LATENCY_P99_MS=""
DESIRED_WEB_REPLICA_COUNT="0"

usage() {
    cat <<'EOF'
Usage:
  run_origin_case.sh \
    --case-id CASE_ID \
    --service bluemap-perf-SERVICE \
    --service-port PORT \
  --manifest MANIFEST.json \
    --map-id world \
    --configmap bluemap-perf-CONFIGMAP \
    --web-deployment bluemap-perf-DEPLOYMENT \
    --web-pod bluemap-perf-WEB-POD \
    [--database-pod bluemap-perf-DATABASE-POD] [options]

Required targets are explicit; every Service, Deployment, and Pod name must
start with "bluemap-perf-". The runner only performs Kubernetes get, get
--raw, exec into bluemap-perf-loadgen, and port-forward operations.

Workload options:
  --profile NAME                  k6 profile (default: map-data-mixed)
  --rate N                        offered requests/second (default: 100)
  --viewers N                     player polls/second in live-viewers (default: 100)
  --marker-interval-seconds N     per-viewer marker interval (default: 10)
  --min-achieved-rate-ratio R     formal arrival-rate gate (default: 0.99)
  --trace-seed TEXT                deterministic request trace seed
  --latency-p95-ms N               formal p95 gate (default: 500)
  --latency-p99-ms N               formal p99 gate (default: 1000)
  --large-object-latency-p95-ms N  optional large-object p95 override
  --large-object-latency-p99-ms N  optional large-object p99 override
  --pre-allocated-vus N           k6 preallocated VUs (default: 32)
  --max-vus N                     k6 maximum VUs (default: 512)
  --accept-encoding NAME          request encoding (default: zstd)
  --stored-encoding NAME          contract expectation (default: zstd)
  --contract-mode enhanced|legacy HTTP contract gate (default: enhanced)
  --warmup DURATION               k6 warmup duration (default: 2m)
  --measurement DURATION          measured duration (default: 5m)
  --cooldown-seconds N            no-request cooldown (default: 60)
  --repetitions N                 repetitions in this case (formal default: 1)
  --metrics-interval-seconds N    metrics.k8s.io interval (default: 5)
  --prometheus-url URL            optional Prometheus base URL
  --prometheus-step-seconds N     query_range step (default: 15)
  --max-non-target-node-cpu-range-cores N
                                  reject noisy repetitions above this CPU range
                                  (default: 0.5 cores; Prometheus only)
  --max-non-target-node-cpu-mean-cores N
                                  reject mean background CPU above N (default: 2)
  --max-non-target-node-cpu-maximum-cores N
                                  reject peak background CPU above N (default: 3)

Path/cluster options:
  --artifact-root DIRECTORY
  --matrix FILE                     frozen formal matrix (requires schedule/entry)
  --schedule FILE                   generated balanced schedule
  --schedule-entry ID               exact entry to validate for this case
  --variant-id ID                    tested variant id (required for formal runs)
  --implementation php|java|rust     tested server implementation
  --storage-type sql|file            tested storage type
  --database-backend postgresql|mariadb|none
                                      tested database backend
  --k6-script FILE
  --contract-script FILE
  --python COMMAND                 Python with zstandard installed
  --kubeconfig FILE
  --namespace NAME                default: minecraft
  --map-id NAME                   selected manifest map id; repeatable
  --configmap NAME                non-secret rendered config; repeatable
  --web-deployment NAME           repeatable
  --web-pod NAME                  repeatable
  --database-pod NAME             optional and repeatable; omit for file storage
  -h, --help
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Required command '$1' is unavailable"
}

validate_prefixed_name() {
    local kind="$1"
    local name="$2"
    [[ "$name" =~ ^bluemap-perf-[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] ||
        die "$kind name '$name' must be an exact bluemap-perf-* resource name"
}

validate_positive_integer() {
    local name="$1"
    local value="$2"
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$name must be a positive integer"
}

validate_k6_duration() {
    local name="$1"
    local value="$2"
    [[ "$value" =~ ^[1-9][0-9]*(ms|s|m|h)$ ]] ||
        die "$name must be a positive k6 duration such as 30s, 2m, or 1h"
}

validate_positive_number() {
    local name="$1"
    local value="$2"
    jq -en --arg value "$value" \
        '($value | tonumber) as $number | $number > 0' >/dev/null 2>&1 ||
        die "$name must be a positive number"
}

while (($# > 0)); do
    case "$1" in
        --case-id)
            CASE_ID="${2:-}"
            shift 2
            ;;
        --service)
            SERVICE="${2:-}"
            shift 2
            ;;
        --service-port)
            SERVICE_PORT="${2:-}"
            shift 2
            ;;
        --manifest)
            MANIFEST="${2:-}"
            shift 2
            ;;
        --matrix)
            MATRIX="${2:-}"
            shift 2
            ;;
        --schedule)
            SCHEDULE="${2:-}"
            shift 2
            ;;
        --schedule-entry)
            SCHEDULE_ENTRY_ID="${2:-}"
            shift 2
            ;;
        --variant-id)
            VARIANT_ID="${2:-}"
            shift 2
            ;;
        --implementation)
            IMPLEMENTATION="${2:-}"
            shift 2
            ;;
        --storage-type)
            STORAGE_TYPE="${2:-}"
            shift 2
            ;;
        --database-backend)
            DATABASE_BACKEND="${2:-}"
            shift 2
            ;;
        --web-deployment)
            WEB_DEPLOYMENTS+=("${2:-}")
            shift 2
            ;;
        --web-pod)
            WEB_PODS+=("${2:-}")
            shift 2
            ;;
        --database-pod)
            DATABASE_PODS+=("${2:-}")
            shift 2
            ;;
        --map-id)
            EXPECTED_MAP_IDS+=("${2:-}")
            shift 2
            ;;
        --configmap)
            CONFIGMAPS+=("${2:-}")
            shift 2
            ;;
        --profile)
            PROFILE="${2:-}"
            shift 2
            ;;
        --rate)
            RATE="${2:-}"
            shift 2
            ;;
        --viewers)
            VIEWERS="${2:-}"
            shift 2
            ;;
        --marker-interval-seconds)
            MARKER_INTERVAL_SECONDS="${2:-}"
            shift 2
            ;;
        --min-achieved-rate-ratio)
            MIN_ACHIEVED_RATE_RATIO="${2:-}"
            shift 2
            ;;
        --trace-seed)
            TRACE_SEED="${2:-}"
            shift 2
            ;;
        --latency-p95-ms)
            LATENCY_P95_MS="${2:-}"
            shift 2
            ;;
        --latency-p99-ms)
            LATENCY_P99_MS="${2:-}"
            shift 2
            ;;
        --large-object-latency-p95-ms)
            LARGE_OBJECT_LATENCY_P95_MS="${2:-}"
            shift 2
            ;;
        --large-object-latency-p99-ms)
            LARGE_OBJECT_LATENCY_P99_MS="${2:-}"
            shift 2
            ;;
        --pre-allocated-vus)
            PRE_ALLOCATED_VUS="${2:-}"
            shift 2
            ;;
        --max-vus)
            MAX_VUS="${2:-}"
            shift 2
            ;;
        --accept-encoding)
            ACCEPT_ENCODING="${2:-}"
            shift 2
            ;;
        --stored-encoding)
            STORED_ENCODING="${2:-}"
            shift 2
            ;;
        --contract-mode)
            CONTRACT_MODE="${2:-}"
            shift 2
            ;;
        --warmup)
            WARMUP_DURATION="${2:-}"
            shift 2
            ;;
        --measurement)
            MEASUREMENT_DURATION="${2:-}"
            shift 2
            ;;
        --cooldown-seconds)
            COOLDOWN_SECONDS="${2:-}"
            shift 2
            ;;
        --repetitions)
            REPETITIONS="${2:-}"
            shift 2
            ;;
        --metrics-interval-seconds)
            METRICS_INTERVAL_SECONDS="${2:-}"
            shift 2
            ;;
        --prometheus-url)
            PROMETHEUS_URL="${2:-}"
            shift 2
            ;;
        --prometheus-step-seconds)
            PROMETHEUS_STEP_SECONDS="${2:-}"
            shift 2
            ;;
        --max-non-target-node-cpu-range-cores)
            MAX_NON_TARGET_NODE_CPU_RANGE_CORES="${2:-}"
            shift 2
            ;;
        --max-non-target-node-cpu-mean-cores)
            MAX_NON_TARGET_NODE_CPU_MEAN_CORES="${2:-}"
            shift 2
            ;;
        --max-non-target-node-cpu-maximum-cores)
            MAX_NON_TARGET_NODE_CPU_MAXIMUM_CORES="${2:-}"
            shift 2
            ;;
        --artifact-root)
            ARTIFACT_ROOT="${2:-}"
            shift 2
            ;;
        --k6-script)
            K6_SCRIPT="${2:-}"
            shift 2
            ;;
        --contract-script)
            CONTRACT_SCRIPT="${2:-}"
            shift 2
            ;;
        --python)
            PYTHON_BIN="${2:-}"
            shift 2
            ;;
        --kubeconfig)
            KUBECONFIG_PATH="${2:-}"
            shift 2
            ;;
        --namespace)
            NAMESPACE="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "Unknown argument '$1'"
            ;;
    esac
done

[[ "$CASE_ID" =~ ^[a-z0-9][a-z0-9.-]{0,62}$ ]] ||
    die "--case-id must contain 1-63 lowercase letters, digits, dots, or hyphens"
[[ "$NAMESPACE" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] ||
    die "--namespace is not a valid Kubernetes namespace"
[[ -n "$SERVICE" ]] || die "--service is required"
[[ -n "$SERVICE_PORT" ]] || die "--service-port is required"
[[ -n "$MANIFEST" ]] || die "--manifest is required"
((${#WEB_DEPLOYMENTS[@]} > 0)) || die "At least one --web-deployment is required"
((${#WEB_PODS[@]} > 0)) || die "At least one --web-pod is required"
((${#EXPECTED_MAP_IDS[@]} > 0)) || die "At least one --map-id is required"
((${#CONFIGMAPS[@]} > 0)) || die "At least one --configmap is required"

validate_prefixed_name "Service" "$SERVICE"
validate_prefixed_name "load-generator Pod" "$LOADGEN_POD"
for name in "${WEB_DEPLOYMENTS[@]}"; do
    validate_prefixed_name "Deployment" "$name"
done
for name in "${WEB_PODS[@]}"; do
    validate_prefixed_name "web Pod" "$name"
    [[ "$name" != "$LOADGEN_POD" ]] || die "The load-generator Pod cannot be a web Pod"
done
for name in "${DATABASE_PODS[@]}"; do
    validate_prefixed_name "database Pod" "$name"
    [[ "$name" != "$LOADGEN_POD" ]] || die "The load-generator Pod cannot be a database Pod"
done
for name in "${CONFIGMAPS[@]}"; do
    validate_prefixed_name "ConfigMap" "$name"
done
for map_id in "${EXPECTED_MAP_IDS[@]}"; do
    [[ "$map_id" =~ ^[A-Za-z0-9_.-]+$ ]] ||
        die "map id '$map_id' contains unsupported characters"
done

validate_positive_integer "service port" "$SERVICE_PORT"
((SERVICE_PORT <= 65535)) || die "service port must not exceed 65535"
validate_positive_integer "rate" "$RATE"
validate_positive_integer "viewers" "$VIEWERS"
validate_positive_integer "marker interval seconds" "$MARKER_INTERVAL_SECONDS"
validate_positive_integer "pre-allocated VUs" "$PRE_ALLOCATED_VUS"
validate_positive_integer "maximum VUs" "$MAX_VUS"
validate_positive_integer "cooldown seconds" "$COOLDOWN_SECONDS"
validate_positive_integer "repetitions" "$REPETITIONS"
validate_positive_integer "metrics interval" "$METRICS_INTERVAL_SECONDS"
validate_positive_integer "Prometheus step" "$PROMETHEUS_STEP_SECONDS"
validate_positive_number "latency p95" "$LATENCY_P95_MS"
validate_positive_number "latency p99" "$LATENCY_P99_MS"
validate_positive_number \
    "maximum non-target node CPU range" \
    "$MAX_NON_TARGET_NODE_CPU_RANGE_CORES"
validate_positive_number \
    "maximum non-target node CPU mean" \
    "$MAX_NON_TARGET_NODE_CPU_MEAN_CORES"
validate_positive_number \
    "maximum non-target node CPU level" \
    "$MAX_NON_TARGET_NODE_CPU_MAXIMUM_CORES"
if [[ -n "$LARGE_OBJECT_LATENCY_P95_MS" ]]; then
    validate_positive_number \
        "large-object latency p95" \
        "$LARGE_OBJECT_LATENCY_P95_MS"
fi
if [[ -n "$LARGE_OBJECT_LATENCY_P99_MS" ]]; then
    validate_positive_number \
        "large-object latency p99" \
        "$LARGE_OBJECT_LATENCY_P99_MS"
fi
((PROMETHEUS_STEP_SECONDS <= 3600)) ||
    die "Prometheus step must not exceed 3600 seconds"
validate_k6_duration "warmup" "$WARMUP_DURATION"
validate_k6_duration "measurement" "$MEASUREMENT_DURATION"
[[ "$MIN_ACHIEVED_RATE_RATIO" =~ ^(0(\.[0-9]+)?|1(\.0+)?)$ ]] ||
    die "--min-achieved-rate-ratio must be greater than zero and at most one"
[[ "$MIN_ACHIEVED_RATE_RATIO" != "0" &&
   "$MIN_ACHIEVED_RATE_RATIO" != "0.0" &&
   "$MIN_ACHIEVED_RATE_RATIO" != "0.00" ]] ||
    die "--min-achieved-rate-ratio must be greater than zero"
[[ -n "$TRACE_SEED" && ${#TRACE_SEED} -le 128 && "$TRACE_SEED" != *$'\n'* &&
   "$TRACE_SEED" != *$'\r'* ]] ||
    die "--trace-seed must be 1-128 characters without line breaks"
[[ "$CONTRACT_MODE" == "enhanced" || "$CONTRACT_MODE" == "legacy" ]] ||
    die "--contract-mode must be enhanced or legacy"
[[ "$PROFILE" =~ ^(static|hot-tile|random-tiles|large-tile|settings|textures|large-object|missing-tile|conditional|live-viewers|map-data-mixed|browser-mixed)$ ]] ||
    die "--profile is not a supported benchmark profile"
[[ "$PROFILE" != "conditional" || "$CONTRACT_MODE" == "enhanced" ]] ||
    die "The conditional profile requires --contract-mode enhanced"
[[ "$STORED_ENCODING" =~ ^(gzip|zstd|deflate|identity)$ ]] ||
    die "--stored-encoding must be gzip, zstd, deflate, or identity"

variant_option_count=0
[[ -n "$VARIANT_ID" ]] && ((variant_option_count += 1))
[[ -n "$IMPLEMENTATION" ]] && ((variant_option_count += 1))
[[ -n "$STORAGE_TYPE" ]] && ((variant_option_count += 1))
[[ -n "$DATABASE_BACKEND" ]] && ((variant_option_count += 1))
((variant_option_count == 0 || variant_option_count == 4)) ||
    die "--variant-id, --implementation, --storage-type, and --database-backend must be supplied together"
if ((variant_option_count == 4)); then
    [[ "$VARIANT_ID" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] ||
        die "--variant-id must contain 1-40 lowercase letters, digits, or hyphens"
    [[ "$IMPLEMENTATION" =~ ^(php|java|rust)$ ]] ||
        die "--implementation must be php, java, or rust"
    [[ "$STORAGE_TYPE" =~ ^(sql|file)$ ]] ||
        die "--storage-type must be sql or file"
    [[ "$DATABASE_BACKEND" =~ ^(postgresql|mariadb|none)$ ]] ||
        die "--database-backend must be postgresql, mariadb, or none"
    if [[ "$STORAGE_TYPE" == "file" ]]; then
        [[ "$DATABASE_BACKEND" == "none" ]] ||
            die "File storage requires --database-backend none"
        ((${#DATABASE_PODS[@]} == 0)) ||
            die "File storage must not select a --database-pod"
    else
        [[ "$DATABASE_BACKEND" != "none" ]] ||
            die "SQL storage requires a real --database-backend"
        ((${#DATABASE_PODS[@]} > 0)) ||
            die "SQL storage requires at least one --database-pod"
    fi
fi
[[ -f "$MANIFEST" ]] || die "Manifest '$MANIFEST' is not a regular file"
[[ -f "$K6_SCRIPT" ]] || die "k6 script '$K6_SCRIPT' is not a regular file"
[[ -f "$CONTRACT_SCRIPT" ]] || die "Contract script '$CONTRACT_SCRIPT' is not a regular file"
[[ -f "$SCRIPT_DIR/sanitize_kubernetes_resource.py" ]] ||
    die "Kubernetes snapshot helper is unavailable"
[[ -f "$SCRIPT_DIR/capture_prometheus.py" ]] ||
    die "Prometheus capture helper is unavailable"
[[ -f "$SCRIPT_DIR/sanitize_configmap.py" ]] ||
    die "ConfigMap snapshot helper is unavailable"
[[ -f "$SCRIPT_DIR/configmap_references.py" ]] ||
    die "ConfigMap reference helper is unavailable"
[[ -f "$SCRIPT_DIR/slow_reader.py" ]] ||
    die "Slow-reader helper is unavailable"
[[ -f "$SCRIPT_DIR/generate_schedule.py" ]] ||
    die "Schedule validator is unavailable"
[[ -f "$SCRIPT_DIR/check_arrival_gate.py" ]] ||
    die "Arrival-gate helper is unavailable"
[[ -f "$KUBECONFIG_PATH" ]] || die "Kubeconfig '$KUBECONFIG_PATH' is not a regular file"

for command in kubectl jq sha256sum tee diff; do
    require_command "$command"
done
require_command "$PYTHON_BIN"
if [[ -n "$PROMETHEUS_URL" ]]; then
    PROMETHEUS_INSPECTION="$(
        "$PYTHON_BIN" "$SCRIPT_DIR/capture_prometheus.py" \
            inspect-url "$PROMETHEUS_URL"
    )" || die "Prometheus URL validation failed"
fi
"$PYTHON_BIN" -c 'import zstandard' >/dev/null 2>&1 ||
    die "The local Python environment needs the zstandard module for contract checks"
jq -en --arg ratio "$MIN_ACHIEVED_RATE_RATIO" \
    '($ratio | tonumber) as $value | $value > 0 and $value <= 1' >/dev/null ||
    die "--min-achieved-rate-ratio must be greater than zero and at most one"

EFFECTIVE_LATENCY_P95_MS="$LATENCY_P95_MS"
EFFECTIVE_LATENCY_P99_MS="$LATENCY_P99_MS"
if [[ "$PROFILE" == "large-object" ]]; then
    EFFECTIVE_LATENCY_P95_MS="${LARGE_OBJECT_LATENCY_P95_MS:-$LATENCY_P95_MS}"
    EFFECTIVE_LATENCY_P99_MS="${LARGE_OBJECT_LATENCY_P99_MS:-$LATENCY_P99_MS}"
fi
jq -en \
    --arg p95 "$EFFECTIVE_LATENCY_P95_MS" \
    --arg p99 "$EFFECTIVE_LATENCY_P99_MS" \
    '($p99 | tonumber) >= ($p95 | tonumber)' >/dev/null ||
    die "The effective p99 latency gate must be at least the p95 gate"

schedule_option_count=0
[[ -n "$MATRIX" ]] && ((schedule_option_count += 1))
[[ -n "$SCHEDULE" ]] && ((schedule_option_count += 1))
[[ -n "$SCHEDULE_ENTRY_ID" ]] && ((schedule_option_count += 1))
((schedule_option_count == 0 || schedule_option_count == 3)) ||
    die "--matrix, --schedule, and --schedule-entry must be supplied together"
if ((schedule_option_count == 3)); then
    ((variant_option_count == 4)) ||
        die "Formal schedule entries require complete variant metadata"
    ((REPETITIONS == 1)) ||
        die "A formal schedule entry must run with --repetitions 1"
    [[ -f "$MATRIX" ]] || die "Matrix '$MATRIX' is not a regular file"
    [[ -f "$SCHEDULE" ]] || die "Schedule '$SCHEDULE' is not a regular file"
    SCHEDULE_ENTRY_JSON="$(
        "$PYTHON_BIN" "$SCRIPT_DIR/generate_schedule.py" validate-entry \
            "$MATRIX" "$SCHEDULE" "$SCHEDULE_ENTRY_ID"
    )" || die "Formal matrix/schedule validation failed"
    jq -e \
        --arg caseId "$CASE_ID" \
        --arg variantId "$VARIANT_ID" \
        --arg implementation "$IMPLEMENTATION" \
        --arg storageType "$STORAGE_TYPE" \
        --arg databaseBackend "$DATABASE_BACKEND" \
        --argjson replicaCount "${#WEB_PODS[@]}" \
        --arg profile "$PROFILE" \
        --argjson rate "$RATE" \
        --argjson viewers "$VIEWERS" \
        --argjson markerIntervalSeconds "$MARKER_INTERVAL_SECONDS" \
        --arg traceSeed "$TRACE_SEED" \
        --arg contractMode "$CONTRACT_MODE" \
        --arg acceptEncoding "$ACCEPT_ENCODING" \
        --arg storedEncoding "$STORED_ENCODING" \
        --arg manifestSha256 "$(sha256sum "$MANIFEST" | awk '{print $1}')" \
        --arg warmupDuration "$WARMUP_DURATION" \
        --arg measurementDuration "$MEASUREMENT_DURATION" \
        --argjson cooldownSeconds "$COOLDOWN_SECONDS" \
        --argjson minimumAchievedRateRatio "$MIN_ACHIEVED_RATE_RATIO" \
        --argjson preAllocatedVUs "$PRE_ALLOCATED_VUS" \
        --argjson maxVUs "$MAX_VUS" \
        --argjson latencyP95Milliseconds "$EFFECTIVE_LATENCY_P95_MS" \
        --argjson latencyP99Milliseconds "$EFFECTIVE_LATENCY_P99_MS" \
        '.runnerCaseId == $caseId
         and .variantId == $variantId
         and .implementation == $implementation
         and .storageType == $storageType
         and .databaseBackend == $databaseBackend
         and .replicaCount == $replicaCount
         and .profile == $profile
         and .rate == $rate
         and .viewers == $viewers
         and .markerIntervalSeconds == $markerIntervalSeconds
         and .traceSeed == $traceSeed
         and .contractMode == $contractMode
         and .acceptEncoding == $acceptEncoding
         and .storedEncoding == $storedEncoding
         and .manifestSha256 == $manifestSha256
         and .warmupDuration == $warmupDuration
         and .measurementDuration == $measurementDuration
         and .cooldownSeconds == $cooldownSeconds
         and .minimumAchievedRateRatio == $minimumAchievedRateRatio
         and .preAllocatedVUs == $preAllocatedVUs
         and .maxVUs == $maxVUs
         and .latencyP95Milliseconds == $latencyP95Milliseconds
         and .latencyP99Milliseconds == $latencyP99Milliseconds' \
        <<<"$SCHEDULE_ENTRY_JSON" >/dev/null ||
        die "Runner workload does not exactly match the selected schedule entry"
fi

EXPECTED_MAP_IDS_JSON="$(
    printf '%s\n' "${EXPECTED_MAP_IDS[@]}" |
        jq -Rsc 'split("\n")[:-1] | sort | unique'
)"
if ((${#EXPECTED_MAP_IDS[@]} != $(jq 'length' <<<"$EXPECTED_MAP_IDS_JSON"))); then
    die "Repeated --map-id values must be unique"
fi
MANIFEST_MAP_IDS_JSON="$(
    jq -ce '
        .mapIds
        | if type != "array"
             or length == 0
             or any(.[]; type != "string" or length == 0)
             or length != (unique | length)
          then error("mapIds must be a non-empty string array")
          else sort | unique
          end
    ' "$MANIFEST"
)" || die "Manifest mapIds validation failed"
[[ "$MANIFEST_MAP_IDS_JSON" == "$EXPECTED_MAP_IDS_JSON" ]] ||
    die "Manifest mapIds do not exactly match the repeated --map-id arguments"
if [[ -n "$SCHEDULE_ENTRY_JSON" ]]; then
    jq -e \
        --argjson mapIds "$MANIFEST_MAP_IDS_JSON" \
        '.mapIds == $mapIds' <<<"$SCHEDULE_ENTRY_JSON" >/dev/null ||
        die "Manifest mapIds do not match the selected formal schedule entry"
fi

CONFIGMAPS_JSON="$(
    printf '%s\n' "${CONFIGMAPS[@]}" |
        jq -Rsc 'split("\n")[:-1] | sort | unique'
)"
if ((${#CONFIGMAPS[@]} != $(jq 'length' <<<"$CONFIGMAPS_JSON"))); then
    die "Repeated --configmap names must be unique"
fi

EXPECTED_ITERATION_RATE="$RATE"
if [[ "$PROFILE" == "live-viewers" ]]; then
    marker_count="$(jq '.markers | length' "$MANIFEST")"
    EXPECTED_ITERATION_RATE="$(
        "$PYTHON_BIN" -c \
            'import sys; viewers=int(sys.argv[1]); interval=int(sys.argv[2]); markers=int(sys.argv[3]); print(viewers + (viewers / interval if markers else 0))' \
            "$VIEWERS" "$MARKER_INTERVAL_SECONDS" "$marker_count"
    )"
fi

umask 077
mkdir -p -- "$ARTIFACT_ROOT"
[[ ! -e "$ARTIFACT_ROOT/$CASE_ID" ]] ||
    die "Artifact directory '$ARTIFACT_ROOT/$CASE_ID' already exists"
mkdir -- "$ARTIFACT_ROOT/$CASE_ID"
ARTIFACT_DIR="$(cd -- "$ARTIFACT_ROOT/$CASE_ID" && pwd)"
mkdir -- \
    "$ARTIFACT_DIR/inputs" \
    "$ARTIFACT_DIR/cluster" \
    "$ARTIFACT_DIR/repetitions" \
    "$ARTIFACT_DIR/samples"

PHASE_FILE="$ARTIFACT_DIR/.current-phase"
SAMPLER_STOP_FILE="$ARTIFACT_DIR/.stop-sampler"
SAMPLER_FAILED_FILE="$ARTIFACT_DIR/.sampler-failed"
ENDPOINT_SAMPLE_FAILED_FILE="$ARTIFACT_DIR/.endpoint-sample-failed"
FAILURES_FILE="$ARTIFACT_DIR/failures.log"
printf 'setup\n' > "$PHASE_FILE"
: > "$FAILURES_FILE"

KUBECTL=(
    kubectl
    --kubeconfig "$KUBECONFIG_PATH"
    --namespace "$NAMESPACE"
)

kube() {
    "${KUBECTL[@]}" "$@"
}

timestamp() {
    date -u +'%Y-%m-%dT%H:%M:%S.%3NZ'
}

json_array() {
    if (($# == 0)); then
        printf '[]\n'
        return
    fi
    printf '%s\n' "$@" | jq -Rsc 'split("\n")[:-1]'
}

record_failure() {
    local message="$1"
    printf '%s %s\n' "$(timestamp)" "$message" | tee -a "$FAILURES_FILE" >&2
}

stop_port_forward() {
    if [[ -n "$PORT_FORWARD_PID" ]]; then
        kill "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
        wait "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
        PORT_FORWARD_PID=""
    fi
}

stop_sampler() {
    if [[ -n "$SAMPLER_PID" ]]; then
        : > "$SAMPLER_STOP_FILE"
        wait "$SAMPLER_PID" >/dev/null 2>&1 || true
        SAMPLER_PID=""
    fi
}

# Invoked through the signal/exit trap.
# shellcheck disable=SC2317,SC2329
cleanup() {
    set +e
    stop_port_forward
    stop_sampler
    if [[ -n "$DERIVED_CONFIGMAPS_FILE" ]]; then
        rm -f -- "$DERIVED_CONFIGMAPS_FILE"
    fi
}
trap cleanup EXIT INT TERM

validate_ready_pod() {
    local pod="$1"
    kube get pod "$pod" -o json |
        jq -e '
            .status.phase == "Running"
            and any(.status.conditions[]?;
                .type == "Ready" and .status == "True")
        ' >/dev/null ||
        die "Pod '$pod' is not Running and Ready"
}

validate_available_deployment() {
    local deployment="$1"
    kube get deployment "$deployment" -o json |
        jq -e '
            (.spec.replicas // 0) > 0
            and (.status.availableReplicas // 0) >= (.spec.replicas // 0)
        ' >/dev/null ||
        die "Deployment '$deployment' does not have all requested replicas available"
}

verify_service_endpoints() {
    local label="$1"
    local destination="$ARTIFACT_DIR/cluster/endpoints-$label.json"
    local payload
    local expected_pods_json
    expected_pods_json="$(
        printf '%s\n' "${WEB_PODS[@]}" |
            jq -Rsc 'split("\n")[:-1] | sort | unique'
    )"
    payload="$(
        kube get endpointslice \
            --selector "kubernetes.io/service-name=$SERVICE" \
            -o json
    )" || return 1

    jq \
        --arg capturedAt "$(timestamp)" \
        --arg service "$SERVICE" \
        --argjson expected "$expected_pods_json" \
        '{
            capturedAt: $capturedAt,
            service: $service,
            expectedReadyPods: $expected,
            allReadyEndpointsReferencePods: (
                all(
                    .items[].endpoints[]?
                    | select(
                        .conditions.ready == true
                        and (.conditions.serving // true) == true
                        and (.conditions.terminating // false) == false
                    );
                    .targetRef.kind == "Pod"
                    and (.targetRef.name | type) == "string"
                )
            ),
            endpointSlices: [
                .items[] | {
                    name: .metadata.name,
                    addressType,
                    endpoints: [
                        .endpoints[]? | {
                            targetKind: .targetRef.kind,
                            targetName: .targetRef.name,
                            conditions
                        }
                    ]
                }
            ],
            readyPods: (
                [
                    .items[].endpoints[]?
                    | select(
                        .conditions.ready == true
                        and (.conditions.serving // true) == true
                        and (.conditions.terminating // false) == false
                    )
                    | select(.targetRef.kind == "Pod")
                    | .targetRef.name
                ]
                | sort
                | unique
            )
        }' <<<"$payload" > "$destination"
    jq -e '
        .allReadyEndpointsReferencePods == true
        and .readyPods == .expectedReadyPods
    ' "$destination" >/dev/null
}

sample_service_endpoints() {
    local phase="$1"
    local destination="$ARTIFACT_DIR/samples/endpoint-membership.ndjson"
    local payload
    local expected_pods_json
    expected_pods_json="$(json_array "${WEB_PODS[@]}" | jq -cS .)"
    if ! payload="$(
        kube get endpointslice \
            --selector "kubernetes.io/service-name=$SERVICE" \
            -o json
    )"; then
        : > "$ENDPOINT_SAMPLE_FAILED_FILE"
        return
    fi

    if ! jq -c \
        --arg capturedAt "$(timestamp)" \
        --arg phase "$phase" \
        --argjson expected "$expected_pods_json" \
        '([
            .items[].endpoints[]?
            | select(
                .conditions.ready == true
                and (.conditions.serving // true) == true
                and (.conditions.terminating // false) == false
            )
            | select(.targetRef.kind == "Pod")
            | .targetRef.name
        ] | sort | unique) as $actual
        | {
            capturedAt: $capturedAt,
            phase: $phase,
            expectedReadyPods: $expected,
            readyPods: $actual,
            passed: ($actual == $expected)
        }' <<<"$payload" >> "$destination"; then
        : > "$ENDPOINT_SAMPLE_FAILED_FILE"
        return
    fi
    if ! tail -n 1 "$destination" | jq -e '.passed == true' >/dev/null; then
        : > "$ENDPOINT_SAMPLE_FAILED_FILE"
    fi
}

snapshot_resource() {
    local kind="$1"
    local name="$2"
    local destination="$3"
    local captured_at
    captured_at="$(timestamp)"
    kube get "$kind" "$name" -o json |
        "$PYTHON_BIN" "$SCRIPT_DIR/sanitize_kubernetes_resource.py" \
            --captured-at "$captured_at" > "$destination" ||
        return 1
}

snapshot_configmap() {
    local name="$1"
    local destination="$2"
    local captured_at
    captured_at="$(timestamp)"
    kube get configmap "$name" -o json |
        "$PYTHON_BIN" "$SCRIPT_DIR/sanitize_configmap.py" \
            --captured-at "$captured_at" > "$destination"
}

capture_configmap_set() {
    local label="$1"
    local directory="$ARTIFACT_DIR/cluster/$label"
    local digest_items="$ARTIFACT_DIR/cluster/.config-digests-$label.ndjson"
    : > "$digest_items"

    for configmap in "${CONFIGMAPS[@]}"; do
        local snapshot_file="$directory/configmap-$configmap.json"
        local digest
        snapshot_configmap "$configmap" "$snapshot_file" || return 1
        digest="$(
            jq -cS '.resource.data' "$snapshot_file" |
                sha256sum |
                awk '{print $1}'
        )" || return 1
        jq -nc \
            --arg name "$configmap" \
            --arg sha256 "$digest" \
            '{name: $name, sanitizedDataSha256: $sha256}' \
            >> "$digest_items" || return 1
    done

    jq -s \
        --arg capturedAt "$(timestamp)" \
        '{capturedAt: $capturedAt, configMaps: (sort_by(.name))}' \
        "$digest_items" > "$ARTIFACT_DIR/cluster/config-digests-$label.json" ||
        return 1
    rm -f -- "$digest_items"
}

capture_snapshot_set() {
    local label="$1"
    local directory="$ARTIFACT_DIR/cluster/$label"
    mkdir -- "$directory" || return 1

    snapshot_resource service "$SERVICE" "$directory/service-$SERVICE.json" ||
        return 1
    for deployment in "${WEB_DEPLOYMENTS[@]}"; do
        snapshot_resource deployment "$deployment" \
            "$directory/deployment-$deployment.json" ||
            return 1
    done
    for target in "${SAMPLE_TARGETS[@]}"; do
        local pod="${target#*:}"
        snapshot_resource pod "$pod" "$directory/pod-$pod.json" ||
            return 1
    done
    capture_configmap_set "$label" || return 1

    jq -s '{
        pods: map(.resource | {
            pod: .metadata.name,
            uid: .metadata.uid,
            nodeName: .spec.nodeName,
            containers: [
                .status.containerStatuses[]? | {
                    name,
                    image,
                    imageID,
                    ready,
                    restartCount
                }
            ]
        })
    }' "$directory"/pod-*.json > "$ARTIFACT_DIR/cluster/images-$label.json" ||
        return 1
}

capture_restart_counts() {
    local destination="$1"
    local items_file="${destination%.json}.items.ndjson"
    : > "$items_file"

    for target in "${SAMPLE_TARGETS[@]}"; do
        local role="${target%%:*}"
        local pod="${target#*:}"
        kube get pod "$pod" -o json |
            jq -c \
                --arg capturedAt "$(timestamp)" \
                --arg role "$role" \
                '{
                    capturedAt: $capturedAt,
                    role: $role,
                    pod: .metadata.name,
                    uid: .metadata.uid,
                    containers: [
                        .status.containerStatuses[]? | {
                            name,
                            image,
                            imageID,
                            ready,
                            restartCount
                        }
                    ]
                }' >> "$items_file" ||
            return 1
    done

    jq -s '{pods: .}' "$items_file" > "$destination" || return 1
    rm -f -- "$items_file"
}

normalized_restarts() {
    jq -S '[
        .pods[] |
        .pod as $pod |
        .containers[] |
        {
            pod: $pod,
            name,
            restartCount
        }
    ] | sort_by(.pod, .name)' "$1"
}

record_phase_event() {
    local repetition="$1"
    local phase="$2"
    local event="$3"
    jq -nc \
        --arg timestamp "$(timestamp)" \
        --argjson repetition "$repetition" \
        --arg phase "$phase" \
        --arg event "$event" \
        '{
            timestamp: $timestamp,
            repetition: $repetition,
            phase: $phase,
            event: $event
        }' >> "$ARTIFACT_DIR/phases.ndjson"
}

set_phase() {
    local repetition="$1"
    local phase="$2"
    printf 'repetition-%02d/%s\n' "$repetition" "$phase" > "$PHASE_FILE"
}

sample_metrics() {
    local output="$ARTIFACT_DIR/samples/resource-usage.ndjson"
    local errors="$ARTIFACT_DIR/samples/resource-errors.ndjson"
    local stderr_log="$ARTIFACT_DIR/samples/metrics-api.stderr.log"
    : > "$output"
    : > "$errors"
    : > "$stderr_log"

    while [[ ! -f "$SAMPLER_STOP_FILE" ]]; do
        local captured_at
        local phase
        captured_at="$(timestamp)"
        phase="$(<"$PHASE_FILE")"

        for target in "${SAMPLE_TARGETS[@]}"; do
            local role="${target%%:*}"
            local pod="${target#*:}"
            local endpoint="/apis/metrics.k8s.io/v1beta1/namespaces/$NAMESPACE/pods/$pod"
            local payload

            if payload="$("${KUBECTL[@]}" get --raw "$endpoint" 2>>"$stderr_log")"; then
                if ! jq -c \
                    --arg capturedAt "$captured_at" \
                    --arg phase "$phase" \
                    --arg role "$role" \
                    --arg expectedPod "$pod" \
                    'if .metadata.name != $expectedPod then
                        error("metrics response Pod did not match exact target")
                    elif (.containers | type) != "array" then
                        error("metrics response has no container array")
                    else
                        {
                            capturedAt: $capturedAt,
                            phase: $phase,
                            role: $role,
                            pod: .metadata.name,
                            expectedPod: $expectedPod,
                            metricTimestamp: .timestamp,
                            window: .window,
                            containers: [
                                .containers[] | {
                                    name,
                                    cpu: .usage.cpu,
                                    memory: .usage.memory
                                }
                            ]
                        }
                    end' <<<"$payload" >> "$output"; then
                    : > "$SAMPLER_FAILED_FILE"
                    jq -nc \
                        --arg capturedAt "$captured_at" \
                        --arg phase "$phase" \
                        --arg role "$role" \
                        --arg pod "$pod" \
                        --arg error "invalid metrics.k8s.io response" \
                        '{
                            capturedAt: $capturedAt,
                            phase: $phase,
                            role: $role,
                            pod: $pod,
                            error: $error
                        }' >> "$errors"
                fi
            else
                : > "$SAMPLER_FAILED_FILE"
                jq -nc \
                    --arg capturedAt "$captured_at" \
                    --arg phase "$phase" \
                    --arg role "$role" \
                    --arg pod "$pod" \
                    --arg error "metrics.k8s.io request failed" \
                    '{
                        capturedAt: $capturedAt,
                        phase: $phase,
                        role: $role,
                        pod: $pod,
                        error: $error
                    }' >> "$errors"
            fi
        done
        if [[ "$phase" == */measurement ]]; then
            sample_service_endpoints "$phase"
        fi

        sleep "$METRICS_INTERVAL_SECONDS"
    done
}

find_free_local_port() {
    "$PYTHON_BIN" -c '
import socket
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
'
}

wait_for_port_forward() {
    local process_id="$1"
    local log_file="$2"

    for _ in {1..100}; do
        if grep -q 'Forwarding from 127.0.0.1:' "$log_file"; then
            return 0
        fi
        if ! kill -0 "$process_id" >/dev/null 2>&1; then
            return 1
        fi
        sleep 0.1
    done
    return 1
}

run_contract_check() {
    local repetition="$1"
    local repetition_dir="$2"
    local local_port
    local_port="$(find_free_local_port)"
    local forward_log="$repetition_dir/contract-port-forward.log"
    local contract_log="$repetition_dir/contract.log"

    kube port-forward \
        --address 127.0.0.1 \
        "service/$SERVICE" \
        "$local_port:$SERVICE_PORT" >"$forward_log" 2>&1 &
    PORT_FORWARD_PID=$!

    if ! wait_for_port_forward "$PORT_FORWARD_PID" "$forward_log"; then
        stop_port_forward
        record_failure "Repetition $repetition: port-forward readiness failed"
        return 1
    fi

    set +e
    "$PYTHON_BIN" "$CONTRACT_SCRIPT" \
        "http://127.0.0.1:$local_port" \
        "$MANIFEST" \
        --mode "$CONTRACT_MODE" \
        --stored-encoding "$STORED_ENCODING" \
        --user-agent "BlueMap-Contract-Check/$CASE_ID-r$repetition" \
        2>&1 | tee "$contract_log"
    local status="${PIPESTATUS[0]}"
    set -e
    stop_port_forward

    if ((status != 0)); then
        record_failure \
            "Repetition $repetition: HTTP correctness gate failed with exit $status"
        return 1
    fi
}

capture_prometheus_metrics() {
    [[ -n "$PROMETHEUS_URL" ]] || return 0
    [[ -n "$CASE_START_EPOCH" && -n "$CASE_END_EPOCH" ]] || {
        record_failure "Prometheus capture has no bounded case timestamps"
        return 1
    }

    local query_url
    query_url="$(jq -r '.baseUrl' <<<"$PROMETHEUS_INSPECTION")"
    local prometheus_service
    prometheus_service="$(
        jq -r '.clusterService.service // empty' <<<"$PROMETHEUS_INSPECTION"
    )"
    local forward_log="$ARTIFACT_DIR/samples/prometheus-port-forward.log"

    if [[ -n "$prometheus_service" ]]; then
        local prometheus_namespace
        local prometheus_port
        local prometheus_scheme
        local prometheus_path
        local local_port
        prometheus_namespace="$(
            jq -r '.clusterService.namespace' <<<"$PROMETHEUS_INSPECTION"
        )"
        prometheus_port="$(
            jq -r '.clusterService.port' <<<"$PROMETHEUS_INSPECTION"
        )"
        prometheus_scheme="$(jq -r '.scheme' <<<"$PROMETHEUS_INSPECTION")"
        prometheus_path="$(jq -r '.path' <<<"$PROMETHEUS_INSPECTION")"
        local_port="$(find_free_local_port)"

        kubectl \
            --kubeconfig "$KUBECONFIG_PATH" \
            --namespace "$prometheus_namespace" \
            port-forward \
            --address 127.0.0.1 \
            "service/$prometheus_service" \
            "$local_port:$prometheus_port" >"$forward_log" 2>&1 &
        PORT_FORWARD_PID=$!
        if ! wait_for_port_forward "$PORT_FORWARD_PID" "$forward_log"; then
            stop_port_forward
            record_failure "Prometheus Service port-forward readiness failed"
            return 1
        fi
        query_url="$prometheus_scheme://127.0.0.1:$local_port$prometheus_path"
    fi

    local -a capture_arguments=(
        capture
        --base-url "$query_url"
        --source-url "$(jq -r '.baseUrl' <<<"$PROMETHEUS_INSPECTION")"
        --start "$CASE_START_EPOCH"
        --end "$CASE_END_EPOCH"
        --step "$PROMETHEUS_STEP_SECONDS"
        --namespace "$NAMESPACE"
        --output "$ARTIFACT_DIR/samples/prometheus-query-range.json"
    )
    for target in "${SAMPLE_TARGETS[@]}"; do
        capture_arguments+=(--pod "${target/:/=}")
    done
    for node in "${TARGET_NODES[@]}"; do
        capture_arguments+=(--node "$node")
    done
    capture_arguments+=(
        --phase-events "$ARTIFACT_DIR/phases.ndjson"
        --max-non-target-node-cpu-range-cores \
        "$MAX_NON_TARGET_NODE_CPU_RANGE_CORES"
        --max-non-target-node-cpu-mean-cores \
        "$MAX_NON_TARGET_NODE_CPU_MEAN_CORES"
        --max-non-target-node-cpu-maximum-cores \
        "$MAX_NON_TARGET_NODE_CPU_MAXIMUM_CORES"
    )

    if ! "$PYTHON_BIN" "$SCRIPT_DIR/capture_prometheus.py" \
        "${capture_arguments[@]}" \
        2>"$ARTIFACT_DIR/samples/prometheus-capture.stderr.log"; then
        stop_port_forward
        record_failure "Prometheus query_range capture failed"
        return 1
    fi
    stop_port_forward

    if ! jq -e '.nodeNoise.passed == true' \
        "$ARTIFACT_DIR/samples/prometheus-query-range.json" >/dev/null; then
        local noisy_repetitions
        noisy_repetitions="$(
            jq -c '.nodeNoise.noisyRepetitions' \
                "$ARTIFACT_DIR/samples/prometheus-query-range.json"
        )"
        record_failure \
            "Prometheus flagged noisy or incomplete node samples in repetitions $noisy_repetitions"
        return 1
    fi
}

copy_remote_file() {
    local remote="$1"
    local local_file="$2"
    kube exec "pod/$LOADGEN_POD" -c k6 -- test -f "$remote" >/dev/null 2>&1 ||
        return 1
    kube exec "pod/$LOADGEN_POD" -c k6 -- cat "$remote" > "$local_file"
    [[ -s "$local_file" ]]
}

validate_arrival_gate() {
    local summary="$1"
    local destination="$2"
    local duration="$3"
    local -a arguments=(
        "$summary"
        --output "$destination"
        --profile "$PROFILE"
        --rate "$RATE"
        --viewers "$VIEWERS"
        --marker-interval-seconds "$MARKER_INTERVAL_SECONDS"
        --duration "$duration"
        --minimum-achieved-ratio "$MIN_ACHIEVED_RATE_RATIO"
    )
    if [[ "$PROFILE" == "live-viewers" ]] &&
        (($(jq '.markers | length' "$MANIFEST") > 0)); then
        arguments+=(--markers-present)
    fi
    "$PYTHON_BIN" "$SCRIPT_DIR/check_arrival_gate.py" "${arguments[@]}"
}

validate_latency_gate() {
    local summary="$1"
    local destination="$2"

    jq \
        --argjson maximumP95Milliseconds "$EFFECTIVE_LATENCY_P95_MS" \
        --argjson maximumP99Milliseconds "$EFFECTIVE_LATENCY_P99_MS" \
        '(.metrics["http_req_duration{traffic:workload}"] // {}) as $metric
        | ($metric.values // $metric) as $values
        | {
            maximumP95Milliseconds: $maximumP95Milliseconds,
            maximumP99Milliseconds: $maximumP99Milliseconds,
            observedP95Milliseconds: $values["p(95)"],
            observedP99Milliseconds: $values["p(99)"],
            passed: (
                ($values["p(95)"] | type) == "number"
                and ($values["p(99)"] | type) == "number"
                and $values["p(95)"] < $maximumP95Milliseconds
                and $values["p(99)"] < $maximumP99Milliseconds
            )
        }' "$summary" > "$destination" || return 1

    jq -e '.passed == true' "$destination" >/dev/null
}

run_k6_phase() {
    local repetition="$1"
    local phase="$2"
    local duration="$3"
    local repetition_name
    repetition_name="$(printf '%02d' "$repetition")"
    local local_dir="$ARTIFACT_DIR/repetitions/$repetition_name/$phase"
    local remote_dir="/artifacts/$CASE_ID/repetitions/$repetition_name/$phase"
    local remote_summary="$remote_dir/summary.json"
    local remote_raw="$remote_dir/raw.ndjson"
    mkdir -- "$local_dir" || return 1
    if ! verify_service_endpoints \
        "repetition-$repetition_name-$phase-start"; then
        record_failure \
            "Repetition $repetition $phase: ready EndpointSlice targets changed at phase start"
        return 1
    fi
    # $1 is expanded by the shell inside the load-generator container.
    # shellcheck disable=SC2016
    kube exec "pod/$LOADGEN_POD" -c k6 -- \
        sh -ceu 'mkdir "$1"' sh "$remote_dir" ||
        return 1

    set_phase "$repetition" "$phase"
    record_phase_event "$repetition" "$phase" "start"

    local enforce_latency_gates="false"
    if [[ "$phase" == "measurement" ]]; then
        enforce_latency_gates="true"
    fi

    set +e
    kube exec "pod/$LOADGEN_POD" -c k6 -- \
        env K6_NO_USAGE_REPORT=true K6_NO_COLOR=true \
        k6 run \
        --summary-export "$remote_summary" \
        --out "json=$remote_raw" \
        -e "BASE_URL=$BASE_URL" \
        -e "MANIFEST=/artifacts/$CASE_ID/inputs/manifest.json" \
        -e "PROFILE=$PROFILE" \
        -e "RATE=$RATE" \
        -e "DURATION=$duration" \
        -e "PRE_ALLOCATED_VUS=$PRE_ALLOCATED_VUS" \
        -e "MAX_VUS=$MAX_VUS" \
        -e "VIEWERS=$VIEWERS" \
        -e "MARKER_INTERVAL_SECONDS=$MARKER_INTERVAL_SECONDS" \
        -e "MIN_ACHIEVED_RATE_RATIO=$MIN_ACHIEVED_RATE_RATIO" \
        -e "TRACE_SEED=$TRACE_SEED" \
        -e "LATENCY_P95_MS=$LATENCY_P95_MS" \
        -e "LATENCY_P99_MS=$LATENCY_P99_MS" \
        -e "LARGE_OBJECT_LATENCY_P95_MS=$LARGE_OBJECT_LATENCY_P95_MS" \
        -e "LARGE_OBJECT_LATENCY_P99_MS=$LARGE_OBJECT_LATENCY_P99_MS" \
        -e "ENFORCE_LATENCY_GATES=$enforce_latency_gates" \
        -e "EXPERIMENT_ID=$CASE_ID-r$repetition_name-$phase" \
        -e "ACCEPT_ENCODING=$ACCEPT_ENCODING" \
        -e "CONTRACT_MODE=$CONTRACT_MODE" \
        "/artifacts/$CASE_ID/inputs/bluemap.js" \
        2>&1 | tee "$local_dir/console.log"
    local status="${PIPESTATUS[0]}"
    set -e
    printf '%s\n' "$status" > "$local_dir/exit-status.txt"

    local artifact_failure=0
    if ! copy_remote_file "$remote_summary" "$local_dir/summary.json"; then
        record_failure \
            "Repetition $repetition $phase: k6 summary artifact is missing"
        artifact_failure=1
    fi
    if ! copy_remote_file "$remote_raw" "$local_dir/raw.ndjson"; then
        record_failure \
            "Repetition $repetition $phase: raw k6 metric output is missing"
        artifact_failure=1
    fi
    if [[ -s "$local_dir/summary.json" ]] &&
        ! validate_arrival_gate \
            "$local_dir/summary.json" \
            "$local_dir/arrival-gate.json" \
            "$duration"; then
        record_failure \
            "Repetition $repetition $phase: scheduled/completed or dropped-iteration gate failed"
        artifact_failure=1
    fi
    if [[ "$phase" == "measurement" &&
        -s "$local_dir/summary.json" ]] &&
        ! validate_latency_gate \
            "$local_dir/summary.json" \
            "$local_dir/latency-gate.json"; then
        record_failure \
            "Repetition $repetition: p95/p99 latency gate failed"
        artifact_failure=1
    fi

    record_phase_event "$repetition" "$phase" "end"
    if ! verify_service_endpoints \
        "repetition-$repetition_name-$phase-end"; then
        record_failure \
            "Repetition $repetition $phase: ready EndpointSlice targets changed at phase end"
        artifact_failure=1
    fi
    if ((status != 0)); then
        record_failure \
            "Repetition $repetition $phase: k6 checks or thresholds failed with exit $status"
        return 1
    fi
    ((artifact_failure == 0))
}

write_workload_metadata() {
    local web_deployments_json
    local web_pods_json
    local database_pods_json
    local target_nodes_json
    local derived_configmaps_json
    local prometheus_enabled=false
    local prometheus_url=""
    local schedule_enabled=false
    local variant_metadata_enabled=false
    local matrix_sha256=""
    local schedule_sha256=""
    web_deployments_json="$(
        json_array "${WEB_DEPLOYMENTS[@]}"
    )"
    web_pods_json="$(
        json_array "${WEB_PODS[@]}"
    )"
    database_pods_json="$(
        json_array "${DATABASE_PODS[@]}"
    )"
    target_nodes_json="$(
        json_array "${TARGET_NODES[@]}"
    )"
    derived_configmaps_json="$(
        json_array "${DERIVED_CONFIGMAPS[@]}"
    )"
    if [[ -n "$PROMETHEUS_URL" ]]; then
        prometheus_enabled=true
        prometheus_url="$(jq -r '.baseUrl' <<<"$PROMETHEUS_INSPECTION")"
    fi
    if [[ -n "$SCHEDULE_ENTRY_JSON" ]]; then
        schedule_enabled=true
        matrix_sha256="$(sha256sum "$MATRIX" | awk '{print $1}')"
        schedule_sha256="$(sha256sum "$SCHEDULE" | awk '{print $1}')"
    fi
    if [[ -n "$VARIANT_ID" ]]; then
        variant_metadata_enabled=true
    fi

    jq -n \
        --arg capturedAt "$(timestamp)" \
        --arg caseId "$CASE_ID" \
        --arg namespace "$NAMESPACE" \
        --arg service "$SERVICE" \
        --argjson servicePort "$SERVICE_PORT" \
        --arg baseUrl "$BASE_URL" \
        --arg profile "$PROFILE" \
        --argjson rate "$RATE" \
        --argjson viewers "$VIEWERS" \
        --argjson markerIntervalSeconds "$MARKER_INTERVAL_SECONDS" \
        --argjson preAllocatedVUs "$PRE_ALLOCATED_VUS" \
        --argjson maxVUs "$MAX_VUS" \
        --argjson minimumAchievedRateRatio "$MIN_ACHIEVED_RATE_RATIO" \
        --arg traceSeed "$TRACE_SEED" \
        --argjson latencyP95Milliseconds "$LATENCY_P95_MS" \
        --argjson latencyP99Milliseconds "$LATENCY_P99_MS" \
        --arg largeObjectLatencyP95Milliseconds "$LARGE_OBJECT_LATENCY_P95_MS" \
        --arg largeObjectLatencyP99Milliseconds "$LARGE_OBJECT_LATENCY_P99_MS" \
        --argjson effectiveLatencyP95Milliseconds "$EFFECTIVE_LATENCY_P95_MS" \
        --argjson effectiveLatencyP99Milliseconds "$EFFECTIVE_LATENCY_P99_MS" \
        --argjson offeredIterationsPerSecond "$EXPECTED_ITERATION_RATE" \
        --arg acceptEncoding "$ACCEPT_ENCODING" \
        --arg storedEncoding "$STORED_ENCODING" \
        --arg contractMode "$CONTRACT_MODE" \
        --arg warmup "$WARMUP_DURATION" \
        --arg measurement "$MEASUREMENT_DURATION" \
        --argjson cooldownSeconds "$COOLDOWN_SECONDS" \
        --argjson repetitions "$REPETITIONS" \
        --argjson metricsIntervalSeconds "$METRICS_INTERVAL_SECONDS" \
        --argjson prometheusEnabled "$prometheus_enabled" \
        --arg prometheusUrl "$prometheus_url" \
        --argjson prometheusStepSeconds "$PROMETHEUS_STEP_SECONDS" \
        --argjson maximumNonTargetNodeCpuRangeCores "$MAX_NON_TARGET_NODE_CPU_RANGE_CORES" \
        --argjson maximumNonTargetNodeCpuMeanCores "$MAX_NON_TARGET_NODE_CPU_MEAN_CORES" \
        --argjson maximumNonTargetNodeCpuLevelCores "$MAX_NON_TARGET_NODE_CPU_MAXIMUM_CORES" \
        --argjson scheduleEnabled "$schedule_enabled" \
        --argjson variantMetadataEnabled "$variant_metadata_enabled" \
        --arg variantId "$VARIANT_ID" \
        --arg implementation "$IMPLEMENTATION" \
        --arg storageType "$STORAGE_TYPE" \
        --arg databaseBackend "$DATABASE_BACKEND" \
        --argjson desiredWebReplicaCount "$DESIRED_WEB_REPLICA_COUNT" \
        --argjson namedWebPodCount "${#WEB_PODS[@]}" \
        --arg scheduleEntryId "$SCHEDULE_ENTRY_ID" \
        --arg matrixSha256 "$matrix_sha256" \
        --arg scheduleSha256 "$schedule_sha256" \
        --argjson scheduleEntry "${SCHEDULE_ENTRY_JSON:-null}" \
        --arg pythonCommand "$PYTHON_BIN" \
        --arg benchmarkCommit "$BENCHMARK_COMMIT" \
        --arg manifestSha256 "$(sha256sum "$MANIFEST" | awk '{print $1}')" \
        --arg k6ScriptSha256 "$(sha256sum "$K6_SCRIPT" | awk '{print $1}')" \
        --arg contractScriptSha256 "$(sha256sum "$CONTRACT_SCRIPT" | awk '{print $1}')" \
        --arg runnerSha256 "$(sha256sum "${BASH_SOURCE[0]}" | awk '{print $1}')" \
        --arg configSanitizerSha256 "$(sha256sum "$SCRIPT_DIR/sanitize_configmap.py" | awk '{print $1}')" \
        --arg configMapReferencesSha256 "$(sha256sum "$SCRIPT_DIR/configmap_references.py" | awk '{print $1}')" \
        --arg arrivalGateSha256 "$(sha256sum "$SCRIPT_DIR/check_arrival_gate.py" | awk '{print $1}')" \
        --arg slowReaderSha256 "$(sha256sum "$SCRIPT_DIR/slow_reader.py" | awk '{print $1}')" \
        --argjson mapIds "$MANIFEST_MAP_IDS_JSON" \
        --argjson configMaps "$CONFIGMAPS_JSON" \
        --argjson derivedConfigMaps "$derived_configmaps_json" \
        --argjson webDeployments "$web_deployments_json" \
        --argjson webPods "$web_pods_json" \
        --argjson databasePods "$database_pods_json" \
        --argjson nodes "$target_nodes_json" \
        '{
            capturedAt: $capturedAt,
            caseId: $caseId,
            namespace: $namespace,
            origin: {
                service: $service,
                port: $servicePort,
                baseUrl: $baseUrl
            },
            workload: {
                profile: $profile,
                rate: $rate,
                viewers: $viewers,
                markerIntervalSeconds: $markerIntervalSeconds,
                preAllocatedVUs: $preAllocatedVUs,
                maxVUs: $maxVUs,
                minimumAchievedRateRatio: $minimumAchievedRateRatio,
                traceSeed: $traceSeed,
                latencyGates: {
                    p95Milliseconds: $latencyP95Milliseconds,
                    p99Milliseconds: $latencyP99Milliseconds,
                    largeObjectP95Milliseconds: (
                        if $largeObjectLatencyP95Milliseconds == ""
                        then null
                        else ($largeObjectLatencyP95Milliseconds | tonumber)
                        end
                    ),
                    largeObjectP99Milliseconds: (
                        if $largeObjectLatencyP99Milliseconds == ""
                        then null
                        else ($largeObjectLatencyP99Milliseconds | tonumber)
                        end
                    ),
                    effectiveP95Milliseconds: $effectiveLatencyP95Milliseconds,
                    effectiveP99Milliseconds: $effectiveLatencyP99Milliseconds
                },
                offeredIterationsPerSecond: $offeredIterationsPerSecond,
                acceptEncoding: $acceptEncoding,
                storedEncoding: $storedEncoding,
                contractMode: $contractMode,
                warmup: $warmup,
                measurement: $measurement,
                cooldownSeconds: $cooldownSeconds,
                repetitions: $repetitions,
                metricsIntervalSeconds: $metricsIntervalSeconds
            },
            observability: {
                metricsKubernetes: {
                    enabled: true,
                    intervalSeconds: $metricsIntervalSeconds
                },
                prometheus: {
                    enabled: $prometheusEnabled,
                    baseUrl: (
                        if $prometheusEnabled then $prometheusUrl else null end
                    ),
                    stepSeconds: (
                        if $prometheusEnabled then $prometheusStepSeconds else null end
                    ),
                    maximumNonTargetNodeCpuRangeCores: (
                        if $prometheusEnabled
                        then $maximumNonTargetNodeCpuRangeCores
                        else null
                    ),
                    maximumNonTargetNodeCpuMeanCores: (
                        if $prometheusEnabled
                        then $maximumNonTargetNodeCpuMeanCores
                        else null
                    ),
                    maximumNonTargetNodeCpuLevelCores: (
                        if $prometheusEnabled
                        then $maximumNonTargetNodeCpuLevelCores
                        else null
                    )
                }
            },
            formalSchedule: {
                enabled: $scheduleEnabled,
                entryId: (if $scheduleEnabled then $scheduleEntryId else null end),
                matrixSha256: (if $scheduleEnabled then $matrixSha256 else null end),
                scheduleSha256: (
                    if $scheduleEnabled then $scheduleSha256 else null end
                ),
                entry: (if $scheduleEnabled then $scheduleEntry else null end)
            },
            variant: {
                enabled: $variantMetadataEnabled,
                id: (if $variantMetadataEnabled then $variantId else null end),
                implementation: (
                    if $variantMetadataEnabled then $implementation else null end
                ),
                storageType: (
                    if $variantMetadataEnabled then $storageType else null end
                ),
                databaseBackend: (
                    if $variantMetadataEnabled then $databaseBackend else null end
                ),
                scheduledReplicaCount: (
                    if $scheduleEnabled then $scheduleEntry.replicaCount else null end
                ),
                desiredDeploymentReplicaCount: $desiredWebReplicaCount,
                namedWebPodCount: $namedWebPodCount
            },
            runtime: {
                pythonCommand: $pythonCommand
            },
            targets: {
                mapIds: $mapIds,
                configMaps: $configMaps,
                derivedConfigMaps: $derivedConfigMaps,
                webDeployments: $webDeployments,
                webPods: $webPods,
                databasePods: $databasePods,
                nodes: $nodes
            },
            source: {
                benchmarkCommit: $benchmarkCommit,
                manifestSha256: $manifestSha256,
                k6ScriptSha256: $k6ScriptSha256,
                contractScriptSha256: $contractScriptSha256,
                runnerSha256: $runnerSha256,
                configSanitizerSha256: $configSanitizerSha256,
                configMapReferencesSha256: $configMapReferencesSha256,
                arrivalGateSha256: $arrivalGateSha256,
                slowReaderSha256: $slowReaderSha256
            }
        }' > "$ARTIFACT_DIR/inputs/workload.json"
}

SAMPLE_TARGETS=("loadgen:$LOADGEN_POD")
ALL_PODS=("$LOADGEN_POD")
for pod in "${WEB_PODS[@]}"; do
    SAMPLE_TARGETS+=("web:$pod")
    ALL_PODS+=("$pod")
done
for pod in "${DATABASE_PODS[@]}"; do
    SAMPLE_TARGETS+=("database:$pod")
    ALL_PODS+=("$pod")
done

if (($(printf '%s\n' "${ALL_PODS[@]}" | sort | uniq -d | wc -l) > 0)); then
    die "Pod targets must be distinct"
fi

BASE_URL="http://$SERVICE.$NAMESPACE.svc.cluster.local:$SERVICE_PORT"
BENCHMARK_COMMIT="$(git -C "$REPOSITORY_ROOT" rev-parse HEAD 2>/dev/null || printf 'unknown')"

kube get service "$SERVICE" -o name >/dev/null
validate_ready_pod "$LOADGEN_POD"
for deployment in "${WEB_DEPLOYMENTS[@]}"; do
    validate_available_deployment "$deployment"
    deployment_replicas="$(
        kube get deployment "$deployment" -o json |
            jq -er '.spec.replicas // 0'
    )" || die "Could not determine desired replicas for Deployment '$deployment'"
    [[ "$deployment_replicas" =~ ^[1-9][0-9]*$ ]] ||
        die "Deployment '$deployment' does not request a positive replica count"
    DESIRED_WEB_REPLICA_COUNT="$(
        "$PYTHON_BIN" -c \
            'import sys; print(int(sys.argv[1]) + int(sys.argv[2]))' \
            "$DESIRED_WEB_REPLICA_COUNT" "$deployment_replicas"
    )"
done
((DESIRED_WEB_REPLICA_COUNT == ${#WEB_PODS[@]})) ||
    die "Named web Pod count does not equal the selected Deployments' desired replicas"
if [[ -n "$SCHEDULE_ENTRY_JSON" ]]; then
    scheduled_replica_count="$(jq -r '.replicaCount' <<<"$SCHEDULE_ENTRY_JSON")"
    ((DESIRED_WEB_REPLICA_COUNT == scheduled_replica_count)) ||
        die "Selected Deployments' desired replicas do not match the formal schedule"
fi
for pod in "${WEB_PODS[@]}" "${DATABASE_PODS[@]}"; do
    validate_ready_pod "$pod"
done
for pod in "${ALL_PODS[@]}"; do
    node="$(
        kube get pod "$pod" -o jsonpath='{.spec.nodeName}'
    )"
    [[ "$node" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] ||
        die "Pod '$pod' has no valid assigned node"
    TARGET_NODES+=("$node")
done
mapfile -t TARGET_NODES < <(printf '%s\n' "${TARGET_NODES[@]}" | sort -u)

DERIVED_CONFIGMAPS_FILE="$(mktemp)"
for deployment in "${WEB_DEPLOYMENTS[@]}"; do
    kube get deployment "$deployment" -o json |
        "$PYTHON_BIN" "$SCRIPT_DIR/configmap_references.py" |
        jq -r '.[]' >> "$DERIVED_CONFIGMAPS_FILE"
done
for pod in "${WEB_PODS[@]}"; do
    kube get pod "$pod" -o json |
        "$PYTHON_BIN" "$SCRIPT_DIR/configmap_references.py" |
        jq -r '.[]' >> "$DERIVED_CONFIGMAPS_FILE"
done
mapfile -t DERIVED_CONFIGMAPS < <(sort -u "$DERIVED_CONFIGMAPS_FILE")
rm -f -- "$DERIVED_CONFIGMAPS_FILE"
DERIVED_CONFIGMAPS_FILE=""

missing_configmaps="$(
    comm -23 \
        <(printf '%s\n' "${DERIVED_CONFIGMAPS[@]}" | sort -u) \
        <(printf '%s\n' "${CONFIGMAPS[@]}" | sort -u)
)"
[[ -z "$missing_configmaps" ]] ||
    die "Referenced ConfigMaps missing from --configmap: ${missing_configmaps//$'\n'/, }"

for configmap in "${CONFIGMAPS[@]}"; do
    kube get configmap "$configmap" -o name >/dev/null
done

cp -- "$MANIFEST" "$ARTIFACT_DIR/inputs/manifest.json"
cp -- "$K6_SCRIPT" "$ARTIFACT_DIR/inputs/bluemap.js"
cp -- "$CONTRACT_SCRIPT" "$ARTIFACT_DIR/inputs/check_http_contract.py"
cp -- "${BASH_SOURCE[0]}" "$ARTIFACT_DIR/inputs/run_origin_case.sh"
cp -- "$SCRIPT_DIR/sanitize_kubernetes_resource.py" \
    "$ARTIFACT_DIR/inputs/sanitize_kubernetes_resource.py"
cp -- "$SCRIPT_DIR/sanitize_configmap.py" \
    "$ARTIFACT_DIR/inputs/sanitize_configmap.py"
cp -- "$SCRIPT_DIR/configmap_references.py" \
    "$ARTIFACT_DIR/inputs/configmap_references.py"
cp -- "$SCRIPT_DIR/capture_prometheus.py" \
    "$ARTIFACT_DIR/inputs/capture_prometheus.py"
cp -- "$SCRIPT_DIR/check_arrival_gate.py" \
    "$ARTIFACT_DIR/inputs/check_arrival_gate.py"
cp -- "$SCRIPT_DIR/slow_reader.py" \
    "$ARTIFACT_DIR/inputs/slow_reader.py"
cp -- "$SCRIPT_DIR/generate_schedule.py" \
    "$ARTIFACT_DIR/inputs/generate_schedule.py"
if [[ -n "$SCHEDULE_ENTRY_JSON" ]]; then
    cp -- "$MATRIX" "$ARTIFACT_DIR/inputs/matrix.json"
    cp -- "$SCHEDULE" "$ARTIFACT_DIR/inputs/schedule.json"
    printf '%s\n' "$SCHEDULE_ENTRY_JSON" |
        jq -S . > "$ARTIFACT_DIR/inputs/schedule-entry.json"
fi
write_workload_metadata
(
    cd -- "$ARTIFACT_DIR/inputs"
    sha256sum \
        manifest.json \
        bluemap.js \
        check_http_contract.py \
        run_origin_case.sh \
        sanitize_kubernetes_resource.py \
        sanitize_configmap.py \
        configmap_references.py \
        capture_prometheus.py \
        check_arrival_gate.py \
        slow_reader.py \
        generate_schedule.py \
        workload.json > SHA256SUMS
    if [[ -n "$SCHEDULE_ENTRY_JSON" ]]; then
        sha256sum matrix.json schedule.json schedule-entry.json >> SHA256SUMS
    fi
)

REMOTE_ROOT="/artifacts/$CASE_ID"
# $1 is expanded by the shell inside the load-generator container.
# shellcheck disable=SC2016
kube exec "pod/$LOADGEN_POD" -c k6 -- \
    sh -ceu \
    'umask 077; mkdir "$1"; mkdir "$1/inputs" "$1/repetitions"' \
    sh "$REMOTE_ROOT"
# $1 is expanded by the shell inside the load-generator container.
# shellcheck disable=SC2016
kube exec -i "pod/$LOADGEN_POD" -c k6 -- \
    sh -ceu 'test ! -e "$1"; cat > "$1"' \
    sh "$REMOTE_ROOT/inputs/manifest.json" < "$MANIFEST"
# $1 is expanded by the shell inside the load-generator container.
# shellcheck disable=SC2016
kube exec -i "pod/$LOADGEN_POD" -c k6 -- \
    sh -ceu 'test ! -e "$1"; cat > "$1"' \
    sh "$REMOTE_ROOT/inputs/bluemap.js" < "$K6_SCRIPT"

capture_snapshot_set before
verify_service_endpoints before ||
    die "Ready EndpointSlice Pod targets do not exactly match --web-pod targets"
capture_restart_counts "$ARTIFACT_DIR/cluster/restarts-case-before.json"

CASE_START_EPOCH="$(date -u +%s)"
CASE_START_TIMESTAMP="$(timestamp)"
record_phase_event 0 "case" "start"
sample_metrics &
SAMPLER_PID=$!

case_failed=0
completed_repetitions=0
for ((repetition = 1; repetition <= REPETITIONS; repetition++)); do
    repetition_name="$(printf '%02d' "$repetition")"
    repetition_dir="$ARTIFACT_DIR/repetitions/$repetition_name"
    mkdir -- "$repetition_dir"
    capture_restart_counts "$repetition_dir/restarts-before.json"

    if ! verify_service_endpoints "repetition-$repetition_name-before"; then
        record_failure \
            "Repetition $repetition: ready EndpointSlice targets changed"
        case_failed=1
    fi

    set_phase "$repetition" "correctness"
    record_phase_event "$repetition" "correctness" "start"
    if ! verify_service_endpoints \
        "repetition-$repetition_name-correctness-start"; then
        record_failure \
            "Repetition $repetition: ready EndpointSlice targets changed at correctness start"
        case_failed=1
    fi
    if ((case_failed == 0)) &&
        ! run_contract_check "$repetition" "$repetition_dir"; then
        record_phase_event "$repetition" "correctness" "failed"
        case_failed=1
    elif ((case_failed == 0)); then
        record_phase_event "$repetition" "correctness" "end"
        if ! verify_service_endpoints \
            "repetition-$repetition_name-correctness-end"; then
            record_failure \
                "Repetition $repetition: ready EndpointSlice targets changed at correctness end"
            case_failed=1
        fi
    fi

    if ((case_failed == 0)) &&
        ! run_k6_phase "$repetition" "warmup" "$WARMUP_DURATION"; then
        case_failed=1
    fi
    if ((case_failed == 0)) &&
        ! run_k6_phase "$repetition" "measurement" "$MEASUREMENT_DURATION"; then
        case_failed=1
    fi

    if ((case_failed == 0)); then
        set_phase "$repetition" "cooldown"
        record_phase_event "$repetition" "cooldown" "start"
        if ! verify_service_endpoints \
            "repetition-$repetition_name-cooldown-start"; then
            record_failure \
                "Repetition $repetition: ready EndpointSlice targets changed at cooldown start"
            case_failed=1
        fi
        sleep "$COOLDOWN_SECONDS"
        record_phase_event "$repetition" "cooldown" "end"
        if ! verify_service_endpoints \
            "repetition-$repetition_name-cooldown-end"; then
            record_failure \
                "Repetition $repetition: ready EndpointSlice targets changed at cooldown end"
            case_failed=1
        fi
    fi

    capture_restart_counts "$repetition_dir/restarts-after.json"
    if ! verify_service_endpoints "repetition-$repetition_name-after"; then
        record_failure \
            "Repetition $repetition: ready EndpointSlice targets changed"
        case_failed=1
    fi
    if ! diff -u \
        <(normalized_restarts "$repetition_dir/restarts-before.json") \
        <(normalized_restarts "$repetition_dir/restarts-after.json") \
        > "$repetition_dir/restarts.diff"; then
        record_failure "Repetition $repetition: a selected container restarted"
        case_failed=1
    fi

    if ((case_failed != 0)); then
        break
    fi
    completed_repetitions="$repetition"
done

set_phase "${completed_repetitions:-0}" "finished"
CASE_END_EPOCH="$(date -u +%s)"
CASE_END_TIMESTAMP="$(timestamp)"
record_phase_event 0 "case" "end"
stop_sampler

if [[ -f "$SAMPLER_FAILED_FILE" ]]; then
    record_failure "At least one metrics.k8s.io sample failed"
    case_failed=1
fi
if [[ -f "$ENDPOINT_SAMPLE_FAILED_FILE" ]]; then
    record_failure \
        "Ready EndpointSlice membership changed or could not be sampled during measurement"
    case_failed=1
fi

if ! capture_snapshot_set after; then
    record_failure "Final Kubernetes resource snapshot failed"
    case_failed=1
fi
if ! verify_service_endpoints after; then
    record_failure "Ready EndpointSlice targets changed by the end of the case"
    case_failed=1
fi
if ! capture_restart_counts "$ARTIFACT_DIR/cluster/restarts-case-after.json"; then
    record_failure "Final restart-count snapshot failed"
    case_failed=1
fi
if ! diff -u \
    <(normalized_restarts "$ARTIFACT_DIR/cluster/restarts-case-before.json") \
    <(normalized_restarts "$ARTIFACT_DIR/cluster/restarts-case-after.json") \
    > "$ARTIFACT_DIR/cluster/restarts-case.diff"; then
    record_failure "A selected container restarted during the case"
    case_failed=1
fi
if ! diff -u \
    <(jq -S '.configMaps' "$ARTIFACT_DIR/cluster/config-digests-before.json") \
    <(jq -S '.configMaps' "$ARTIFACT_DIR/cluster/config-digests-after.json") \
    > "$ARTIFACT_DIR/cluster/config-digests.diff"; then
    record_failure "A selected rendered ConfigMap changed during the case"
    case_failed=1
fi

if ((completed_repetitions != REPETITIONS)); then
    record_failure \
        "Only $completed_repetitions of $REPETITIONS repetitions completed"
    case_failed=1
fi
if ! capture_prometheus_metrics; then
    case_failed=1
fi

result="passed"
exit_status=0
if ((case_failed != 0)); then
    result="failed"
    exit_status=1
fi
jq -n \
    --arg completedAt "$(timestamp)" \
    --arg startedAt "$CASE_START_TIMESTAMP" \
    --arg endedAt "$CASE_END_TIMESTAMP" \
    --arg caseId "$CASE_ID" \
    --arg result "$result" \
    --argjson startEpoch "$CASE_START_EPOCH" \
    --argjson endEpoch "$CASE_END_EPOCH" \
    --argjson completedRepetitions "$completed_repetitions" \
    --argjson requestedRepetitions "$REPETITIONS" \
    '{
        completedAt: $completedAt,
        startedAt: $startedAt,
        endedAt: $endedAt,
        range: {
            startEpoch: $startEpoch,
            endEpoch: $endEpoch
        },
        caseId: $caseId,
        result: $result,
        completedRepetitions: $completedRepetitions,
        requestedRepetitions: $requestedRepetitions
    }' > "$ARTIFACT_DIR/result.json"

if ((exit_status != 0)); then
    printf 'CASE FAILED: %s (see %s)\n' "$CASE_ID" "$ARTIFACT_DIR" >&2
else
    printf 'CASE PASSED: %s (%s)\n' "$CASE_ID" "$ARTIFACT_DIR"
fi
exit "$exit_status"
