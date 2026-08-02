#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[3]
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing required environment variable {name}")
    return value


def require_digest(name: str) -> str:
    value = required(name)
    if not DIGEST.fullmatch(value):
        raise SystemExit(f"{name} is not an immutable sha256 digest: {value}")
    return value


def file_sha256(relative: str) -> str:
    return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def built_image(prefix: str, dockerfile: str, role: str) -> dict[str, str]:
    tag = required(f"{prefix}_IMAGE")
    digest = require_digest(f"{prefix}_DIGEST")
    package, tag_name = tag.rsplit(":", 1)
    return {
        "role": role,
        "tag": tag,
        "digest": digest,
        "immutableRef": f"{package}@{digest}",
        "dockerfile": dockerfile,
        "dockerfileSha256": file_sha256(dockerfile),
        "deletionIdentity": f"{package}:{tag_name}@{digest}",
    }


def main() -> None:
    revision = required("GITHUB_SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SystemExit("GITHUB_SHA must be a full commit hash")

    lock = {
        "schemaVersion": 1,
        "platform": "linux/amd64",
        "source": {
            "repository": required("GITHUB_REPOSITORY"),
            "url": required("SOURCE_URL"),
            "revision": revision,
            "ref": required("GITHUB_REF"),
            "workflowRunId": required("GITHUB_RUN_ID"),
            "workflowRunAttempt": required("GITHUB_RUN_ATTEMPT"),
            "created": required("IMAGE_CREATED"),
        },
        "built": {
            "upstream": built_image(
                "UPSTREAM",
                "benchmarks/web-throughput/images/upstream/Dockerfile",
                "upstream",
            ),
            "php": built_image(
                "PHP", "benchmarks/web-throughput/images/php/Dockerfile", "upstream-php"
            ),
            "java": built_image(
                "JAVA", "benchmarks/web-throughput/images/java/Dockerfile", "new-java"
            ),
            "loadGenerator": built_image(
                "LOADGEN",
                "benchmarks/web-throughput/images/loadgen/Dockerfile",
                "load-generator",
            ),
            "mariadb": built_image(
                "MARIADB",
                "benchmarks/web-throughput/images/mariadb/Dockerfile",
                "mariadb",
            ),
        },
        "baseImages": {
            "upstreamApplication": {
                "indexRef": "ghcr.io/bluemap-minecraft/bluemap@sha256:93ced47a5a36c6e5e9337af8f6ca3cf815010011af65eabd571ca42e100f6aba",
                "amd64Ref": "ghcr.io/bluemap-minecraft/bluemap@sha256:5760d82b0cc3dbc1ef2482a45a8d283e680687e60c57aa17dd9fa083184dc062",
                "sourceRevision": "e664c1abdf697c64703401dca1d7e1956f755f65",
            },
            "php": "docker.io/library/php:8.4-fpm-alpine3.22@sha256:e0f1ffe8bd43a137facdccdcac41cabdfc23e8d0fdd0665916a062f16e2f4058",
            "java": "docker.io/library/eclipse-temurin:25-jre-jammy@sha256:b8ba5fca9d88b6ecc3a46c8e75b744f84aca9a9d08587901b5ab480baf641ab5",
            "python": "docker.io/library/python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64",
            "k6": "docker.io/grafana/k6:1.3.0@sha256:3ddc8b1a33a2c3d8edc6e99b6a762ae36cba08788463458f5e6a7703e14eb77d",
            "mariadbApplication": {
                "indexRef": "docker.io/library/mariadb:11.8.8@sha256:efb4959ef2c835cd735dbc388eb9ad6aab0c78dd64febcd51bc17481111890c4",
                "amd64Ref": "docker.io/library/mariadb@sha256:09336a8c0cff9f363e133d8a245cca5ad3eeb326e0ffaedf74d49214c4571486",
            },
        },
        "wrapperOverhead": {
            "contract": "benchmarks/web-throughput/images/WRAPPER-NOTES.md",
            "contractSha256": file_sha256(
                "benchmarks/web-throughput/images/WRAPPER-NOTES.md"
            ),
            "commonBootstrapSha256": file_sha256(
                "benchmarks/web-throughput/images/common/bootstrap.sh"
            ),
            "commonSshdConfigSha256": file_sha256(
                "benchmarks/web-throughput/images/common/sshd_config"
            ),
        },
        "inputs": {
            "upstreamSqlPhpSha256": "e160a9ecbd996b5c701f172a7b22bf73eec96670cf6508034e18251067bebb6b",
            "benchmarkRunSha256": file_sha256("benchmarks/web-throughput/run.sh"),
            "benchmarkRunnerSha256": file_sha256(
                "benchmarks/web-throughput/run_benchmark.py"
            ),
            "k6ScriptSha256": file_sha256("benchmarks/web-throughput/throughput.js"),
        },
    }

    output = pathlib.Path(required("BUILD_LOCK_PATH"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

