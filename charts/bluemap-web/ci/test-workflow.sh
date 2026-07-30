#!/bin/sh
set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_root=$(CDPATH= cd -- "$script_directory/../../.." && pwd)
workflow="$repository_root/.github/workflows/web.yml"

assert_contains() {
    grep -F -- "$2" "$1" >/dev/null || {
        echo "expected $1 to contain: $2" >&2
        exit 1
    }
}

assert_absent() {
    if grep -F -- "$2" "$1" >/dev/null; then
        echo "expected $1 not to contain: $2" >&2
        exit 1
    fi
}

assert_contains "$workflow" 'group: standalone-web-${{ github.workflow }}-${{ github.ref }}'
assert_contains "$workflow" "cancel-in-progress: true"
assert_contains "$workflow" 'app_version="sha-${GITHUB_SHA}"'
assert_contains "$workflow" 'chart_version="0.1.0-dev.sha.${GITHUB_SHA}"'
assert_absent "$workflow" "format=short"
assert_absent "$workflow" 'GITHUB_SHA:0:7'
assert_contains "$workflow" ":core:spotlessCheck :core:test"
assert_contains "$workflow" ":common:spotlessCheck :common:test"
assert_contains "$workflow" "python -m unittest discover"
assert_contains "$workflow" "charts/bluemap-web/ci/test-rust.sh"
assert_contains "$workflow" \
    "benchmarks/web-performance/kubernetes/test-helm-values.sh"
assert_contains "$workflow" "needs: validate"

long_sha_tags=$(grep -F -c "type=sha,format=long,prefix=sha-" "$workflow")
if [ "$long_sha_tags" -ne 3 ]; then
    echo "expected all three images to have one full-SHA tag, found $long_sha_tags" >&2
    exit 1
fi

assert_contains "$workflow" "cargo fmt --check"
assert_contains "$workflow" 'toolchain: "1.97.0"'
assert_contains "$workflow" "cargo test --target x86_64-unknown-linux-gnu --all-targets --locked"
assert_contains "$workflow" "cargo clippy --target x86_64-unknown-linux-gnu --all-targets --locked -- -D warnings"
checks_line=$(grep -n -F "name: Run Rust checks" "$workflow" | cut -d: -f1)
smoke_line=$(grep -n -F "name: Run mandatory Rust image smoke test" "$workflow" | cut -d: -f1)
if [ "$checks_line" -ge "$smoke_line" ]; then
    echo "Rust source checks must run before the image smoke test" >&2
    exit 1
fi
