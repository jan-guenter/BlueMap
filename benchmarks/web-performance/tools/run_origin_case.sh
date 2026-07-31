#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BENCHMARK_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "$BENCHMARK_ROOT/../.." && pwd)"

KUBECONFIG_PATH="/root/.kube/guenter-cloud"
NAMESPACE="minecraft"
LOADGEN_POD="bluemap-perf-loadgen"
LOADGEN_BACKEND="kubernetes"
LOADGEN_IDENTITY=""
LOADGEN_IDENTITY_KEY=""
RUNPOD_LOADGEN_HELPER="$BENCHMARK_ROOT/tools/runpod_loadgen.sh"
TRAFFIC_BASE_URL=""
TRAFFIC_MODE=""
ORIGIN_BASE_URL=""
DIRECT_ORIGIN_BASE_URL=""
CLUSTER_SERVICE_TRANSPORT="port-forward"
FORMAL_RUN_ID=""
REQUIRE_EDGE_BYPASS="false"
K6_SCRIPT="$BENCHMARK_ROOT/k6/bluemap.js"
CONTRACT_SCRIPT="$BENCHMARK_ROOT/tools/check_http_contract.py"
RUNTIME_IDENTITY_SCRIPT="$BENCHMARK_ROOT/tools/runtime_identity.py"
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
PRE_ALLOCATED_VUS="256"
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
MAX_NON_TARGET_NODE_CPU_MEAN_CORES="3.0"
MAX_NON_TARGET_NODE_CPU_MAXIMUM_CORES="4.0"
PYTHON_BIN="${BENCHMARK_PYTHON:-python3}"
SERVICE_PORT=""
SERVICE=""
TRAFFIC_SERVICE=""
TRAFFIC_SERVICE_PORT=""
TRAFFIC_INGRESS="bluemap-perf-public"
TRAFFIC_HOST="bluemap-test.guenter.cloud"
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
BENCHMARK_COMMIT=""
EXPECTED_IMAGES_JSON=""
EXPECTED_SANITIZED_CONFIG_SHA256=""
EXPECTED_SANITIZED_RUNTIME_SPEC_SHA256=""

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
--raw, and port-forward operations. Kubernetes load generation additionally
uses exec/cp into bluemap-perf-loadgen. RunPod load generation uses a frozen
identity plus a dedicated Ed25519 key and sends traffic through a public URL.

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
  --pre-allocated-vus N           k6 preallocated VUs (default: 256)
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
                                  reject mean background CPU above N (default: 3)
  --max-non-target-node-cpu-maximum-cores N
                                  reject peak background CPU above N (default: 4)

Path/cluster options:
  --load-generator-backend kubernetes|runpod-ssh
                                  request source (default: kubernetes)
  --load-generator-identity FILE frozen non-secret RunPod identity
  --load-generator-identity-key FILE
                                  private Ed25519 identity for RunPod SSH
  --traffic-base-url URL          mode-specific request URL for RunPod
  --traffic-mode MODE             cloudflare-https (default) or ssh-l4-traefik
  --traffic-service NAME          public routing Service for RunPod
  --traffic-service-port PORT     public routing Service port for RunPod
  --origin-base-url URL           exact cluster-DNS origin URL for direct access
  --formal-run-id ID              unique run identifier included in User-Agent
  --require-edge-bypass           reject Cloudflare cache hits/challenges
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

verify_committed_benchmark_file() {
    local relative_path="$1"
    local actual_path="$2"
    local expected_digest
    local actual_digest
    expected_digest="$(
        git -C "$REPOSITORY_ROOT" show \
            "$BENCHMARK_COMMIT:$relative_path" |
            sha256sum |
            awk '{print $1}'
    )" || die "Could not read $relative_path from the scheduled Git revision"
    actual_digest="$(sha256sum "$actual_path" | awk '{print $1}')" ||
        die "Could not hash benchmark input $actual_path"
    [[ "$actual_digest" == "$expected_digest" ]] ||
        die "Benchmark input $actual_path differs from the scheduled Git revision"
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
        --load-generator-backend)
            LOADGEN_BACKEND="${2:-}"
            shift 2
            ;;
        --load-generator-identity)
            LOADGEN_IDENTITY="${2:-}"
            shift 2
            ;;
        --load-generator-identity-key)
            LOADGEN_IDENTITY_KEY="${2:-}"
            shift 2
            ;;
        --traffic-base-url)
            TRAFFIC_BASE_URL="${2:-}"
            shift 2
            ;;
        --traffic-mode)
            TRAFFIC_MODE="${2:-}"
            shift 2
            ;;
        --traffic-service)
            TRAFFIC_SERVICE="${2:-}"
            shift 2
            ;;
        --traffic-service-port)
            TRAFFIC_SERVICE_PORT="${2:-}"
            shift 2
            ;;
        --origin-base-url)
            DIRECT_ORIGIN_BASE_URL="${2:-}"
            shift 2
            ;;
        --formal-run-id)
            FORMAL_RUN_ID="${2:-}"
            shift 2
            ;;
        --require-edge-bypass)
            REQUIRE_EDGE_BYPASS="true"
            shift
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
[[ "$LOADGEN_BACKEND" == "kubernetes" || "$LOADGEN_BACKEND" == "runpod-ssh" ]] ||
    die "--load-generator-backend must be kubernetes or runpod-ssh"
if [[ "$LOADGEN_BACKEND" == "kubernetes" ]]; then
    validate_prefixed_name "load-generator Pod" "$LOADGEN_POD"
    [[ -z "$LOADGEN_IDENTITY" && -z "$LOADGEN_IDENTITY_KEY" ]] ||
        die "Kubernetes load generation does not accept RunPod identity files"
    [[ -z "$TRAFFIC_BASE_URL" ]] ||
        die "Kubernetes load generation does not accept --traffic-base-url"
    [[ -z "$TRAFFIC_MODE" ]] ||
        die "Kubernetes load generation does not accept --traffic-mode"
    [[ -z "$TRAFFIC_SERVICE" && -z "$TRAFFIC_SERVICE_PORT" ]] ||
        die "Kubernetes load generation does not accept a traffic Service"
    [[ "$REQUIRE_EDGE_BYPASS" == "false" ]] ||
        die "Kubernetes load generation cannot require an edge bypass"
else
    [[ -n "$TRAFFIC_MODE" ]] || TRAFFIC_MODE="cloudflare-https"
    [[ -f "$LOADGEN_IDENTITY" && ! -L "$LOADGEN_IDENTITY" ]] ||
        die "RunPod load generation requires a regular --load-generator-identity"
    [[ -f "$LOADGEN_IDENTITY_KEY" && ! -L "$LOADGEN_IDENTITY_KEY" ]] ||
        die "RunPod load generation requires a regular --load-generator-identity-key"
    [[ "$TRAFFIC_BASE_URL" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[^[:space:]]*)?$ ]] ||
        die "RunPod load generation requires an absolute HTTP(S) --traffic-base-url"
    [[ "$TRAFFIC_BASE_URL" != *"?"* && "$TRAFFIC_BASE_URL" != *"#"* ]] ||
        die "RunPod traffic URLs must not contain a query or fragment"
    case "$TRAFFIC_MODE" in
        cloudflare-https)
            [[ "${TRAFFIC_BASE_URL%/}" == "https://$TRAFFIC_HOST" ]] ||
                die "cloudflare-https traffic must use the exact HTTPS benchmark URL"
            [[ "$REQUIRE_EDGE_BYPASS" == "true" ]] ||
                die "cloudflare-https traffic requires --require-edge-bypass"
            ;;
        ssh-l4-traefik)
            [[ "${TRAFFIC_BASE_URL%/}" == "http://$TRAFFIC_HOST" ]] ||
                die "ssh-l4-traefik traffic must use the exact HTTP benchmark URL"
            [[ "$REQUIRE_EDGE_BYPASS" == "false" ]] ||
                die "ssh-l4-traefik traffic forbids --require-edge-bypass"
            ;;
        *)
            die "--traffic-mode must be cloudflare-https or ssh-l4-traefik"
            ;;
    esac
    [[ "$TRAFFIC_SERVICE" == "bluemap-perf-public" ]] ||
        die "RunPod load generation requires --traffic-service bluemap-perf-public"
    [[ "$TRAFFIC_SERVICE_PORT" == "8100" ]] ||
        die "RunPod load generation requires --traffic-service-port 8100"
    [[ "$TRAFFIC_SERVICE" != "$SERVICE" ]] ||
        die "RunPod traffic and candidate Services must be distinct"
    [[ "$FORMAL_RUN_ID" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] ||
        die "RunPod load generation requires a valid --formal-run-id"
    [[ "$(jq -r '.runId // empty' "$LOADGEN_IDENTITY")" == "$FORMAL_RUN_ID" ]] ||
        die "RunPod identity runId differs from --formal-run-id"
fi
for name in "${WEB_DEPLOYMENTS[@]}"; do
    validate_prefixed_name "Deployment" "$name"
done
for name in "${WEB_PODS[@]}"; do
    validate_prefixed_name "web Pod" "$name"
    [[ "$LOADGEN_BACKEND" != "kubernetes" || "$name" != "$LOADGEN_POD" ]] ||
        die "The load-generator Pod cannot be a web Pod"
done
for name in "${DATABASE_PODS[@]}"; do
    validate_prefixed_name "database Pod" "$name"
    [[ "$LOADGEN_BACKEND" != "kubernetes" || "$name" != "$LOADGEN_POD" ]] ||
        die "The load-generator Pod cannot be a database Pod"
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
if [[ -n "$DIRECT_ORIGIN_BASE_URL" ]]; then
    [[ "$DIRECT_ORIGIN_BASE_URL" == \
        "http://$SERVICE.$NAMESPACE.svc.cluster.local:$SERVICE_PORT" ]] ||
        die "--origin-base-url must exactly match the selected Service cluster-DNS URL"
    CLUSTER_SERVICE_TRANSPORT="direct-cluster-dns"
fi
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
[[ -f "$SCRIPT_DIR/check_load_generator_capacity.py" ]] ||
    die "Load-generator capacity helper is unavailable"
[[ -f "$RUNTIME_IDENTITY_SCRIPT" ]] ||
    die "Runtime-identity helper is unavailable"
[[ -f "$RUNPOD_LOADGEN_HELPER" ]] ||
    die "RunPod load-generator helper is unavailable"
[[ -f "$KUBECONFIG_PATH" ]] || die "Kubeconfig '$KUBECONFIG_PATH' is not a regular file"

for command in git kubectl jq sha256sum tee diff; do
    require_command "$command"
done
if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
    for command in scp ssh; do
        require_command "$command"
    done
fi
require_command "$PYTHON_BIN"
BENCHMARK_COMMIT="$(
    git -C "$REPOSITORY_ROOT" rev-parse --verify 'HEAD^{commit}' 2>/dev/null
)" || die "Could not resolve the benchmark repository Git revision"
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
    [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]] ||
        die "Formal scheduled cases require the RunPod SSH load generator"
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
    expected_benchmark_revision="$(
        jq -er '.benchmarkGitRevision' <<<"$SCHEDULE_ENTRY_JSON"
    )" || die "Formal schedule entry has no benchmark Git revision"
    [[ "$BENCHMARK_COMMIT" == "$expected_benchmark_revision" ]] ||
        die "Benchmark Git revision does not match the formal schedule"
    tracked_worktree_status="$(
        git -C "$REPOSITORY_ROOT" status --porcelain --untracked-files=no
    )" || die "Could not verify the formal benchmark worktree state"
    [[ -z "$tracked_worktree_status" ]] ||
        die "Formal runs require a clean tracked Git worktree"

    verify_committed_benchmark_file \
        "benchmarks/web-performance/tools/run_origin_case.sh" \
        "${BASH_SOURCE[0]}"
    verify_committed_benchmark_file \
        "benchmarks/web-performance/k6/bluemap.js" \
        "$K6_SCRIPT"
    verify_committed_benchmark_file \
        "benchmarks/web-performance/tools/check_http_contract.py" \
        "$CONTRACT_SCRIPT"
    for helper in \
        capture_prometheus.py \
        check_arrival_gate.py \
        check_load_generator_capacity.py \
        configmap_references.py \
        generate_schedule.py \
        runtime_identity.py \
        sanitize_configmap.py \
        sanitize_kubernetes_resource.py \
        slow_reader.py \
        runpod_loadgen.sh; do
        verify_committed_benchmark_file \
            "benchmarks/web-performance/tools/$helper" \
            "$SCRIPT_DIR/$helper"
    done

    EXPECTED_IMAGES_JSON="$(
        jq -ceS '.expectedImages' <<<"$SCHEDULE_ENTRY_JSON"
    )" || die "Formal schedule entry has no expected image identity"
    EXPECTED_SANITIZED_CONFIG_SHA256="$(
        jq -er '.expectedSanitizedConfigSha256' <<<"$SCHEDULE_ENTRY_JSON"
    )" || die "Formal schedule entry has no expected configuration identity"
    EXPECTED_SANITIZED_RUNTIME_SPEC_SHA256="$(
        jq -er '.expectedSanitizedRuntimeSpecSha256' <<<"$SCHEDULE_ENTRY_JSON"
    )" || die "Formal schedule entry has no expected runtime-spec identity"
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

validate_load_generator_pod() {
    local pod_json
    pod_json="$(kube get pod "$LOADGEN_POD" -o json)" ||
        die "Could not inspect load-generator Pod '$LOADGEN_POD'"

    jq -e \
        --arg pod "$LOADGEN_POD" \
        --arg namespace "$NAMESPACE" \
        '
        .apiVersion == "v1"
        and .kind == "Pod"
        and .metadata.name == $pod
        and .metadata.namespace == $namespace
        and (.metadata.uid | type == "string" and length > 0)
        and .metadata.deletionTimestamp == null
        and ((.metadata.ownerReferences // []) | length == 0)
        and .metadata.labels["app.kubernetes.io/name"] == $pod
        and .metadata.labels["app.kubernetes.io/part-of"]
            == "bluemap-web-performance"
        and .metadata.labels["bluemap.guenter.cloud/experiment-id"]
            == "origin-loadgen"
        and .spec.automountServiceAccountToken == false
        and ((.spec.initContainers // []) | length == 0)
        and ((.spec.ephemeralContainers // []) | length == 0)
        and (.spec.containers | length == 1)
        and .spec.containers[0].name == "k6"
        and (.spec.volumes | length == 3)
        and (
            [.spec.volumes[] |
                select(
                    .name == "benchmark"
                    and .configMap.name == $pod
                    and ((keys - ["configMap", "name"]) | length == 0)
                )
            ] | length == 1
        )
        and (
            [.spec.volumes[] |
                select(
                    .name == "artifacts"
                    and (.emptyDir | type == "object")
                    and ((keys - ["emptyDir", "name"]) | length == 0)
                )
            ] | length == 1
        )
        and (
            [.spec.volumes[] |
                select(
                    .name == "tmp"
                    and (.emptyDir | type == "object")
                    and ((keys - ["emptyDir", "name"]) | length == 0)
                )
            ] | length == 1
        )
        and (
            [.spec.containers[0].volumeMounts[]? |
                select(
                    .name == "artifacts"
                    and .mountPath == "/artifacts"
                    and (.readOnly // false) == false
                    and (.subPath // "") == ""
                    and (.subPathExpr // "") == ""
                )
            ] | length == 1
        )
        and (
            [.spec.containers[0].volumeMounts[]? |
                select(.mountPath == "/artifacts")
            ] | length == 1
        )
        and .status.phase == "Running"
        and any(.status.conditions[]?;
            .type == "Ready" and .status == "True")
        ' <<<"$pod_json" >/dev/null ||
        die "Pod '$LOADGEN_POD' does not match the fail-closed load-generator structure"
}

validate_runpod_load_generator() {
    "$RUNPOD_LOADGEN_HELPER" \
        --identity "$LOADGEN_IDENTITY" \
        --identity-key "$LOADGEN_IDENTITY_KEY" \
        validate
}

loadgen_exec() {
    if [[ "$LOADGEN_BACKEND" == "kubernetes" ]]; then
        validate_load_generator_pod
        kube exec "pod/$LOADGEN_POD" -c k6 -- "$@"
    else
        "$RUNPOD_LOADGEN_HELPER" \
            --identity "$LOADGEN_IDENTITY" \
            --identity-key "$LOADGEN_IDENTITY_KEY" \
            exec "$@"
    fi
}

loadgen_k6_exec() {
    local transport_output="${1:-}"
    shift || return 1
    if [[ "$LOADGEN_BACKEND" == "runpod-ssh" &&
        "$TRAFFIC_MODE" == "ssh-l4-traefik" ]]; then
        "$RUNPOD_LOADGEN_HELPER" \
            --identity "$LOADGEN_IDENTITY" \
            --identity-key "$LOADGEN_IDENTITY_KEY" \
            exec-traefik-forward \
            --transport-output "$transport_output" \
            -- \
            "$@"
    else
        loadgen_exec "$@"
    fi
}

loadgen_copy_to() {
    local local_file="$1"
    local remote_file="$2"
    if [[ "$LOADGEN_BACKEND" == "kubernetes" ]]; then
        validate_load_generator_pod
        kube cp "$local_file" "$LOADGEN_POD:$remote_file" -c k6
    else
        "$RUNPOD_LOADGEN_HELPER" \
            --identity "$LOADGEN_IDENTITY" \
            --identity-key "$LOADGEN_IDENTITY_KEY" \
            copy-to "$local_file" "$remote_file"
    fi
}

loadgen_copy_from() {
    local remote_file="$1"
    local local_file="$2"
    if [[ "$LOADGEN_BACKEND" == "kubernetes" ]]; then
        validate_load_generator_pod
        kube cp "$LOADGEN_POD:$remote_file" "$local_file" -c k6
    else
        "$RUNPOD_LOADGEN_HELPER" \
            --identity "$LOADGEN_IDENTITY" \
            --identity-key "$LOADGEN_IDENTITY_KEY" \
            copy-from "$remote_file" "$local_file"
    fi
}

validate_available_deployment() {
    local deployment="$1"
    kube get deployment "$deployment" -o json |
        jq -e '
            (.spec.replicas // 0) > 0
            and (.spec.paused // false) == false
            and (.status.observedGeneration // 0) == .metadata.generation
            and (.status.replicas // 0) == .spec.replicas
            and (.status.updatedReplicas // 0) == .spec.replicas
            and (.status.readyReplicas // 0) == .spec.replicas
            and (.status.availableReplicas // 0) == .spec.replicas
            and (.status.unavailableReplicas // 0) == 0
        ' >/dev/null ||
        die "Deployment '$deployment' has not converged on its current Pod template"
}

validate_traffic_service() {
    local payload
    payload="$(kube get service "$TRAFFIC_SERVICE" -o json)" ||
        return 1
    jq -e \
        --arg service "$TRAFFIC_SERVICE" \
        --arg namespace "$NAMESPACE" \
        --argjson port "$TRAFFIC_SERVICE_PORT" \
        '
        .apiVersion == "v1"
        and .kind == "Service"
        and .metadata.name == $service
        and .metadata.namespace == $namespace
        and (.metadata.uid | type == "string" and length > 0)
        and .metadata.deletionTimestamp == null
        and .metadata.labels["app.kubernetes.io/part-of"]
            == "bluemap-web-performance"
        and .metadata.labels["bluemap.guenter.cloud/experiment-id"]
            == "runpod-public-route"
        and .spec.type == "ClusterIP"
        and .spec.selector == {
            "app.kubernetes.io/name": "bluemap-web",
            "app.kubernetes.io/part-of": "bluemap-web-performance"
        }
        and (.spec.ports | length) == 1
        and .spec.ports[0].name == "http"
        and .spec.ports[0].port == $port
        and .spec.ports[0].protocol == "TCP"
        and .spec.ports[0].targetPort == "http"
        ' <<<"$payload" >/dev/null
}

validate_traffic_ingress() {
    local payload
    payload="$(kube get ingress "$TRAFFIC_INGRESS" -o json)" ||
        return 1
    jq -e \
        --arg ingress "$TRAFFIC_INGRESS" \
        --arg namespace "$NAMESPACE" \
        --arg host "$TRAFFIC_HOST" \
        --arg service "$TRAFFIC_SERVICE" \
        '
        .apiVersion == "networking.k8s.io/v1"
        and .kind == "Ingress"
        and .metadata.name == $ingress
        and .metadata.namespace == $namespace
        and (.metadata.uid | type == "string" and length > 0)
        and .metadata.deletionTimestamp == null
        and .metadata.labels["app.kubernetes.io/part-of"]
            == "bluemap-web-performance"
        and .metadata.labels["bluemap.guenter.cloud/experiment-id"]
            == "runpod-public-route"
        and .spec.ingressClassName == "traefik"
        and .spec.defaultBackend == null
        and ((.spec.tls // []) | length) == 0
        and (.spec.rules | length) == 1
        and .spec.rules[0].host == $host
        and (.spec.rules[0].http.paths | length) == 1
        and .spec.rules[0].http.paths[0].path == "/"
        and .spec.rules[0].http.paths[0].pathType == "Prefix"
        and .spec.rules[0].http.paths[0].backend.service.name == $service
        and .spec.rules[0].http.paths[0].backend.service.port == {
            "name": "http"
        }
        ' <<<"$payload" >/dev/null
}

verify_named_service_endpoints() {
    local service="$1"
    local destination="$2"
    local payload
    local expected_pods_json
    expected_pods_json="$(
        printf '%s\n' "${WEB_PODS[@]}" |
            jq -Rsc 'split("\n")[:-1] | sort | unique'
    )"
    payload="$(
        kube get endpointslice \
            --selector "kubernetes.io/service-name=$service" \
            -o json
    )" || return 1

    jq \
        --arg capturedAt "$(timestamp)" \
        --arg service "$service" \
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

verify_service_endpoints() {
    local label="$1"
    verify_named_service_endpoints \
        "$SERVICE" \
        "$ARTIFACT_DIR/cluster/endpoints-$label.json" ||
        return 1
    if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
        validate_traffic_service || return 1
        validate_traffic_ingress || return 1
        verify_named_service_endpoints \
            "$TRAFFIC_SERVICE" \
            "$ARTIFACT_DIR/cluster/traffic-endpoints-$label.json" ||
            return 1
    fi
}

verify_web_pod_ownership() {
    local label="$1"
    local items_file="$ARTIFACT_DIR/cluster/.ownership-$label.ndjson"
    local destination="$ARTIFACT_DIR/cluster/ownership-$label.json"
    local selected
    : > "$items_file"

    for pod in "${WEB_PODS[@]}"; do
        local pod_payload
        local pod_uid
        local snapshot_pod_uid
        local pod_controller
        local replicaset_name
        local replicaset_uid
        local replicaset_payload
        local replicaset_controller
        local deployment_name
        local deployment_uid
        local deployment_snapshot
        local deployment_payload
        local deployment_resource_version
        local deployment_revision
        local replicaset_revision
        local selected_deployment=false

        pod_payload="$(kube get pod "$pod" -o json)" || return 1
        pod_uid="$(jq -er '.metadata.uid' <<<"$pod_payload")" || return 1
        snapshot_pod_uid="$(
            jq -er '.resource.metadata.uid' \
                "$ARTIFACT_DIR/cluster/$label/pod-$pod.json"
        )" || return 1
        [[ "$pod_uid" == "$snapshot_pod_uid" ]] || return 1
        pod_controller="$(
            jq -ce '
                [.metadata.ownerReferences[]?
                 | select(.controller == true)] as $controllers
                | if ($controllers | length) != 1
                     or $controllers[0].apiVersion != "apps/v1"
                     or $controllers[0].kind != "ReplicaSet"
                     or ($controllers[0].name | type) != "string"
                     or ($controllers[0].uid | type) != "string"
                  then error(
                      "Pod must have exactly one apps/v1 ReplicaSet controller"
                  )
                  else $controllers[0]
                  end
            ' <<<"$pod_payload"
        )" || return 1
        replicaset_name="$(jq -er '.name' <<<"$pod_controller")" || return 1
        replicaset_uid="$(jq -er '.uid' <<<"$pod_controller")" || return 1
        validate_prefixed_name "ReplicaSet" "$replicaset_name"

        replicaset_payload="$(
            kube get replicaset "$replicaset_name" -o json
        )" || return 1
        jq -e \
            --arg namespace "$NAMESPACE" \
            --arg name "$replicaset_name" \
            --arg uid "$replicaset_uid" \
            '.apiVersion == "apps/v1"
             and .kind == "ReplicaSet"
             and .metadata.namespace == $namespace
             and .metadata.name == $name
             and .metadata.uid == $uid' <<<"$replicaset_payload" >/dev/null ||
            return 1
        replicaset_controller="$(
            jq -ce '
                [.metadata.ownerReferences[]?
                 | select(.controller == true)] as $controllers
                | if ($controllers | length) != 1
                     or $controllers[0].apiVersion != "apps/v1"
                     or $controllers[0].kind != "Deployment"
                     or ($controllers[0].name | type) != "string"
                     or ($controllers[0].uid | type) != "string"
                  then error(
                      "ReplicaSet must have exactly one apps/v1 Deployment controller"
                  )
                  else $controllers[0]
                  end
            ' <<<"$replicaset_payload"
        )" || return 1
        deployment_name="$(
            jq -er '.name' <<<"$replicaset_controller"
        )" || return 1
        deployment_uid="$(jq -er '.uid' <<<"$replicaset_controller")" ||
            return 1

        for selected in "${WEB_DEPLOYMENTS[@]}"; do
            if [[ "$selected" == "$deployment_name" ]]; then
                selected_deployment=true
                break
            fi
        done
        [[ "$selected_deployment" == true ]] || return 1
        deployment_snapshot="$ARTIFACT_DIR/cluster/$label/deployment-$deployment_name.json"
        jq -e \
            --arg namespace "$NAMESPACE" \
            --arg name "$deployment_name" \
            --arg uid "$deployment_uid" \
            '.resource.apiVersion == "apps/v1"
             and .resource.kind == "Deployment"
             and .resource.metadata.namespace == $namespace
             and .resource.metadata.name == $name
             and .resource.metadata.uid == $uid' \
            "$deployment_snapshot" >/dev/null ||
            return 1

        deployment_resource_version="$(
            jq -er '.resource.metadata.resourceVersion' "$deployment_snapshot"
        )" || return 1
        deployment_payload="$(
            kube get deployment "$deployment_name" -o json
        )" || return 1
        jq -e \
            --arg namespace "$NAMESPACE" \
            --arg name "$deployment_name" \
            --arg uid "$deployment_uid" \
            --arg resourceVersion "$deployment_resource_version" \
            '.apiVersion == "apps/v1"
             and .kind == "Deployment"
             and .metadata.namespace == $namespace
             and .metadata.name == $name
             and .metadata.uid == $uid
             and .metadata.resourceVersion == $resourceVersion
             and (.spec.paused // false) == false
             and (.status.observedGeneration // 0) == .metadata.generation
             and (.status.replicas // 0) == .spec.replicas
             and (.status.updatedReplicas // 0) == .spec.replicas
             and (.status.readyReplicas // 0) == .spec.replicas
             and (.status.availableReplicas // 0) == .spec.replicas
             and (.status.unavailableReplicas // 0) == 0' \
            <<<"$deployment_payload" >/dev/null ||
            return 1
        deployment_revision="$(
            jq -er '.metadata.annotations["deployment.kubernetes.io/revision"]' \
                <<<"$deployment_payload"
        )" || return 1
        replicaset_revision="$(
            jq -er '.metadata.annotations["deployment.kubernetes.io/revision"]' \
                <<<"$replicaset_payload"
        )" || return 1
        [[ "$deployment_revision" =~ ^[1-9][0-9]*$ ]] || return 1
        [[ "$replicaset_revision" == "$deployment_revision" ]] || return 1

        jq -nc \
            --arg pod "$pod" \
            --arg podUid "$pod_uid" \
            --arg replicaSet "$replicaset_name" \
            --arg replicaSetUid "$replicaset_uid" \
            --arg revision "$replicaset_revision" \
            --arg deployment "$deployment_name" \
            --arg deploymentUid "$deployment_uid" \
            '{
                pod: {name: $pod, uid: $podUid},
                replicaSet: {
                    name: $replicaSet,
                    uid: $replicaSetUid,
                    revision: $revision
                },
                deployment: {
                    name: $deployment,
                    uid: $deploymentUid,
                    revision: $revision
                }
            }' >> "$items_file" ||
            return 1
    done

    for deployment in "${WEB_DEPLOYMENTS[@]}"; do
        local desired_replicas
        local owned_pods
        desired_replicas="$(
            jq -er '.resource.spec.replicas' \
                "$ARTIFACT_DIR/cluster/$label/deployment-$deployment.json"
        )" || return 1
        owned_pods="$(
            jq -s --arg deployment "$deployment" \
                '[.[] | select(.deployment.name == $deployment)] | length' \
                "$items_file"
        )" || return 1
        [[ "$owned_pods" == "$desired_replicas" ]] || return 1
    done

    jq -s \
        --arg capturedAt "$(timestamp)" \
        --arg service "$SERVICE" \
        '{
            capturedAt: $capturedAt,
            service: $service,
            passed: true,
            pods: .
        }' "$items_file" > "$destination" ||
        return 1
    rm -f -- "$items_file"
}

sample_named_service_endpoints() {
    local service="$1"
    local phase="$2"
    local destination="$3"
    local payload
    local expected_pods_json
    expected_pods_json="$(json_array "${WEB_PODS[@]}" | jq -cS .)"
    if ! payload="$(
        kube get endpointslice \
            --selector "kubernetes.io/service-name=$service" \
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

sample_service_endpoints() {
    local phase="$1"
    sample_named_service_endpoints \
        "$SERVICE" \
        "$phase" \
        "$ARTIFACT_DIR/samples/endpoint-membership.ndjson"
    if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
        sample_named_service_endpoints \
            "$TRAFFIC_SERVICE" \
            "$phase" \
            "$ARTIFACT_DIR/samples/traffic-endpoint-membership.ndjson"
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
    local -a snapshot_files=()

    for configmap in "${CONFIGMAPS[@]}"; do
        local snapshot_file="$directory/configmap-$configmap.json"
        snapshot_configmap "$configmap" "$snapshot_file" || return 1
        snapshot_files+=("$snapshot_file")
    done

    "$PYTHON_BIN" "$RUNTIME_IDENTITY_SCRIPT" config-snapshots \
        "${snapshot_files[@]}" |
        jq \
        --arg capturedAt "$(timestamp)" \
        '. + {capturedAt: $capturedAt}' \
        > "$ARTIFACT_DIR/cluster/config-digests-$label.json" ||
        return 1
}

capture_snapshot_set() {
    local label="$1"
    local directory="$ARTIFACT_DIR/cluster/$label"
    local -a runtime_spec_arguments=()
    mkdir -- "$directory" || return 1

    snapshot_resource service "$SERVICE" "$directory/service-$SERVICE.json" ||
        return 1
    if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
        snapshot_resource \
            service \
            "$TRAFFIC_SERVICE" \
            "$directory/service-$TRAFFIC_SERVICE.json" ||
            return 1
        snapshot_resource \
            ingress \
            "$TRAFFIC_INGRESS" \
            "$directory/ingress-$TRAFFIC_INGRESS.json" ||
            return 1
    fi
    runtime_spec_arguments=(
        runtime-spec-snapshots
        --service "$directory/service-$SERVICE.json"
    )
    for deployment in "${WEB_DEPLOYMENTS[@]}"; do
        snapshot_resource deployment "$deployment" \
            "$directory/deployment-$deployment.json" ||
            return 1
        runtime_spec_arguments+=(
            --deployment "$directory/deployment-$deployment.json"
        )
    done
    for target in "${SAMPLE_TARGETS[@]}"; do
        local pod="${target#*:}"
        snapshot_resource pod "$pod" "$directory/pod-$pod.json" ||
            return 1
    done
    capture_configmap_set "$label" || return 1
    "$PYTHON_BIN" "$RUNTIME_IDENTITY_SCRIPT" \
        "${runtime_spec_arguments[@]}" |
        jq \
            --arg capturedAt "$(timestamp)" \
            '. + {capturedAt: $capturedAt}' \
            > "$ARTIFACT_DIR/cluster/runtime-spec-digests-$label.json" ||
        return 1

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

verify_formal_runtime_identity() {
    [[ -n "$SCHEDULE_ENTRY_JSON" ]] || return 0

    local identity_items="$ARTIFACT_DIR/cluster/.runtime-identities.ndjson"
    local config_identity_file="$ARTIFACT_DIR/cluster/config-digests-before.json"
    local runtime_spec_identity_file="$ARTIFACT_DIR/cluster/runtime-spec-digests-before.json"
    local actual_config_sha256
    local actual_runtime_spec_sha256
    local config_passed=false
    local runtime_spec_passed=false
    local mismatch=0
    : > "$identity_items"

    for pod in "${WEB_PODS[@]}"; do
        local pod_snapshot="$ARTIFACT_DIR/cluster/before/pod-$pod.json"
        local actual_images
        actual_images="$(
            "$PYTHON_BIN" "$RUNTIME_IDENTITY_SCRIPT" pod-images \
                < "$pod_snapshot"
        )" || {
            rm -f -- "$identity_items"
            return 1
        }
        actual_images="$(jq -ceS . <<<"$actual_images")" || {
            rm -f -- "$identity_items"
            return 1
        }
        jq -nc \
            --arg pod "$pod" \
            --argjson expected "$EXPECTED_IMAGES_JSON" \
            --argjson actual "$actual_images" \
            '{
                pod: $pod,
                expectedImages: $expected,
                actualImages: $actual,
                passed: ($expected == $actual)
            }' >> "$identity_items" || {
                rm -f -- "$identity_items"
                return 1
            }
        [[ "$actual_images" == "$EXPECTED_IMAGES_JSON" ]] || mismatch=1
    done

    actual_config_sha256="$(
        jq -er '.sanitizedConfigSha256' "$config_identity_file"
    )" || {
        rm -f -- "$identity_items"
        return 1
    }
    if [[ "$actual_config_sha256" == "$EXPECTED_SANITIZED_CONFIG_SHA256" ]]; then
        config_passed=true
    else
        mismatch=1
    fi
    actual_runtime_spec_sha256="$(
        jq -er '.sanitizedRuntimeSpecSha256' "$runtime_spec_identity_file"
    )" || {
        rm -f -- "$identity_items"
        return 1
    }
    if [[ "$actual_runtime_spec_sha256" == \
        "$EXPECTED_SANITIZED_RUNTIME_SPEC_SHA256" ]]; then
        runtime_spec_passed=true
    else
        mismatch=1
    fi

    jq -s \
        --arg benchmarkGitRevision "$BENCHMARK_COMMIT" \
        --arg expectedSanitizedConfigSha256 \
            "$EXPECTED_SANITIZED_CONFIG_SHA256" \
        --arg actualSanitizedConfigSha256 "$actual_config_sha256" \
        --argjson configPassed "$config_passed" \
        --arg expectedSanitizedRuntimeSpecSha256 \
            "$EXPECTED_SANITIZED_RUNTIME_SPEC_SHA256" \
        --arg actualSanitizedRuntimeSpecSha256 "$actual_runtime_spec_sha256" \
        --argjson runtimeSpecPassed "$runtime_spec_passed" \
        '{
            benchmarkGitRevision: $benchmarkGitRevision,
            webPods: .,
            configuration: {
                expectedSanitizedConfigSha256:
                    $expectedSanitizedConfigSha256,
                actualSanitizedConfigSha256:
                    $actualSanitizedConfigSha256,
                passed: $configPassed
            },
            runtimeSpec: {
                expectedSanitizedRuntimeSpecSha256:
                    $expectedSanitizedRuntimeSpecSha256,
                actualSanitizedRuntimeSpecSha256:
                    $actualSanitizedRuntimeSpecSha256,
                passed: $runtimeSpecPassed
            },
            passed: (
                $configPassed
                and $runtimeSpecPassed
                and all(.[]; .passed == true)
            )
        }' "$identity_items" \
        > "$ARTIFACT_DIR/cluster/runtime-identity-before.json" || {
            rm -f -- "$identity_items"
            return 1
        }
    rm -f -- "$identity_items"
    ((mismatch == 0))
}

capture_restart_counts() {
    local destination="$1"
    local items_file="${destination%.json}.items.ndjson"
    local runtime_identity_mismatch=0
    : > "$items_file"

    for target in "${SAMPLE_TARGETS[@]}"; do
        local role="${target%%:*}"
        local pod="${target#*:}"
        local pod_payload
        local actual_pod_uid
        local expected_pod_uid
        local pod_identity_passed=false
        local actual_images_json="null"
        local expected_images_json="null"
        local image_identity_passed="null"

        pod_payload="$(kube get pod "$pod" -o json)" || return 1
        actual_pod_uid="$(jq -er '.metadata.uid' <<<"$pod_payload")" || return 1
        expected_pod_uid="$(
            jq -er '.resource.metadata.uid' \
                "$ARTIFACT_DIR/cluster/before/pod-$pod.json"
        )" || return 1
        if [[ "$actual_pod_uid" == "$expected_pod_uid" ]]; then
            pod_identity_passed=true
        else
            runtime_identity_mismatch=1
        fi

        if [[ "$role" == "web" && -n "$SCHEDULE_ENTRY_JSON" ]]; then
            actual_images_json="$(
                "$PYTHON_BIN" "$RUNTIME_IDENTITY_SCRIPT" pod-images \
                    <<<"$pod_payload"
            )" || return 1
            actual_images_json="$(jq -ceS . <<<"$actual_images_json")" ||
                return 1
            expected_images_json="$EXPECTED_IMAGES_JSON"
            if [[ "$actual_images_json" == "$EXPECTED_IMAGES_JSON" ]]; then
                image_identity_passed=true
            else
                image_identity_passed=false
                runtime_identity_mismatch=1
            fi
        fi

        jq -c \
            --arg capturedAt "$(timestamp)" \
            --arg role "$role" \
            --arg actualPodUid "$actual_pod_uid" \
            --arg expectedPodUid "$expected_pod_uid" \
            --argjson podIdentityPassed "$pod_identity_passed" \
            --argjson actualImages "$actual_images_json" \
            --argjson expectedImages "$expected_images_json" \
            --argjson imageIdentityPassed "$image_identity_passed" \
            '{
                capturedAt: $capturedAt,
                role: $role,
                pod: .metadata.name,
                uid: .metadata.uid,
                podIdentity: {
                    expectedUid: $expectedPodUid,
                    actualUid: $actualPodUid,
                    passed: $podIdentityPassed
                },
                containers: [
                    .status.containerStatuses[]? | {
                        name,
                        image,
                        imageID,
                        ready,
                        restartCount
                    }
                ],
                imageIdentity: (
                    if $imageIdentityPassed == null then null
                    else {
                        expectedImages: $expectedImages,
                        actualImages: $actualImages,
                        passed: $imageIdentityPassed
                    }
                    end
                )
            }' <<<"$pod_payload" >> "$items_file" ||
            return 1
    done

    jq -s '{pods: .}' "$items_file" > "$destination" || return 1
    rm -f -- "$items_file"
    ((runtime_identity_mismatch == 0))
}

normalized_restarts() {
    jq -S '[
        .pods[] |
        .pod as $pod |
        .uid as $uid |
        .containers[] |
        {
            pod: $pod,
            uid: $uid,
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
        if [[ -f "$log_file" ]] &&
            grep -q 'Forwarding from 127.0.0.1:' "$log_file"; then
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
    local contract_base_url
    local transport
    local forward_log="$repetition_dir/contract-port-forward.log"
    local contract_log="$repetition_dir/contract.log"

    if [[ -n "$DIRECT_ORIGIN_BASE_URL" ]]; then
        contract_base_url="$DIRECT_ORIGIN_BASE_URL"
        transport="direct-cluster-dns"
    else
        local local_port
        local_port="$(find_free_local_port)"
        contract_base_url="http://127.0.0.1:$local_port"
        transport="port-forward"
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
    fi
    jq -n \
        --arg capturedAt "$(timestamp)" \
        --arg transport "$transport" \
        --arg baseUrl "$contract_base_url" \
        '{
            capturedAt: $capturedAt,
            transport: $transport,
            baseUrl: $baseUrl
        }' > "$repetition_dir/contract-transport.json"

    set +e
    "$PYTHON_BIN" "$CONTRACT_SCRIPT" \
        "$contract_base_url" \
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

    local prometheus_transport="direct-http"
    if [[ -n "$prometheus_service" &&
        "$CLUSTER_SERVICE_TRANSPORT" == "port-forward" ]]; then
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
        prometheus_transport="port-forward"
    elif [[ -n "$prometheus_service" ]]; then
        prometheus_transport="direct-cluster-dns"
    fi
    jq -n \
        --arg capturedAt "$(timestamp)" \
        --arg transport "$prometheus_transport" \
        --arg sourceUrl "$(jq -r '.baseUrl' <<<"$PROMETHEUS_INSPECTION")" \
        --arg queryUrl "$query_url" \
        '{
            capturedAt: $capturedAt,
            transport: $transport,
            sourceUrl: $sourceUrl,
            queryUrl: $queryUrl
        }' > "$ARTIFACT_DIR/samples/prometheus-transport.json"

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
    loadgen_exec test -f "$remote" >/dev/null 2>&1 ||
        return 1
    loadgen_copy_from "$remote" "$local_file" ||
        return 1
    [[ -s "$local_file" ]]
}

copy_local_file() {
    local local_file="$1"
    local remote_file="$2"
    local expected_sha256
    local actual_sha256

    expected_sha256="$(sha256sum -- "$local_file" | awk '{print $1}')"
    loadgen_exec test ! -e "$remote_file" || return 1
    loadgen_copy_to "$local_file" "$remote_file" || return 1
    actual_sha256="$(
        loadgen_exec sha256sum "$remote_file" |
            awk '{print $1}'
    )"
    [[ "$actual_sha256" == "$expected_sha256" ]]
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

validate_ssh_l4_transport_evidence() {
    local artifact="$1"
    local helper_status="$2"
    local expected_transport_output="$3"

    jq -e \
        --argjson helperStatus "$helper_status" \
        --arg expectedTransportOutput "$expected_transport_output" \
        '
        def exact_keys($expected):
            (keys | sort) == ($expected | sort);
        def timestamp:
            type == "string"
            and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?Z$");
        def probe($transportStarted; $transportFinished):
            exact_keys(["attempted", "passed", "at", "httpStatus"])
            and (.attempted | type) == "boolean"
            and (.passed | type) == "boolean"
            and (
                if .attempted
                then (.at | timestamp)
                    and $transportStarted <= .at
                    and .at <= $transportFinished
                else .at == null
                end
            )
            and (.httpStatus == null or (
                (.httpStatus | type) == "number"
                and .httpStatus == (.httpStatus | floor)
            ))
            and .passed == (.attempted and .httpStatus == 200);
        def expected_backends:
            [range(1; 9) | {
                id: "lane-\(.)",
                listenHost: "127.0.0.1",
                listenPort: (18080 + .),
                targetHost: "rke2-traefik.kube-system.svc.cluster.local",
                targetPort: 80
            }];
        def lane($id; $port; $transportStarted; $transportFinished):
            exact_keys([
                "id", "listenPort", "startAttempted", "started", "startedAt",
                "preProbe", "postProbe", "exitedEarly", "exitStatus",
                "stoppedByHelper"
            ])
            and .id == $id
            and .listenPort == $port
            and (.startAttempted | type) == "boolean"
            and (.started | type) == "boolean"
            and (
                if .started
                then (.startedAt | timestamp)
                    and $transportStarted <= .startedAt
                    and .startedAt <= $transportFinished
                else .startedAt == null
                end
            )
            and (.preProbe | probe($transportStarted; $transportFinished))
            and (.postProbe | probe($transportStarted; $transportFinished))
            and (.exitedEarly | type) == "boolean"
            and (.exitStatus == null or (
                (.exitStatus | type) == "number"
                and .exitStatus == (.exitStatus | floor)
            ))
            and (.stoppedByHelper | type) == "boolean"
            and (if .started then .startAttempted else true end)
            and (if .preProbe.attempted then .started else true end)
            and (if .postProbe.attempted
                then .preProbe.attempted else true end)
            and (if .stoppedByHelper then .started else true end)
            and (if .exitedEarly then .startAttempted else true end)
            and (if .exitStatus != null then .startAttempted else true end)
            and (
                if .startAttempted == false
                then .started == false
                    and .preProbe.attempted == false
                    and .postProbe.attempted == false
                    and .exitedEarly == false
                    and .exitStatus == null
                    and .stoppedByHelper == false
                elif .started == false
                then .exitedEarly == true
                    and (.exitStatus | type) == "number"
                    and .stoppedByHelper == false
                else (.exitStatus | type) == "number"
                    and (.exitedEarly != .stoppedByHelper)
                end
            )
            and (if .started and .preProbe.attempted
                then .startedAt <= .preProbe.at else true end)
            and (if .preProbe.attempted and .postProbe.attempted
                then .preProbe.at <= .postProbe.at else true end);
        def healthy_lane($transportStarted; $transportFinished):
            .startAttempted == true
            and .started == true
            and ((.startedAt | type) == "string" and (.startedAt | length) > 0)
            and .preProbe == {
                attempted: true,
                passed: true,
                at: .preProbe.at,
                httpStatus: 200
            }
            and ((.preProbe.at | type) == "string" and (.preProbe.at | length) > 0)
            and .postProbe == {
                attempted: true,
                passed: true,
                at: .postProbe.at,
                httpStatus: 200
            }
            and ((.postProbe.at | type) == "string" and (.postProbe.at | length) > 0)
            and $transportStarted <= .startedAt
            and .startedAt <= .preProbe.at
            and .preProbe.at <= .postProbe.at
            and .postProbe.at <= $transportFinished
            and .exitedEarly == false
            and (.exitStatus | type) == "number"
            and .exitStatus == (.exitStatus | floor)
            and .stoppedByHelper == true;
        def command_receipt($id; $output):
            exact_keys([
                "kind", "formatVersion", "sessionId", "sessionOutput",
                "activeLock", "startedAt", "completedAt", "lease",
                "termination", "passed"
            ])
            and .kind == "runpod-command-session"
            and .formatVersion == 1
            and .sessionId == $id
            and .sessionOutput == $output
            and .activeLock == "/tmp/bluemap-runpod-active-phase.lock"
            and ((.startedAt | type) == "string" and (.startedAt | length) > 0)
            and ((.completedAt | type) == "string" and (.completedAt | length) > 0)
            and (.startedAt | timestamp)
            and (.completedAt | timestamp)
            and .startedAt <= .completedAt
            and (.lease | exact_keys([
                "required", "eofObserved", "protocolViolation", "observedAt"
            ]))
            and .lease.required == true
            and (.lease.eofObserved | type) == "boolean"
            and .lease.protocolViolation == false
            and (
                if .lease.eofObserved
                then (.lease.observedAt | timestamp)
                    and .startedAt <= .lease.observedAt
                    and .lease.observedAt <= .completedAt
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
                then .termination.requested == true else true end);
        def command_session($root):
            . as $session
            | exact_keys([
                "required", "id", "outputPath", "leaseClosedByHelper",
                "leaseCloseReason", "confirmationAttempted", "confirmed",
                "receipt"
            ])
            and (.required | type) == "boolean"
            and ((.id | type) == "string"
                and (.id | test("^[a-f0-9]{64}$")))
            and .outputPath == (
                $expectedTransportOutput + ".command-session." + .id + ".json"
            )
            and (.leaseClosedByHelper | type) == "boolean"
            and (.confirmationAttempted | type) == "boolean"
            and (.confirmed | type) == "boolean"
            and (
                if .required
                then .leaseClosedByHelper == true
                    and (.leaseCloseReason == "after-command-exit"
                        or .leaseCloseReason == "lane-failure"
                        or .leaseCloseReason == "helper-deadline"
                        or .leaseCloseReason == "local-exit-timeout")
                    and .confirmationAttempted == true
                    and (
                        if .confirmed
                        then (.receipt
                            | command_receipt($session.id; $session.outputPath))
                            and $root.startedAt <= .receipt.startedAt
                            and .receipt.completedAt <= $root.finishedAt
                            and (
                                $root.commandExitStatus == null
                                or
                                .receipt.lease.eofObserved
                                or .receipt.termination.commandExitStatus
                                    == $root.commandExitStatus
                            )
                        else .receipt == null
                        end
                    )
                else .leaseClosedByHelper == false
                    and .leaseCloseReason == null
                    and .confirmationAttempted == false
                    and .confirmed == false
                    and .receipt == null
                end
            );
        . as $root
        |
        exact_keys([
            "formatVersion", "kind", "mode", "startedAt", "finishedAt",
            "topology", "allRequired", "commandExitStatus",
            "commandTerminatedForLaneFailure", "commandSession", "lanes",
            "failure", "passed"
        ])
        and .formatVersion == 1
        and .kind == "ssh-l4-traefik-transport"
        and .mode == "ssh-l4-traefik"
        and (.startedAt | timestamp)
        and (.finishedAt | timestamp)
        and .startedAt <= .finishedAt
        and .topology == {
            formatVersion: 1,
            balancer: "haproxy-tcp-static-rr",
            frontend: {host: "127.0.0.1", port: 18080},
            tunnelCount: 8,
            backends: expected_backends,
            healthPolicy: "all-required"
        }
        and .allRequired == true
        and (.commandExitStatus == null or (
            (.commandExitStatus | type) == "number"
            and .commandExitStatus == (.commandExitStatus | floor)
        ))
        and (.commandTerminatedForLaneFailure | type) == "boolean"
        and (.commandSession | command_session($root))
        and (.lanes | type) == "array"
        and (.lanes | length) == 8
        and all(
            range(0; 8);
            . as $index
            | ($root.lanes[$index]
                | lane(
                    "lane-\($index + 1)";
                    18081 + $index;
                    $root.startedAt;
                    $root.finishedAt
                ))
        )
        and (.failure == null or ((.failure | type) == "string" and (.failure | length) > 0))
        and (.passed | type) == "boolean"
        and .passed == (
            .commandTerminatedForLaneFailure == false
            and .failure == null
            and .commandSession.required == true
            and .commandSession.confirmed == true
            and .commandSession.leaseCloseReason == "after-command-exit"
            and .commandSession.receipt.lease.eofObserved == false
            and .commandSession.receipt.termination.requested == false
            and .commandSession.receipt.termination.killEscalated == false
            and .commandSession.receipt.termination.commandExitStatus
                == .commandExitStatus
            and all(
                .lanes[];
                healthy_lane($root.startedAt; $root.finishedAt)
            )
        )
        and (
            if .passed
            then (.commandExitStatus | type) == "number"
                and .commandExitStatus == $helperStatus
            else $helperStatus == 86
            end
        )
        ' "$artifact" >/dev/null
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
    local remote_resources="$remote_dir/load-generator-resources.ndjson"
    local remote_transport="$remote_dir/ssh-l4-transport.json"
    local phase_timeout_seconds
    local -a phase_command
    mkdir -- "$local_dir" || return 1
    if ! verify_service_endpoints \
        "repetition-$repetition_name-$phase-start"; then
        record_failure \
            "Repetition $repetition $phase: ready EndpointSlice targets changed at phase start"
        return 1
    fi
    # $1 is expanded by the shell inside the load-generator container.
    # shellcheck disable=SC2016
    loadgen_exec \
        sh -ceu 'mkdir "$1"' sh "$remote_dir" ||
        return 1

    set_phase "$repetition" "$phase"
    record_phase_event "$repetition" "$phase" "start"

    local enforce_latency_gates="false"
    if [[ "$phase" == "measurement" ]]; then
        enforce_latency_gates="true"
    fi
    phase_timeout_seconds="$(
        "$PYTHON_BIN" -c '
import math
import re
import sys

match = re.fullmatch(r"([1-9][0-9]*)(ms|s|m|h)", sys.argv[1])
if match is None:
    raise SystemExit(1)
value = int(match.group(1))
factor = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match.group(2)]
print(math.ceil(value * factor) + 90)
' "$duration"
    )" || return 1

    phase_command=(
        env K6_NO_USAGE_REPORT=true K6_NO_COLOR=true
        k6 run
        --summary-export "$remote_summary"
        --out "json=$remote_raw"
        -e "BASE_URL=$BASE_URL"
        -e "MANIFEST=/artifacts/$CASE_ID/inputs/manifest.json"
        -e "PROFILE=$PROFILE"
        -e "RATE=$RATE"
        -e "DURATION=$duration"
        -e "PRE_ALLOCATED_VUS=$PRE_ALLOCATED_VUS"
        -e "MAX_VUS=$MAX_VUS"
        -e "VIEWERS=$VIEWERS"
        -e "MARKER_INTERVAL_SECONDS=$MARKER_INTERVAL_SECONDS"
        -e "MIN_ACHIEVED_RATE_RATIO=$MIN_ACHIEVED_RATE_RATIO"
        -e "TRACE_SEED=$TRACE_SEED"
        -e "LATENCY_P95_MS=$LATENCY_P95_MS"
        -e "LATENCY_P99_MS=$LATENCY_P99_MS"
        -e "LARGE_OBJECT_LATENCY_P95_MS=$LARGE_OBJECT_LATENCY_P95_MS"
        -e "LARGE_OBJECT_LATENCY_P99_MS=$LARGE_OBJECT_LATENCY_P99_MS"
        -e "ENFORCE_LATENCY_GATES=$enforce_latency_gates"
        -e "FORMAL_RUN_ID=$FORMAL_RUN_ID"
        -e "REQUIRE_EDGE_BYPASS=$REQUIRE_EDGE_BYPASS"
        -e "TRAFFIC_MODE=$TRAFFIC_MODE"
        -e "EXPERIMENT_ID=$CASE_ID-r$repetition_name-$phase"
        -e "ACCEPT_ENCODING=$ACCEPT_ENCODING"
        -e "STORED_ENCODING=$STORED_ENCODING"
        -e "CONTRACT_MODE=$CONTRACT_MODE"
        "/artifacts/$CASE_ID/inputs/bluemap.js"
    )
    if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
        phase_command=(
            env "BLUEMAP_PHASE_TIMEOUT_SECONDS=$phase_timeout_seconds"
            bluemap-runpod-run-phase
            --resource-output "$remote_resources"
            --
            "${phase_command[@]}"
        )
    fi

    set +e
    loadgen_k6_exec "$remote_transport" "${phase_command[@]}" \
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
    if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]] &&
        ! copy_remote_file \
            "$remote_resources" \
            "$local_dir/load-generator-resources.ndjson"; then
        record_failure \
            "Repetition $repetition $phase: RunPod resource telemetry is missing"
        artifact_failure=1
    elif [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]] &&
        ! "$PYTHON_BIN" "$SCRIPT_DIR/check_load_generator_capacity.py" \
            "$local_dir/load-generator-resources.ndjson" \
            --identity "$ARTIFACT_DIR/generator/frozen-identity.json" \
            --runtime-identity \
            "$ARTIFACT_DIR/generator/live-identity-before.json" \
            --output "$local_dir/load-generator-capacity.json"; then
        record_failure \
            "Repetition $repetition $phase: RunPod load generator exceeded its capacity gate"
        artifact_failure=1
    fi
    if [[ "$LOADGEN_BACKEND" == "runpod-ssh" &&
        "$TRAFFIC_MODE" == "ssh-l4-traefik" ]] &&
        ! copy_remote_file \
            "$remote_transport" \
            "$local_dir/ssh-l4-transport.json"; then
        die "Repetition $repetition $phase: SSH L4 transport evidence is missing; remote phase termination is unconfirmed"
    elif [[ "$LOADGEN_BACKEND" == "runpod-ssh" &&
        "$TRAFFIC_MODE" == "ssh-l4-traefik" ]] &&
        ! validate_ssh_l4_transport_evidence \
            "$local_dir/ssh-l4-transport.json" \
            "$status" \
            "$remote_transport"; then
        die "Repetition $repetition $phase: SSH L4 transport or command-session evidence failed validation"
    elif [[ "$LOADGEN_BACKEND" == "runpod-ssh" &&
        "$TRAFFIC_MODE" == "ssh-l4-traefik" ]] &&
        jq -e '
            .commandSession.required == true
            and .commandSession.confirmed != true
        ' "$local_dir/ssh-l4-transport.json" >/dev/null; then
        die "Repetition $repetition $phase: remote process-group termination is unconfirmed"
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
    local prometheus_transport=""
    local schedule_enabled=false
    local variant_metadata_enabled=false
    local matrix_sha256=""
    local schedule_sha256=""
    local generator_identity_json
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
        if [[ -n "$(jq -r '.clusterService.service // empty' \
            <<<"$PROMETHEUS_INSPECTION")" ]]; then
            prometheus_transport="$CLUSTER_SERVICE_TRANSPORT"
        else
            prometheus_transport="direct-http"
        fi
    fi
    if [[ -n "$SCHEDULE_ENTRY_JSON" ]]; then
        schedule_enabled=true
        matrix_sha256="$(sha256sum "$MATRIX" | awk '{print $1}')"
        schedule_sha256="$(sha256sum "$SCHEDULE" | awk '{print $1}')"
    fi
    if [[ -n "$VARIANT_ID" ]]; then
        variant_metadata_enabled=true
    fi
    if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
        generator_identity_json="$(jq -cS . "$LOADGEN_IDENTITY")"
    else
        generator_identity_json="$(
            jq -nc \
                --arg pod "$LOADGEN_POD" \
                '{
                    formatVersion: 1,
                    backend: "kubernetes",
                    pod: $pod
                }'
        )"
    fi

    jq -n \
        --arg capturedAt "$(timestamp)" \
        --arg caseId "$CASE_ID" \
        --arg namespace "$NAMESPACE" \
        --arg service "$SERVICE" \
        --argjson servicePort "$SERVICE_PORT" \
        --arg originBaseUrl "$ORIGIN_BASE_URL" \
        --arg trafficBaseUrl "$BASE_URL" \
        --arg trafficMode "$TRAFFIC_MODE" \
        --arg trafficService "$TRAFFIC_SERVICE" \
        --arg trafficServicePort "$TRAFFIC_SERVICE_PORT" \
        --arg clusterServiceTransport "$CLUSTER_SERVICE_TRANSPORT" \
        --arg prometheusTransport "$prometheus_transport" \
        --arg loadGeneratorBackend "$LOADGEN_BACKEND" \
        --arg formalRunId "$FORMAL_RUN_ID" \
        --argjson requireEdgeBypass "$REQUIRE_EDGE_BYPASS" \
        --argjson generatorIdentity "$generator_identity_json" \
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
        --arg loadGeneratorCapacitySha256 "$(sha256sum "$SCRIPT_DIR/check_load_generator_capacity.py" | awk '{print $1}')" \
        --arg slowReaderSha256 "$(sha256sum "$SCRIPT_DIR/slow_reader.py" | awk '{print $1}')" \
        --arg runpodLoadgenHelperSha256 "$(sha256sum "$RUNPOD_LOADGEN_HELPER" | awk '{print $1}')" \
        --arg runtimeIdentitySha256 "$(sha256sum "$RUNTIME_IDENTITY_SCRIPT" | awk '{print $1}')" \
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
                baseUrl: $originBaseUrl,
                correctnessTransport: $clusterServiceTransport
            },
            traffic: {
                mode: (
                    if $trafficMode == "" then null else $trafficMode end
                ),
                baseUrl: $trafficBaseUrl,
                service: (
                    if $trafficService == "" then null else $trafficService end
                ),
                port: (
                    if $trafficServicePort == ""
                    then null
                    else ($trafficServicePort | tonumber)
                    end
                ),
                formalRunId: (
                    if $formalRunId == "" then null else $formalRunId end
                ),
                requiresEdgeBypass: $requireEdgeBypass,
                tunnel: (
                    if $trafficMode == "ssh-l4-traefik"
                    then {
                        formatVersion: 1,
                        balancer: "haproxy-tcp-static-rr",
                        frontend: {
                            host: "127.0.0.1",
                            port: 18080
                        },
                        tunnelCount: 8,
                        backends: [
                            {
                                id: "lane-1",
                                listenHost: "127.0.0.1",
                                listenPort: 18081,
                                targetHost: "rke2-traefik.kube-system.svc.cluster.local",
                                targetPort: 80
                            },
                            {
                                id: "lane-2",
                                listenHost: "127.0.0.1",
                                listenPort: 18082,
                                targetHost: "rke2-traefik.kube-system.svc.cluster.local",
                                targetPort: 80
                            },
                            {
                                id: "lane-3",
                                listenHost: "127.0.0.1",
                                listenPort: 18083,
                                targetHost: "rke2-traefik.kube-system.svc.cluster.local",
                                targetPort: 80
                            },
                            {
                                id: "lane-4",
                                listenHost: "127.0.0.1",
                                listenPort: 18084,
                                targetHost: "rke2-traefik.kube-system.svc.cluster.local",
                                targetPort: 80
                            },
                            {
                                id: "lane-5",
                                listenHost: "127.0.0.1",
                                listenPort: 18085,
                                targetHost: "rke2-traefik.kube-system.svc.cluster.local",
                                targetPort: 80
                            },
                            {
                                id: "lane-6",
                                listenHost: "127.0.0.1",
                                listenPort: 18086,
                                targetHost: "rke2-traefik.kube-system.svc.cluster.local",
                                targetPort: 80
                            },
                            {
                                id: "lane-7",
                                listenHost: "127.0.0.1",
                                listenPort: 18087,
                                targetHost: "rke2-traefik.kube-system.svc.cluster.local",
                                targetPort: 80
                            },
                            {
                                id: "lane-8",
                                listenHost: "127.0.0.1",
                                listenPort: 18088,
                                targetHost: "rke2-traefik.kube-system.svc.cluster.local",
                                targetPort: 80
                            }
                        ],
                        healthPolicy: "all-required"
                    }
                    else null
                    end
                )
            },
            loadGenerator: {
                backend: $loadGeneratorBackend,
                identity: $generatorIdentity
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
                    clusterServiceTransport: (
                        if $prometheusEnabled
                        then $prometheusTransport
                        else null
                        end
                    ),
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
                        end
                    ),
                    maximumNonTargetNodeCpuMeanCores: (
                        if $prometheusEnabled
                        then $maximumNonTargetNodeCpuMeanCores
                        else null
                        end
                    ),
                    maximumNonTargetNodeCpuLevelCores: (
                        if $prometheusEnabled
                        then $maximumNonTargetNodeCpuLevelCores
                        else null
                        end
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
                loadGeneratorCapacitySha256: $loadGeneratorCapacitySha256,
                slowReaderSha256: $slowReaderSha256,
                runpodLoadgenHelperSha256: $runpodLoadgenHelperSha256,
                runtimeIdentitySha256: $runtimeIdentitySha256
            }
        }' > "$ARTIFACT_DIR/inputs/workload.json"
}

SAMPLE_TARGETS=()
ALL_PODS=()
if [[ "$LOADGEN_BACKEND" == "kubernetes" ]]; then
    SAMPLE_TARGETS+=("loadgen:$LOADGEN_POD")
    ALL_PODS+=("$LOADGEN_POD")
fi
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

ORIGIN_BASE_URL="http://$SERVICE.$NAMESPACE.svc.cluster.local:$SERVICE_PORT"
BASE_URL="$ORIGIN_BASE_URL"
if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
    BASE_URL="${TRAFFIC_BASE_URL%/}"
fi

kube get service "$SERVICE" -o name >/dev/null
if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
    validate_traffic_service ||
        die "Traffic Service '$TRAFFIC_SERVICE' differs from the fail-closed public route"
    validate_traffic_ingress ||
        die "Ingress '$TRAFFIC_INGRESS' does not exactly route $TRAFFIC_HOST to $TRAFFIC_SERVICE"
    mkdir -- "$ARTIFACT_DIR/generator"
    validate_runpod_load_generator \
        > "$ARTIFACT_DIR/generator/live-identity-before.json"
    jq -S . "$LOADGEN_IDENTITY" \
        > "$ARTIFACT_DIR/generator/frozen-identity.json"
else
    validate_load_generator_pod
fi
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
cp -- "$SCRIPT_DIR/check_load_generator_capacity.py" \
    "$ARTIFACT_DIR/inputs/check_load_generator_capacity.py"
cp -- "$SCRIPT_DIR/slow_reader.py" \
    "$ARTIFACT_DIR/inputs/slow_reader.py"
cp -- "$SCRIPT_DIR/generate_schedule.py" \
    "$ARTIFACT_DIR/inputs/generate_schedule.py"
cp -- "$RUNTIME_IDENTITY_SCRIPT" \
    "$ARTIFACT_DIR/inputs/runtime_identity.py"
cp -- "$RUNPOD_LOADGEN_HELPER" \
    "$ARTIFACT_DIR/inputs/runpod_loadgen.sh"
if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
    jq -S . "$LOADGEN_IDENTITY" \
        > "$ARTIFACT_DIR/inputs/runpod-load-generator-identity.json"
fi
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
        check_load_generator_capacity.py \
        slow_reader.py \
        generate_schedule.py \
        runtime_identity.py \
        runpod_loadgen.sh \
        workload.json > SHA256SUMS
    if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
        sha256sum runpod-load-generator-identity.json >> SHA256SUMS
    fi
    if [[ -n "$SCHEDULE_ENTRY_JSON" ]]; then
        sha256sum matrix.json schedule.json schedule-entry.json >> SHA256SUMS
    fi
)

REMOTE_ROOT="/artifacts/$CASE_ID"
# $1 is expanded by the shell inside the load-generator container.
# shellcheck disable=SC2016
loadgen_exec \
    sh -ceu \
    'umask 077; mkdir "$1"; mkdir "$1/inputs" "$1/repetitions"' \
    sh "$REMOTE_ROOT"
copy_local_file "$MANIFEST" "$REMOTE_ROOT/inputs/manifest.json" ||
    die "Could not copy and verify the manifest on the load generator"
copy_local_file "$K6_SCRIPT" "$REMOTE_ROOT/inputs/bluemap.js" ||
    die "Could not copy and verify the k6 script on the load generator"

capture_snapshot_set before
verify_web_pod_ownership before ||
    die "Named web Pods are not owned by the selected Deployments"
verify_formal_runtime_identity ||
    die "Formal runtime image, configuration, or runtime-spec identity does not match"
verify_service_endpoints before ||
    die "Ready EndpointSlice Pod targets do not exactly match --web-pod targets"
capture_restart_counts "$ARTIFACT_DIR/cluster/restarts-case-before.json" ||
    die "Could not capture restart counts or verify live Pod/image identities"

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
    # $1 is expanded by the shell inside the load-generator container.
    # shellcheck disable=SC2016
    if ! loadgen_exec \
        sh -ceu 'mkdir "$1"' \
        sh "$REMOTE_ROOT/repetitions/$repetition_name"; then
        record_failure \
            "Repetition $repetition: could not create the remote artifact directory"
        case_failed=1
        break
    fi
    if ! capture_restart_counts "$repetition_dir/restarts-before.json"; then
        record_failure \
            "Repetition $repetition: restart snapshot or live Pod/image identity failed before load"
        case_failed=1
        break
    fi

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

    if ! capture_restart_counts "$repetition_dir/restarts-after.json"; then
        record_failure \
            "Repetition $repetition: restart snapshot or live Pod/image identity failed after load"
        case_failed=1
    fi
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
if ! verify_web_pod_ownership after; then
    record_failure \
        "Named web Pod ownership changed or no longer matches the selected Deployments"
    case_failed=1
fi
if ! verify_service_endpoints after; then
    record_failure "Ready EndpointSlice targets changed by the end of the case"
    case_failed=1
fi
if ! capture_restart_counts "$ARTIFACT_DIR/cluster/restarts-case-after.json"; then
    record_failure "Final restart-count snapshot or live Pod/image identity failed"
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
if ! diff -u \
    <(jq -S 'del(.capturedAt)' \
        "$ARTIFACT_DIR/cluster/runtime-spec-digests-before.json") \
    <(jq -S 'del(.capturedAt)' \
        "$ARTIFACT_DIR/cluster/runtime-spec-digests-after.json") \
    > "$ARTIFACT_DIR/cluster/runtime-spec-digests.diff"; then
    record_failure "A selected Service or Deployment runtime spec changed during the case"
    case_failed=1
fi
if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]] &&
    ! diff -u \
        <(jq -S 'del(.capturedAt)' \
            "$ARTIFACT_DIR/cluster/before/service-$TRAFFIC_SERVICE.json") \
        <(jq -S 'del(.capturedAt)' \
            "$ARTIFACT_DIR/cluster/after/service-$TRAFFIC_SERVICE.json") \
        > "$ARTIFACT_DIR/cluster/traffic-service.diff"; then
    record_failure "The public traffic Service changed during the case"
    case_failed=1
fi
if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]] &&
    ! diff -u \
        <(jq -S 'del(.capturedAt)' \
            "$ARTIFACT_DIR/cluster/before/ingress-$TRAFFIC_INGRESS.json") \
        <(jq -S 'del(.capturedAt)' \
            "$ARTIFACT_DIR/cluster/after/ingress-$TRAFFIC_INGRESS.json") \
        > "$ARTIFACT_DIR/cluster/traffic-ingress.diff"; then
    record_failure "The public traffic Ingress changed during the case"
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
if [[ "$LOADGEN_BACKEND" == "runpod-ssh" ]]; then
    if ! validate_runpod_load_generator \
        > "$ARTIFACT_DIR/generator/live-identity-after.json"; then
        record_failure "Final RunPod load-generator identity validation failed"
        case_failed=1
    elif ! diff -u \
        <(jq -S 'del(.capturedAt)' \
            "$ARTIFACT_DIR/generator/live-identity-before.json") \
        <(jq -S 'del(.capturedAt)' \
            "$ARTIFACT_DIR/generator/live-identity-after.json") \
        > "$ARTIFACT_DIR/generator/live-identity.diff"; then
        record_failure "RunPod load-generator runtime identity changed"
        case_failed=1
    fi
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
