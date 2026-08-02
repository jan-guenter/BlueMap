#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

: "${UPSTREAM_URL:?Set UPSTREAM_URL to the direct upstream built-in server URL}"
: "${UPSTREAM_PHP_URL:?Set UPSTREAM_PHP_URL to the direct upstream PHP URL}"
: "${NEW_JAVA_URL:?Set NEW_JAVA_URL to the direct new Java server URL}"
: "${UPSTREAM_ID:?Set UPSTREAM_ID to the exact upstream revision or artifact digest}"
: "${NEW_JAVA_ID:?Set NEW_JAVA_ID to the exact new Java revision or artifact digest}"
: "${DATASET_ID:?Set DATASET_ID to the immutable database snapshot identifier}"
: "${SETUP_MANIFEST:?Set SETUP_MANIFEST to the reviewed benchmark setup JSON}"
: "${PATHS_FILE:?Set PATHS_FILE to the frozen /maps path list}"

arguments=(
  --upstream-url "$UPSTREAM_URL"
  --upstream-php-url "$UPSTREAM_PHP_URL"
  --new-java-url "$NEW_JAVA_URL"
  --upstream-id "$UPSTREAM_ID"
  --new-java-id "$NEW_JAVA_ID"
  --dataset-id "$DATASET_ID"
  --setup-manifest "$SETUP_MANIFEST"
  --paths "$PATHS_FILE"
  --vus 12
  --warmup-duration "${WARMUP_DURATION:-30s}"
  --duration "${DURATION:-120s}"
  --repetitions "${REPETITIONS:-5}"
  --accept-encoding "${ACCEPT_ENCODING:-zstd}"
  --required-content-encoding "${REQUIRED_CONTENT_ENCODING:-zstd}"
  --preflight-timeout-seconds "${PREFLIGHT_TIMEOUT_SECONDS:-30}"
  --k6 "${K6_BIN:-k6}"
)

if [[ -n "${OUTPUT_DIR:-}" ]]; then
  arguments+=(--output "$OUTPUT_DIR")
fi

exec python3 "$script_dir/run_benchmark.py" "${arguments[@]}"
