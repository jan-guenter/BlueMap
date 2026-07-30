#!/bin/sh
set -eu

script_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repository_root=$(CDPATH='' cd -- "$script_directory/../.." && pwd)
image="bluemap-rust-web:smoke-$$"
container=
temporary=$(mktemp -d)

cleanup() {
    if [ -n "$container" ]; then
        docker rm -f "$container" >/dev/null 2>&1 || true
    fi
    docker image rm "$image" >/dev/null 2>&1 || true
    rm -rf "$temporary"
}
trap cleanup EXIT INT TERM

docker build \
    -f "$repository_root/docker/rust-web/Dockerfile" \
    --build-arg BLUEMAP_VERSION=smoke \
    -t "$image" \
    "$repository_root"

container=$(docker run -d --read-only --user 10001:10001 \
    -p 127.0.0.1::8100 \
    -v "$repository_root/docker/rust-web/test/config.toml:/etc/bluemap-web/config.toml:ro" \
    -v "$repository_root/docker/rust-web/test/maps:/data/maps:ro" \
    "$image")
port=$(docker inspect \
    --format '{{(index (index .NetworkSettings.Ports "8100/tcp") 0).HostPort}}' \
    "$container")

attempt=0
until curl -fsS "http://127.0.0.1:$port/health/ready" >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 50 ]; then
        docker logs "$container"
        exit 1
    fi
    sleep 0.1
done

curl -fsS "http://127.0.0.1:$port/lang/settings.conf" \
    -o "$temporary/settings.conf"
curl -fsS "http://127.0.0.1:$port/assets/logo.png" \
    -o "$temporary/logo.png"
test -s "$temporary/settings.conf"
test -s "$temporary/logo.png"
