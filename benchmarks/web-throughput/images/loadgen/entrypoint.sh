#!/bin/sh
set -eu

# shellcheck source=benchmarks/web-throughput/images/common/bootstrap.sh
. /usr/local/libexec/bluemap-bootstrap.sh

bootstrap_start_ssh
bootstrap_wait_for_start

exec "$@"
