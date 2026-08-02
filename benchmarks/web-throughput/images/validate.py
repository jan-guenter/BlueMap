#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[3]
IMAGES = ROOT / "benchmarks" / "web-throughput" / "images"
UPSTREAM_REVISION = "e664c1abdf697c64703401dca1d7e1956f755f65"
UPSTREAM_SQL_SHA256 = "e160a9ecbd996b5c701f172a7b22bf73eec96670cf6508034e18251067bebb6b"
EXPECTED_FROM = {
    "upstream/Dockerfile": [
        "ghcr.io/bluemap-minecraft/bluemap@sha256:5760d82b0cc3dbc1ef2482a45a8d283e680687e60c57aa17dd9fa083184dc062"
    ],
    "php/Dockerfile": [
        "php:8.4-fpm-alpine3.22@sha256:e0f1ffe8bd43a137facdccdcac41cabdfc23e8d0fdd0665916a062f16e2f4058"
    ],
    "java/Dockerfile": [
        "eclipse-temurin:25-jre-jammy@sha256:b8ba5fca9d88b6ecc3a46c8e75b744f84aca9a9d08587901b5ab480baf641ab5"
    ],
    "loadgen/Dockerfile": [
        "grafana/k6:1.3.0@sha256:3ddc8b1a33a2c3d8edc6e99b6a762ae36cba08788463458f5e6a7703e14eb77d",
        "python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64",
    ],
    "mariadb/Dockerfile": [
        "mariadb@sha256:09336a8c0cff9f363e133d8a245cca5ad3eeb326e0ffaedf74d49214c4571486"
    ],
}
EXPECTED_DOCKERIGNORE = {
    "upstream": (
        "**",
        "!benchmarks/",
        "!benchmarks/web-throughput/",
        "!benchmarks/web-throughput/images/",
        "!benchmarks/web-throughput/images/common/",
        "!benchmarks/web-throughput/images/common/bootstrap.sh",
        "!benchmarks/web-throughput/images/common/sshd_config",
        "!benchmarks/web-throughput/images/upstream/",
        "!benchmarks/web-throughput/images/upstream/entrypoint.sh",
    ),
    "php": (
        "**",
        "!common/",
        "!common/webapp/",
        "!common/webapp/public/",
        "!common/webapp/public/sql.php",
        "!benchmarks/",
        "!benchmarks/web-throughput/",
        "!benchmarks/web-throughput/images/",
        "!benchmarks/web-throughput/images/common/",
        "!benchmarks/web-throughput/images/common/bootstrap.sh",
        "!benchmarks/web-throughput/images/common/sshd_config",
        "!benchmarks/web-throughput/images/php/",
        "!benchmarks/web-throughput/images/php/entrypoint.sh",
        "!benchmarks/web-throughput/images/php/nginx.conf",
        "!benchmarks/web-throughput/images/php/php-fpm-www.conf",
        "!benchmarks/web-throughput/images/php/php.ini",
    ),
    "java": (
        "**",
        "!build/",
        "!build/release/",
        "!build/release/*-webserver.jar",
        "!benchmarks/",
        "!benchmarks/web-throughput/",
        "!benchmarks/web-throughput/images/",
        "!benchmarks/web-throughput/images/common/",
        "!benchmarks/web-throughput/images/common/bootstrap.sh",
        "!benchmarks/web-throughput/images/common/sshd_config",
        "!benchmarks/web-throughput/images/java/",
        "!benchmarks/web-throughput/images/java/entrypoint.sh",
    ),
    "loadgen": (
        "**",
        "!benchmarks/",
        "!benchmarks/web-throughput/",
        "!benchmarks/web-throughput/run.sh",
        "!benchmarks/web-throughput/run_benchmark.py",
        "!benchmarks/web-throughput/throughput.js",
        "!benchmarks/web-throughput/images/",
        "!benchmarks/web-throughput/images/common/",
        "!benchmarks/web-throughput/images/common/bootstrap.sh",
        "!benchmarks/web-throughput/images/common/sshd_config",
        "!benchmarks/web-throughput/images/loadgen/",
        "!benchmarks/web-throughput/images/loadgen/entrypoint.sh",
    ),
    "mariadb": (
        "**",
        "!benchmarks/",
        "!benchmarks/web-throughput/",
        "!benchmarks/web-throughput/images/",
        "!benchmarks/web-throughput/images/common/",
        "!benchmarks/web-throughput/images/common/bootstrap.sh",
        "!benchmarks/web-throughput/images/common/sshd_config",
        "!benchmarks/web-throughput/images/mariadb/",
        "!benchmarks/web-throughput/images/mariadb/entrypoint.sh",
    ),
}


def fail(message: str) -> None:
    raise SystemExit(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    sql_php = ROOT / "common" / "webapp" / "public" / "sql.php"
    working_sql = sql_php.read_bytes()
    upstream_sql = subprocess.run(
        ["git", "show", f"{UPSTREAM_REVISION}:common/webapp/public/sql.php"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    if sha256(working_sql) != UPSTREAM_SQL_SHA256:
        fail("working-tree sql.php does not have the recorded upstream digest")
    if working_sql != upstream_sql:
        fail("working-tree sql.php is not byte-identical to the exact upstream revision")

    for role, expected_lines in EXPECTED_DOCKERIGNORE.items():
        ignore_file = IMAGES / role / "Dockerfile.dockerignore"
        actual_lines = tuple(ignore_file.read_text(encoding="utf-8").splitlines())
        if actual_lines != expected_lines:
            fail(
                f"{role}/Dockerfile.dockerignore does not expose only the reviewed "
                "repository-root build inputs"
            )

    for relative, expected_from in EXPECTED_FROM.items():
        dockerfile = IMAGES / relative
        actual_from = []
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if not line.startswith("FROM "):
                continue
            image = line.split(maxsplit=1)[1]
            image = re.split(r"\s+[Aa][Ss]\s+", image, maxsplit=1)[0]
            actual_from.append(image)
            if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
                fail(f"{relative} has an unpinned FROM reference: {image}")
        if actual_from != expected_from:
            fail(f"{relative} immutable bases differ: {actual_from!r}")

        text = dockerfile.read_text(encoding="utf-8")
        if "images/common/bootstrap.sh" not in text or "images/common/sshd_config" not in text:
            fail(f"{relative} does not install the shared bootstrap and SSH contract")
        if "/etc/ssh/ssh_host_*" not in text:
            fail(f"{relative} may retain build-time SSH host keys")

    php_entrypoint = (IMAGES / "php" / "entrypoint.sh").read_text(encoding="utf-8")
    required_php_fragments = (
        UPSTREAM_SQL_SHA256,
        "$markerOffset + strlen($marker)",
        "fastcgi_buffering off",
        "verifyChain = yes",
        '? "checkIP" : "checkHost"',
    )
    php_contract = php_entrypoint + (IMAGES / "php" / "nginx.conf").read_text(
        encoding="utf-8"
    )
    for fragment in required_php_fragments:
        if fragment not in php_contract:
            fail(f"PHP wrapper is missing contract fragment: {fragment}")

    mariadb_entrypoint = (IMAGES / "mariadb" / "entrypoint.sh").read_text(
        encoding="utf-8"
    )
    for fragment in (
        "--require-secure-transport=ON",
        "zstd --test",
        "data directory is not empty",
        "export MARIADB_DATABASE=bluemap",
        "docker-entrypoint.sh mariadbd",
        "--connect-timeout=5",
        "--max-connections=64",
    ):
        if fragment not in mariadb_entrypoint:
            fail(f"MariaDB wrapper is missing fail-closed fragment: {fragment}")
    if "iptables" in (IMAGES / "mariadb" / "Dockerfile").read_text(encoding="utf-8"):
        fail("MariaDB wrapper must not depend on unavailable firewall privileges")

    for candidate in ("upstream", "php", "java"):
        dockerfile = (IMAGES / candidate / "Dockerfile").read_text(encoding="utf-8")
        exposed = re.findall(r"(?m)^EXPOSE[ \t]+(.+)$", dockerfile)
        if exposed != ["22"]:
            fail(f"{candidate} wrapper must expose only SSH; HTTP remains loopback-only")
        if "curl" not in dockerfile:
            fail(f"{candidate} wrapper lacks required network tool curl")
        if "iptables" in dockerfile:
            fail(f"{candidate} wrapper must not depend on unavailable HTTP firewall privileges")

    php_nginx = (IMAGES / "php" / "nginx.conf").read_text(encoding="utf-8")
    php_listeners = re.findall(r"(?m)^[ \t]*listen[ \t]+([^;]+);", php_nginx)
    if php_listeners != ["127.0.0.1:8100"]:
        fail("PHP nginx must contain exactly one loopback-only 127.0.0.1:8100 listener")
    if "access_log off;" not in php_nginx:
        fail("PHP nginx request logging must be disabled")
    for candidate in ("upstream", "java"):
        entrypoint = (IMAGES / candidate / "entrypoint.sh").read_text(encoding="utf-8")
        if 'bootstrap_wait_for_path /bootstrap/tls/ca.crt' not in entrypoint:
            fail(f"{candidate} wrapper does not fail closed on the uploaded MariaDB CA")
        if "bootstrap_validate_java_webserver_config" not in entrypoint:
            fail(
                f"{candidate} wrapper does not enforce loopback port 8100 and "
                "disabled request logging"
            )
    upstream_entrypoint = (IMAGES / "upstream" / "entrypoint.sh").read_text(encoding="utf-8")
    if " -b" in upstream_entrypoint or "--verbose" in upstream_entrypoint:
        fail("upstream CLI verbose request logging must remain disabled")

    bootstrap = (IMAGES / "common" / "bootstrap.sh").read_text(encoding="utf-8")
    if "BENCHMARK_SSH_PUBLIC_KEY" not in bootstrap:
        fail("shared bootstrap does not consume the RunPod public-key input")
    for fragment in (
        "/usr/sbin/sshd -t -f /etc/ssh/sshd_config",
        "/usr/sbin/sshd -D -e -f /etc/ssh/sshd_config &",
        "printf '%s\\n' \"$sshd_pid\" > /run/sshd.pid",
        'kill -0 "$sshd_pid"',
        'configured_ips="$(awk',
        'bootstrap_fail "webserver config must contain exactly one active ip: 127.0.0.1 setting"',
    ):
        if fragment not in bootstrap:
            fail(f"shared bootstrap is missing explicit sshd lifecycle fragment: {fragment}")
    for candidate in EXPECTED_FROM:
        if "images/common/bootstrap.sh" not in (IMAGES / candidate).read_text(encoding="utf-8"):
            fail(f"{candidate} does not consume the shared public-key bootstrap")

    workflow = (
        ROOT / ".github" / "workflows" / "runpod-throughput-images.yml"
    ).read_text(encoding="utf-8")
    for fragment in (
        "benchmark/runpod-mariadb-throughput-v1",
        "platforms: linux/amd64",
        "packages: write",
        "provenance: mode=max",
        "sbom: true",
        "build-lock.json",
        "bluemap-perf-loadgen:upstream-",
        "bluemap-perf-loadgen:mariadb-",
        "smoke-client-tls.cnf",
        "ssl-verify-server-cert",
        "tcp-tls-ca-host-verification=pass",
    ):
        if fragment not in workflow:
            fail(f"workflow is missing required fragment: {fragment}")
    if "linux/arm64" in workflow:
        fail("benchmark workflow must not build arm64 images")

    setup_example = json.loads(
        (ROOT / "benchmarks" / "web-throughput" / "setup.example.json").read_text(
            encoding="utf-8"
        )
    )
    transport = setup_example.get("transport")
    expected_transport = {
        "type": "host-key-pinned SSH local forwarding",
        "initiator": "load-generator",
        "sshHostKeysPinned": True,
        "laneCountPerTarget": 12,
        "originBindAddress": "127.0.0.1",
        "originPort": 8100,
        "loadGeneratorTcpBalancer": "HAProxy mode tcp",
        "candidatePublicHttp": False,
    }
    if transport != expected_transport:
        fail("setup example does not freeze the reviewed loopback/SSH-lane transport")
    if setup_example.get("directOrigin") is not True:
        fail("setup example must retain direct-origin HTTP semantics")
    if "SSH L4 forwarding" not in setup_example.get("protocol", ""):
        fail("setup example protocol does not describe the SSH L4 transport")

    print("five immutable benchmark role images validated")


if __name__ == "__main__":
    main()
