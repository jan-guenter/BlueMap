#!/usr/bin/env python3
"""Fail-closed, offline analysis for the frozen BlueMap formal benchmark run."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import itertools
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit


FORMAT_VERSION = 1
EXPECTED_ENTRIES = 80
EXPECTED_BLOCKS = 5
FORMAL_CONTROL_DIR = Path(__file__).resolve().parent
FROZEN_CONTROL_DIR = FORMAL_CONTROL_DIR.parent / "frozen"
RUNNER_INPUT_FILES = {
    "manifest.json",
    "bluemap.js",
    "check_http_contract.py",
    "run_origin_case.sh",
    "sanitize_kubernetes_resource.py",
    "sanitize_configmap.py",
    "configmap_references.py",
    "capture_prometheus.py",
    "check_arrival_gate.py",
    "check_load_generator_capacity.py",
    "slow_reader.py",
    "generate_schedule.py",
    "runtime_identity.py",
    "runpod_loadgen.sh",
    "runpod-load-generator-identity.json",
    "workload.json",
    "matrix.json",
    "schedule.json",
    "schedule-entry.json",
}
SOURCE_HASH_FILES = {
    "manifestSha256": "manifest.json",
    "k6ScriptSha256": "bluemap.js",
    "contractScriptSha256": "check_http_contract.py",
    "runnerSha256": "run_origin_case.sh",
    "configSanitizerSha256": "sanitize_configmap.py",
    "configMapReferencesSha256": "configmap_references.py",
    "arrivalGateSha256": "check_arrival_gate.py",
    "loadGeneratorCapacitySha256": "check_load_generator_capacity.py",
    "slowReaderSha256": "slow_reader.py",
    "runpodLoadgenHelperSha256": "runpod_loadgen.sh",
    "runtimeIdentitySha256": "runtime_identity.py",
}
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CONTAINER_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
DURATION = re.compile(r"^([1-9][0-9]*)(ms|s|m|h)$")
METRICS_WINDOW = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(ms|s|m)$")
RUNPOD_ID = re.compile(r"^[A-Za-z0-9_-]{3,191}$")
RUNPOD_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RUNPOD_IMAGE = re.compile(
    r"^ghcr\.io/jan-guenter/bluemap-perf-loadgen@"
    r"(?P<digest>sha256:[a-f0-9]{64})$"
)
LOAD_GENERATOR_CONTROL_KEYS = {
    "backend",
    "image",
    "imageDigest",
    "sourceRevision",
}
SSH_HOST_KEY = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/=]+$")
SSH_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]+$")
TRAFFIC_MODES = {"cloudflare-https", "ssh-l4-traefik"}
TRAFFIC_BASE_URLS = {
    "cloudflare-https": "https://bluemap-test.guenter.cloud",
    "ssh-l4-traefik": "http://bluemap-test.guenter.cloud",
}
SSH_L4_TRAEFIK_TUNNEL = {
    "listenHost": "127.0.0.1",
    "listenPort": 18080,
    "targetHost": "rke2-traefik.kube-system.svc.cluster.local",
    "targetPort": 80,
}
PROFILES = {
    "static",
    "hot-tile",
    "random-tiles",
    "large-tile",
    "settings",
    "textures",
    "large-object",
    "missing-tile",
    "conditional",
    "live-viewers",
    "map-data-mixed",
    "browser-mixed",
}

AGGREGATE_METRICS = {
    "offeredThroughput": ("metrics", "throughput", "offeredIterationsPerSecond"),
    "achievedThroughput": (
        "metrics",
        "throughput",
        "achievedIterationsPerSecond",
    ),
    "achievedRateRatio": ("metrics", "throughput", "achievedRateRatio"),
    "failureRate": ("metrics", "requests", "failureRate"),
    "droppedRate": ("metrics", "throughput", "droppedRate"),
    "latencyP50Milliseconds": ("metrics", "latencyMilliseconds", "p50"),
    "latencyP90Milliseconds": ("metrics", "latencyMilliseconds", "p90"),
    "latencyP95Milliseconds": ("metrics", "latencyMilliseconds", "p95"),
    "latencyP99Milliseconds": ("metrics", "latencyMilliseconds", "p99"),
    "ttfbP50Milliseconds": ("metrics", "ttfbMilliseconds", "p50"),
    "ttfbP90Milliseconds": ("metrics", "ttfbMilliseconds", "p90"),
    "ttfbP95Milliseconds": ("metrics", "ttfbMilliseconds", "p95"),
    "ttfbP99Milliseconds": ("metrics", "ttfbMilliseconds", "p99"),
    "receivedBytes": ("metrics", "bytes", "received"),
    "receivedBytesPerIteration": (
        "metrics",
        "bytes",
        "receivedPerCompletedIteration",
    ),
    "webCpuP95Cores": (
        "metrics",
        "webResources",
        "preferred",
        "cpuCores",
        "p95",
    ),
    "webMemoryP95Bytes": (
        "metrics",
        "webResources",
        "preferred",
        "memoryBytes",
        "p95",
    ),
    "webThrottleP95Ratio": (
        "metrics",
        "webResources",
        "prometheus",
        "throttledPeriodRatio",
        "p95",
    ),
}

METRIC_ELIGIBILITY = {
    **{
        name: "http"
        for name in AGGREGATE_METRICS
        if name
        not in {
            "webCpuP95Cores",
            "webMemoryP95Bytes",
            "webThrottleP95Ratio",
        }
    },
    "webCpuP95Cores": "webResource",
    "webMemoryP95Bytes": "webResource",
    "webThrottleP95Ratio": "webPrometheus",
}


class AnalysisFailure(RuntimeError):
    """Structural failure that makes the formal run unsafe to analyze."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and analyze one completed frozen 80-entry formal run. "
            "This command is filesystem-only and never contacts Kubernetes."
        )
    )
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--schedule", required=True, type=Path)
    parser.add_argument("--runtime-admission-identities", type=Path)
    parser.add_argument("--bundle-manifest", type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_load_generator_control(
    value: Any,
    benchmark_revision: str,
    label: str = "frozen bundle loadGenerator",
) -> dict[str, str]:
    if (
        GIT_REVISION.fullmatch(benchmark_revision) is None
        or set(benchmark_revision) == {"0"}
    ):
        raise AnalysisFailure("benchmark revision for loadGenerator is invalid")
    if not isinstance(value, dict) or set(value) != LOAD_GENERATOR_CONTROL_KEYS:
        raise AnalysisFailure(f"{label} must contain exactly four fields")
    image = value.get("image")
    match = RUNPOD_IMAGE.fullmatch(image if isinstance(image, str) else "")
    digest = value.get("imageDigest")
    if (
        value.get("backend") != "runpod-ssh"
        or match is None
        or digest != match.group("digest")
        or set(str(digest).removeprefix("sha256:")) == {"0"}
        or value.get("sourceRevision") != benchmark_revision
    ):
        raise AnalysisFailure(
            f"{label} backend, immutable image, digest, or source revision differs"
        )
    return {
        "backend": value["backend"],
        "image": image,
        "imageDigest": digest,
        "sourceRevision": value["sourceRevision"],
    }


def validate_load_generator_execution_binding(
    control: dict[str, str],
    identity: dict[str, Any],
) -> str:
    runpod = identity.get("runpod")
    if (
        identity.get("backend") != control["backend"]
        or identity.get("sourceRevision") != control["sourceRevision"]
        or not isinstance(runpod, dict)
        or runpod.get("image") != control["image"]
        or runpod.get("imageDigest") != control["imageDigest"]
    ):
        raise AnalysisFailure(
            "frozen bundle loadGenerator differs from execution identity"
        )
    return canonical_sha256(control)


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AnalysisFailure(f"cannot load JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise AnalysisFailure(f"{path} must contain a JSON object")
    return value


def load_optional_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return load_object(path)


def load_regular_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise AnalysisFailure(f"required JSON evidence is missing or a symlink: {path}")
    return load_object(path)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisFailure(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise AnalysisFailure(f"{label} is not a finite value >= {minimum}")
    return result


def duration_seconds(value: Any, label: str) -> float:
    if not isinstance(value, str):
        raise AnalysisFailure(f"{label} must be a duration string")
    match = DURATION.fullmatch(value)
    if match is None:
        raise AnalysisFailure(f"{label} is not a supported duration")
    factor = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[match.group(2)]
    return int(match.group(1)) * factor


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AnalysisFailure(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AnalysisFailure(f"{label} is not a valid ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise AnalysisFailure(f"{label} must include a timezone")
    return parsed


def timestamp_epoch(value: Any, label: str) -> float:
    result = parse_timestamp(value, label).timestamp()
    if not math.isfinite(result):
        raise AnalysisFailure(f"{label} is not a finite timestamp")
    return result


def normalized_http_url(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AnalysisFailure(f"{label} must be an HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise AnalysisFailure(f"{label} is not a valid URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AnalysisFailure(f"{label} is not a credential-free HTTP(S) URL")
    hostname = parsed.hostname
    assert hostname is not None
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc.lower(), path, "", ""))


def validate_traffic_identity(
    value: Any,
    label: str,
    *,
    formal_run_id: str | None = None,
) -> dict[str, Any]:
    expected_keys = {
        "mode",
        "baseUrl",
        "service",
        "port",
        "requiresEdgeBypass",
        "tunnel",
    }
    if formal_run_id is not None:
        expected_keys.add("formalRunId")
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise AnalysisFailure(f"{label} is malformed")
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in TRAFFIC_MODES:
        raise AnalysisFailure(f"{label}.mode is invalid")
    expected_base_url = TRAFFIC_BASE_URLS[mode]
    if (
        value.get("baseUrl") != expected_base_url
        or normalized_http_url(value.get("baseUrl"), f"{label}.baseUrl")
        != expected_base_url
        or value.get("service") != "bluemap-perf-public"
        or value.get("port") != 8100
    ):
        raise AnalysisFailure(f"{label} route is invalid")
    if formal_run_id is not None and value.get("formalRunId") != formal_run_id:
        raise AnalysisFailure(f"{label}.formalRunId differs from execution identity")
    if mode == "cloudflare-https":
        if (
            value.get("requiresEdgeBypass") is not True
            or value.get("tunnel") is not None
        ):
            raise AnalysisFailure(
                f"{label} Cloudflare HTTPS controls are invalid"
            )
    elif (
        value.get("requiresEdgeBypass") is not False
        or value.get("tunnel") != SSH_L4_TRAEFIK_TUNNEL
    ):
        raise AnalysisFailure(f"{label} SSH L4 Traefik controls are invalid")
    return value


def seeded_shuffle(values: list[str], seed: str) -> list[str]:
    return sorted(
        values,
        key=lambda value: (
            hashlib.sha256(f"{seed}\0{value}".encode()).digest(),
            value,
        ),
    )


def validate_digest(value: Any, label: str, *, prefix: bool = False) -> None:
    raw = value.removeprefix("sha256:") if isinstance(value, str) else None
    if (
        not isinstance(value, str)
        or (prefix and not value.startswith("sha256:"))
        or (not prefix and value.startswith("sha256:"))
        or raw is None
        or HEX_64.fullmatch(raw) is None
        or set(raw) == {"0"}
    ):
        expected = "sha256: plus 64" if prefix else "64"
        raise AnalysisFailure(
            f"{label} must contain {expected} nonzero lowercase hex characters"
        )


def validate_matrix_constraints(
    matrix: dict[str, Any], *, expected_repetitions: int = EXPECTED_BLOCKS
) -> None:
    if matrix.get("formatVersion") != 3:
        raise AnalysisFailure("matrix formatVersion must be 3")
    if matrix.get("repetitions") != expected_repetitions:
        raise AnalysisFailure(
            f"matrix must have exactly {expected_repetitions} repetitions"
        )
    revision = matrix.get("benchmarkGitRevision")
    if (
        not isinstance(revision, str)
        or GIT_REVISION.fullmatch(revision) is None
        or set(revision) == {"0"}
    ):
        raise AnalysisFailure("matrix benchmarkGitRevision is not an exact commit")
    for key in ("scheduleSeed", "traceSeed"):
        if not isinstance(matrix.get(key), str) or not matrix[key]:
            raise AnalysisFailure(f"matrix {key} must be a non-empty string")
    validate_digest(matrix.get("manifestSha256"), "matrix manifestSha256")
    map_ids = matrix.get("mapIds")
    if (
        not isinstance(map_ids, list)
        or not map_ids
        or map_ids != sorted(set(map_ids))
        or not all(isinstance(map_id, str) and map_id for map_id in map_ids)
    ):
        raise AnalysisFailure("matrix mapIds must be sorted, unique strings")
    controls = matrix.get("controls")
    if not isinstance(controls, dict):
        raise AnalysisFailure("matrix controls must be an object")
    for key in ("warmupDuration", "measurementDuration"):
        duration_seconds(controls.get(key), f"matrix controls.{key}")
    for key in ("cooldownSeconds", "preAllocatedVUs", "maxVUs"):
        value = controls.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise AnalysisFailure(f"matrix controls.{key} must be positive")
    ratio = controls.get("minimumAchievedRateRatio")
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not 0 < float(ratio) <= 1
    ):
        raise AnalysisFailure(
            "matrix controls.minimumAchievedRateRatio must be in (0, 1]"
        )

    variants = matrix.get("variants")
    cases = matrix.get("cases")
    if not isinstance(variants, list) or len(variants) < 2:
        raise AnalysisFailure("matrix must define at least two variants")
    if not isinstance(cases, list) or not cases:
        raise AnalysisFailure("matrix must define at least one case")
    variant_ids: list[str] = []
    for variant in variants:
        if not isinstance(variant, dict) or ID.fullmatch(variant.get("id", "")) is None:
            raise AnalysisFailure("matrix variant has an invalid ID")
        variant_ids.append(variant["id"])
        if variant.get("contractMode") not in {"enhanced", "legacy"}:
            raise AnalysisFailure(f"variant {variant['id']} contractMode is invalid")
        if variant.get("implementation") not in {"php", "java", "rust"}:
            raise AnalysisFailure(f"variant {variant['id']} implementation is invalid")
        storage = variant.get("storageType")
        backend = variant.get("databaseBackend")
        if storage not in {"sql", "file"}:
            raise AnalysisFailure(f"variant {variant['id']} storageType is invalid")
        if backend not in {"postgresql", "mariadb", "none"}:
            raise AnalysisFailure(f"variant {variant['id']} databaseBackend is invalid")
        if (storage == "file") != (backend == "none"):
            raise AnalysisFailure(
                f"variant {variant['id']} storage/backend combination is invalid"
            )
        replicas = variant.get("replicaCount")
        if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 1:
            raise AnalysisFailure(f"variant {variant['id']} replicaCount is invalid")
        images = variant.get("expectedImages")
        if not isinstance(images, list) or not images:
            raise AnalysisFailure(f"variant {variant['id']} expectedImages is empty")
        normalized_images = []
        seen_images: set[tuple[str, str]] = set()
        for image in images:
            if not isinstance(image, dict) or set(image) != {"kind", "name", "digest"}:
                raise AnalysisFailure(
                    f"variant {variant['id']} has malformed expectedImages"
                )
            kind = image["kind"]
            name = image["name"]
            if kind not in {"container", "initContainer"}:
                raise AnalysisFailure(f"variant {variant['id']} image kind is invalid")
            if (
                not isinstance(name, str)
                or len(name) > 63
                or CONTAINER_NAME.fullmatch(name) is None
            ):
                raise AnalysisFailure(f"variant {variant['id']} image name is invalid")
            validate_digest(
                image["digest"],
                f"variant {variant['id']} image digest",
                prefix=True,
            )
            identity = (kind, name)
            if identity in seen_images:
                raise AnalysisFailure(f"variant {variant['id']} has duplicate images")
            seen_images.add(identity)
            normalized_images.append(image)
        if normalized_images != sorted(
            normalized_images, key=lambda image: (image["kind"], image["name"])
        ):
            raise AnalysisFailure(
                f"variant {variant['id']} expectedImages are not sorted"
            )
        validate_digest(
            variant.get("expectedSanitizedConfigSha256"),
            f"variant {variant['id']} expectedSanitizedConfigSha256",
        )
        validate_digest(
            variant.get("expectedSanitizedRuntimeSpecSha256"),
            f"variant {variant['id']} expectedSanitizedRuntimeSpecSha256",
        )
    if len(set(variant_ids)) != len(variant_ids):
        raise AnalysisFailure("matrix variant IDs are not unique")

    case_ids: list[str] = []
    for case in cases:
        if not isinstance(case, dict) or ID.fullmatch(case.get("id", "")) is None:
            raise AnalysisFailure("matrix case has an invalid ID")
        case_ids.append(case["id"])
        if case.get("profile") not in PROFILES:
            raise AnalysisFailure(f"case {case['id']} profile is invalid")
        for key in ("rate", "viewers", "markerIntervalSeconds"):
            value = case.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise AnalysisFailure(f"case {case['id']} {key} must be positive")
        for key in ("latencyP95Milliseconds", "latencyP99Milliseconds"):
            value = case.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise AnalysisFailure(f"case {case['id']} {key} must be positive")
        if case["latencyP99Milliseconds"] < case["latencyP95Milliseconds"]:
            raise AnalysisFailure(f"case {case['id']} p99 gate is below p95")
        for key in ("acceptEncoding", "storedEncoding"):
            if case.get(key) not in {"gzip", "zstd", "deflate", "identity"}:
                raise AnalysisFailure(f"case {case['id']} {key} is invalid")
        selected = case.get("variants")
        if (
            not isinstance(selected, list)
            or len(selected) < 2
            or len(set(selected)) != len(selected)
            or not set(selected) <= set(variant_ids)
        ):
            raise AnalysisFailure(f"case {case['id']} variants are invalid")
    if len(set(case_ids)) != len(case_ids):
        raise AnalysisFailure("matrix case IDs are not unique")


def build_expected_schedule(
    matrix: dict[str, Any],
    matrix_digest: str,
    schedule_seed: str,
    *,
    expected_repetitions: int = EXPECTED_BLOCKS,
) -> dict[str, Any]:
    validate_matrix_constraints(
        matrix, expected_repetitions=expected_repetitions
    )
    variants_raw = matrix.get("variants")
    cases_raw = matrix.get("cases")
    controls = matrix.get("controls")
    if (
        not isinstance(variants_raw, list)
        or not isinstance(cases_raw, list)
        or not isinstance(controls, dict)
    ):
        raise AnalysisFailure(
            "matrix must be the format-v3 deterministic benchmark design"
        )
    if (
        not isinstance(expected_repetitions, int)
        or isinstance(expected_repetitions, bool)
        or expected_repetitions < 1
        or matrix.get("repetitions") != expected_repetitions
    ):
        raise AnalysisFailure("matrix repetition count differs from expectation")
    variants: dict[str, dict[str, Any]] = {}
    for variant in variants_raw:
        if not isinstance(variant, dict) or not isinstance(variant.get("id"), str):
            raise AnalysisFailure("matrix contains an invalid variant")
        if variant["id"] in variants:
            raise AnalysisFailure(f"duplicate matrix variant {variant['id']!r}")
        variants[variant["id"]] = variant
    cases: dict[str, dict[str, Any]] = {}
    for case in cases_raw:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise AnalysisFailure("matrix contains an invalid case")
        if case["id"] in cases:
            raise AnalysisFailure(f"duplicate matrix case {case['id']!r}")
        selected = case.get("variants")
        if (
            not isinstance(selected, list)
            or len(selected) < 2
            or len(set(selected)) != len(selected)
            or not set(selected) <= set(variants)
        ):
            raise AnalysisFailure(f"matrix case {case['id']} has invalid variants")
        cases[case["id"]] = case

    entries: list[dict[str, Any]] = []
    sequence = 0
    for block in range(1, expected_repetitions + 1):
        case_order = seeded_shuffle(
            list(cases), f"{schedule_seed}\0block\0{block}\0cases"
        )
        for case_id in case_order:
            case = cases[case_id]
            base_order = seeded_shuffle(
                case["variants"],
                f"{schedule_seed}\0case\0{case_id}\0base-variants",
            )
            rotation = (block - 1) % len(base_order)
            order = base_order[rotation:] + base_order[:rotation]
            for ordinal, variant_id in enumerate(order, start=1):
                sequence += 1
                variant = variants[variant_id]
                entries.append(
                    {
                        "entryId": f"{case_id}/{variant_id}/block-{block}",
                        "sequence": sequence,
                        "block": block,
                        "ordinalWithinCase": ordinal,
                        "matrixCaseId": case_id,
                        "variantId": variant_id,
                        "runnerCaseId": f"{case_id}-{variant_id}-b{block}",
                        "profile": case["profile"],
                        "rate": case["rate"],
                        "viewers": case["viewers"],
                        "markerIntervalSeconds": case["markerIntervalSeconds"],
                        "contractMode": variant["contractMode"],
                        "implementation": variant["implementation"],
                        "storageType": variant["storageType"],
                        "databaseBackend": variant["databaseBackend"],
                        "replicaCount": variant["replicaCount"],
                        "benchmarkGitRevision": matrix["benchmarkGitRevision"],
                        "expectedImages": variant["expectedImages"],
                        "expectedSanitizedConfigSha256": variant[
                            "expectedSanitizedConfigSha256"
                        ],
                        "expectedSanitizedRuntimeSpecSha256": variant[
                            "expectedSanitizedRuntimeSpecSha256"
                        ],
                        "acceptEncoding": case["acceptEncoding"],
                        "storedEncoding": case["storedEncoding"],
                        "traceSeed": matrix["traceSeed"],
                        "manifestSha256": matrix["manifestSha256"],
                        "mapIds": matrix["mapIds"],
                        "warmupDuration": controls["warmupDuration"],
                        "measurementDuration": controls["measurementDuration"],
                        "cooldownSeconds": controls["cooldownSeconds"],
                        "minimumAchievedRateRatio": controls[
                            "minimumAchievedRateRatio"
                        ],
                        "preAllocatedVUs": controls["preAllocatedVUs"],
                        "maxVUs": controls["maxVUs"],
                        "latencyP95Milliseconds": case[
                            "latencyP95Milliseconds"
                        ],
                        "latencyP99Milliseconds": case[
                            "latencyP99Milliseconds"
                        ],
                    }
                )
    return {
        "formatVersion": 3,
        "matrixSha256": matrix_digest,
        "scheduleSeed": schedule_seed,
        "traceSeed": matrix["traceSeed"],
        "benchmarkGitRevision": matrix["benchmarkGitRevision"],
        "repetitions": expected_repetitions,
        "entries": entries,
    }


def validate_documents(
    matrix_path: Path, schedule_path: Path
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    if not matrix_path.is_file() or not schedule_path.is_file():
        raise AnalysisFailure("frozen matrix and schedule files must both exist")
    matrix = load_object(matrix_path)
    schedule = load_object(schedule_path)
    matrix_digest = sha256_file(matrix_path)
    schedule_digest = sha256_file(schedule_path)
    seed = schedule.get("scheduleSeed")
    if not isinstance(seed, str) or not seed:
        raise AnalysisFailure("schedule has no non-empty scheduleSeed")
    expected = build_expected_schedule(matrix, matrix_digest, seed)
    if schedule != expected:
        raise AnalysisFailure(
            "schedule does not exactly match the deterministic matrix expansion"
        )
    entries = schedule["entries"]
    if len(entries) != EXPECTED_ENTRIES:
        raise AnalysisFailure(
            f"formal schedule must contain exactly {EXPECTED_ENTRIES} entries, "
            f"found {len(entries)}"
        )
    if [entry["sequence"] for entry in entries] != list(
        range(1, EXPECTED_ENTRIES + 1)
    ):
        raise AnalysisFailure("schedule sequence must be exactly 1..80")
    for key in ("entryId", "runnerCaseId"):
        values = [entry[key] for entry in entries]
        if len(set(values)) != EXPECTED_ENTRIES:
            raise AnalysisFailure(f"schedule {key} values are not unique")
    counts: defaultdict[tuple[Any, Any, Any], int] = defaultdict(int)
    for entry in entries:
        counts[(entry["block"], entry["matrixCaseId"], entry["variantId"])] += 1
    if len(counts) != EXPECTED_ENTRIES or any(value != 1 for value in counts.values()):
        raise AnalysisFailure(
            "schedule does not contain every case/variant/block exactly once"
        )
    return matrix, schedule, matrix_digest, schedule_digest


def validate_frozen_bundle(
    matrix: dict[str, Any],
    matrix_path: Path,
    schedule_path: Path,
    matrix_digest: str,
    schedule_digest: str,
    admission_path: Path,
    bundle_manifest_path: Path,
    analyzer_digest: str,
) -> tuple[dict[str, str], dict[str, Any], str, str]:
    expected_names = {
        matrix_path: "matrix.json",
        schedule_path: "schedule.json",
        admission_path: "runtime-admission-identities.json",
        bundle_manifest_path: "bundle-manifest.json",
    }
    if len({path.resolve().parent for path in expected_names}) != 1:
        raise AnalysisFailure(
            "matrix, schedule, admission identities, and bundle manifest "
            "must come from one exact frozen directory"
        )
    for path, name in expected_names.items():
        if path.name != name or not path.is_file():
            raise AnalysisFailure(
                f"frozen bundle input must be an existing {name}: {path}"
            )

    admission_digest = sha256_file(admission_path)
    bundle_digest = sha256_file(bundle_manifest_path)
    admission = load_object(admission_path)
    bundle = load_object(bundle_manifest_path)
    if (
        admission.get("formatVersion") != 1
        or admission.get("benchmarkGitRevision") != matrix["benchmarkGitRevision"]
        or admission.get("podSpecIdentityVersion") != 1
    ):
        raise AnalysisFailure(
            "frozen admission identities use the wrong format or revision"
        )
    raw_variants = admission.get("variants")
    matrix_variants = matrix["variants"]
    if not isinstance(raw_variants, list) or len(raw_variants) != len(matrix_variants):
        raise AnalysisFailure(
            "frozen admission identities do not cover every matrix variant"
        )
    expected_admission: dict[str, str] = {}
    expected_order = [variant["id"] for variant in matrix_variants]
    replica_counts = {
        variant["id"]: variant["replicaCount"] for variant in matrix_variants
    }
    for item in raw_variants:
        if not isinstance(item, dict):
            raise AnalysisFailure("frozen admission identity contains a non-object")
        variant_id = item.get("variantId")
        if (
            variant_id not in replica_counts
            or variant_id in expected_admission
            or item.get("replicaCount") != replica_counts.get(variant_id)
        ):
            raise AnalysisFailure(
                f"invalid frozen admission identity variant: {variant_id!r}"
            )
        digest = item.get("expectedAdmissionPodSpecSha256")
        validate_digest(digest, f"frozen admission identity {variant_id}")
        expected_admission[variant_id] = digest
    if list(expected_admission) != expected_order:
        raise AnalysisFailure(
            "frozen admission identity ordering differs from the matrix"
        )

    control_paths = {
        "orchestratorSha256": FORMAL_CONTROL_DIR / "orchestrate.py",
        "freezerSha256": FORMAL_CONTROL_DIR / "freeze.py",
        "controllerLockSha256": FROZEN_CONTROL_DIR / "controller-lock.json",
    }
    for path in control_paths.values():
        if not path.is_file():
            raise AnalysisFailure(f"reviewed formal control is missing: {path}")
    expected_bundle = {
        "formatVersion": 1,
        "benchmarkGitRevision": matrix["benchmarkGitRevision"],
        "matrixSha256": matrix_digest,
        "scheduleSha256": schedule_digest,
        "runtimeAdmissionIdentitiesSha256": admission_digest,
        "analyzerSha256": analyzer_digest,
        **{key: sha256_file(path) for key, path in control_paths.items()},
    }
    if set(bundle) != set(expected_bundle) | {"createdAt", "loadGenerator"}:
        raise AnalysisFailure(
            "frozen bundle manifest must contain exactly the reviewed bindings"
        )
    timestamp_epoch(bundle.get("createdAt"), "frozen bundle createdAt")
    for key, expected in expected_bundle.items():
        if bundle.get(key) != expected:
            raise AnalysisFailure(
                f"frozen bundle {key} does not match the exact formal inputs"
            )
    bundle["loadGenerator"] = validate_load_generator_control(
        bundle.get("loadGenerator"),
        matrix["benchmarkGitRevision"],
    )
    controller_lock = load_object(control_paths["controllerLockSha256"])
    if (
        controller_lock.get("formatVersion") != 1
        or controller_lock.get("requiredRevision") != matrix["benchmarkGitRevision"]
        or not isinstance(controller_lock.get("controllers"), list)
    ):
        raise AnalysisFailure("reviewed formal controller lock is malformed")
    expected_controllers = {
        "freeze.py": expected_bundle["freezerSha256"],
        "orchestrate.py": expected_bundle["orchestratorSha256"],
        "analyze.py": analyzer_digest,
    }
    if [
        item.get("path") for item in controller_lock["controllers"]
        if isinstance(item, dict)
    ] != list(expected_controllers):
        raise AnalysisFailure(
            "reviewed formal controller ordering differs from the expected controls"
        )
    actual_controllers: dict[str, str] = {}
    for item in controller_lock["controllers"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or item.get("path") in actual_controllers
        ):
            raise AnalysisFailure("reviewed formal controller lock is malformed")
        validate_digest(item.get("sha256"), "reviewed controller digest")
        actual_controllers[item["path"]] = item["sha256"]
    if actual_controllers != expected_controllers:
        raise AnalysisFailure(
            "reviewed formal controller lock does not bind the active controls"
        )
    return expected_admission, bundle, admission_digest, bundle_digest


def validate_runpod_identity(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisFailure(f"{label} must be an object")
    expected_keys = {
        "backend",
        "capturedAt",
        "formatVersion",
        "remoteRoot",
        "runId",
        "sourceRevision",
        "runpod",
        "ssh",
    }
    if set(value) != expected_keys or value.get("formatVersion") != 1:
        raise AnalysisFailure(f"{label} is malformed")
    if (
        value.get("backend") != "runpod-ssh"
        or value.get("remoteRoot") != "/artifacts"
        or RUNPOD_RUN_ID.fullmatch(value.get("runId", "")) is None
        or GIT_REVISION.fullmatch(value.get("sourceRevision", "")) is None
        or set(value["sourceRevision"]) == {"0"}
    ):
        raise AnalysisFailure(f"{label} has invalid fixed controls")
    timestamp_epoch(value.get("capturedAt"), f"{label}.capturedAt")

    runpod = value.get("runpod")
    runpod_keys = {
        "costPerHour",
        "cpuFlavorId",
        "dataCenterId",
        "image",
        "imageDigest",
        "machineId",
        "maxDownloadMbps",
        "maxUploadMbps",
        "minDownloadMbps",
        "minUploadMbps",
        "podId",
        "publicIp",
        "secureCloud",
        "vcpuCount",
    }
    if not isinstance(runpod, dict) or set(runpod) != runpod_keys:
        raise AnalysisFailure(f"{label}.runpod is malformed")
    if (
        runpod.get("cpuFlavorId") != "cpu5c"
        or runpod.get("dataCenterId")
        not in {"EU-CZ-1", "EU-FR-1", "EU-NL-1", "EU-RO-1", "EU-SE-1"}
        or runpod.get("secureCloud") is not True
        or runpod.get("vcpuCount") != 8
        or runpod.get("minDownloadMbps") != 500
        or runpod.get("minUploadMbps") != 100
        or RUNPOD_ID.fullmatch(runpod.get("podId", "")) is None
        or not isinstance(runpod.get("machineId"), str)
        or not runpod["machineId"]
        or RUNPOD_IMAGE.fullmatch(runpod.get("image", "")) is None
    ):
        raise AnalysisFailure(f"{label}.runpod has invalid frozen controls")
    validate_digest(runpod.get("imageDigest"), f"{label}.runpod.imageDigest", prefix=True)
    if not runpod["image"].endswith(f"@{runpod['imageDigest']}"):
        raise AnalysisFailure(f"{label}.runpod image and digest differ")
    for key, minimum in (("maxDownloadMbps", 500), ("maxUploadMbps", 100)):
        finite_number(runpod.get(key), f"{label}.runpod.{key}", minimum=minimum)
    cost = runpod.get("costPerHour")
    if cost is not None:
        finite_number(cost, f"{label}.runpod.costPerHour", minimum=0)
    try:
        public_ip = str(ipaddress.IPv4Address(runpod.get("publicIp")))
    except (ipaddress.AddressValueError, TypeError) as error:
        raise AnalysisFailure(f"{label}.runpod.publicIp is invalid") from error

    ssh = value.get("ssh")
    ssh_keys = {"host", "hostKey", "hostKeyFingerprint", "port", "user"}
    if not isinstance(ssh, dict) or set(ssh) != ssh_keys:
        raise AnalysisFailure(f"{label}.ssh is malformed")
    if (
        ssh.get("host") != public_ip
        or ssh.get("user") != "loadgen"
        or not isinstance(ssh.get("port"), int)
        or isinstance(ssh.get("port"), bool)
        or not 1 <= ssh["port"] <= 65535
        or SSH_HOST_KEY.fullmatch(ssh.get("hostKey", "")) is None
        or SSH_FINGERPRINT.fullmatch(ssh.get("hostKeyFingerprint", "")) is None
    ):
        raise AnalysisFailure(f"{label}.ssh has invalid frozen controls")
    return value


def validate_execution_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisFailure("run state executionIdentity must be an object")
    expected_keys = {
        "formatVersion",
        "namespace",
        "databasePod",
        "loadGeneratorBackend",
        "loadGeneratorIdentity",
        "loadGeneratorIdentitySha256",
        "formalRunId",
        "traffic",
        "runner",
        "runnerSha256",
        "benchmarkPython",
        "benchmarkPythonSha256",
        "kubeconfig",
        "kubeconfigSha256",
        "prometheus",
        "transitionTimeoutSeconds",
        "metricsTimeoutSeconds",
        "pollIntervalSeconds",
    }
    if set(value) != expected_keys or value.get("formatVersion") != 1:
        raise AnalysisFailure("run state executionIdentity is malformed")
    for key in ("namespace", "databasePod"):
        name = value.get(key)
        if (
            not isinstance(name, str)
            or len(name) > 63
            or CONTAINER_NAME.fullmatch(name) is None
        ):
            raise AnalysisFailure(f"run state executionIdentity.{key} is invalid")
    if value.get("loadGeneratorBackend") != "runpod-ssh":
        raise AnalysisFailure(
            "run state executionIdentity must use the RunPod SSH backend"
        )
    generator_identity = validate_runpod_identity(
        value.get("loadGeneratorIdentity"),
        "run state executionIdentity.loadGeneratorIdentity",
    )
    validate_digest(
        value.get("loadGeneratorIdentitySha256"),
        "run state executionIdentity.loadGeneratorIdentitySha256",
    )
    if value["loadGeneratorIdentitySha256"] != canonical_sha256(generator_identity):
        raise AnalysisFailure(
            "run state executionIdentity load-generator identity digest differs"
        )
    formal_run_id = value.get("formalRunId")
    if (
        not isinstance(formal_run_id, str)
        or RUNPOD_RUN_ID.fullmatch(formal_run_id) is None
        or formal_run_id != generator_identity["runId"]
    ):
        raise AnalysisFailure(
            "run state executionIdentity.formalRunId is invalid or differs "
            "from the frozen generator"
        )
    validate_traffic_identity(
        value.get("traffic"),
        "run state executionIdentity.traffic",
    )
    for key in ("runner", "benchmarkPython", "kubeconfig"):
        path = value.get(key)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise AnalysisFailure(
                f"run state executionIdentity.{key} must be an absolute path"
            )
    for key in ("runnerSha256", "benchmarkPythonSha256", "kubeconfigSha256"):
        validate_digest(value.get(key), f"run state executionIdentity.{key}")
    prometheus = value.get("prometheus")
    if not isinstance(prometheus, dict) or set(prometheus) != {"enabled", "url"}:
        raise AnalysisFailure("run state executionIdentity.prometheus is malformed")
    if not isinstance(prometheus.get("enabled"), bool):
        raise AnalysisFailure(
            "run state executionIdentity.prometheus.enabled is invalid"
        )
    if prometheus["enabled"]:
        normalized_http_url(
            prometheus.get("url"),
            "run state executionIdentity.prometheus.url",
        )
    elif prometheus.get("url") is not None:
        raise AnalysisFailure(
            "disabled run state Prometheus identity must have a null URL"
        )
    for key in ("transitionTimeoutSeconds", "metricsTimeoutSeconds"):
        raw = value.get(key)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 30:
            raise AnalysisFailure(f"run state executionIdentity.{key} is invalid")
    finite_number(
        value.get("pollIntervalSeconds"),
        "run state executionIdentity.pollIntervalSeconds",
        minimum=0.1,
    )
    return value


def validate_state(
    run_root: Path,
    schedule: dict[str, Any],
    matrix_digest: str,
    schedule_digest: str,
    matrix: dict[str, Any],
    *,
    admission_digest: str,
    bundle_digest: str,
    bundle: dict[str, Any],
    analyzer_digest: str,
    expected_admission: dict[str, str],
) -> dict[str, Any]:
    state = load_object(run_root / "state.json")
    expected_header = {
        "formatVersion": 1,
        "status": "completed",
        "nextSequence": EXPECTED_ENTRIES + 1,
        "matrixSha256": matrix_digest,
        "scheduleSha256": schedule_digest,
        "manifestSha256": matrix["manifestSha256"],
        "runtimeAdmissionIdentitiesSha256": admission_digest,
        "bundleManifestSha256": bundle_digest,
        "orchestratorSha256": bundle["orchestratorSha256"],
        "analyzerSha256": analyzer_digest,
        "benchmarkGitRevision": matrix["benchmarkGitRevision"],
        "loadGeneratorSha256": canonical_sha256(bundle["loadGenerator"]),
    }
    for key, expected in expected_header.items():
        if state.get(key) != expected:
            raise AnalysisFailure(
                f"run state {key} mismatch: expected {expected!r}, "
                f"found {state.get(key)!r}"
            )
    timestamp_epoch(state.get("createdAt"), "run state createdAt")
    timestamp_epoch(state.get("updatedAt"), "run state updatedAt")
    timestamp_epoch(state.get("completedAt"), "run state completedAt")
    execution_identity = validate_execution_identity(state.get("executionIdentity"))
    validate_load_generator_execution_binding(
        bundle["loadGenerator"],
        execution_identity["loadGeneratorIdentity"],
    )
    state_entries = state.get("entries")
    if not isinstance(state_entries, dict):
        raise AnalysisFailure("run state entries must be an object")
    expected_keys = {str(index) for index in range(1, EXPECTED_ENTRIES + 1)}
    if set(state_entries) != expected_keys:
        raise AnalysisFailure(
            "run state must contain exactly the 80 scheduled sequence keys"
        )
    for entry in schedule["entries"]:
        item = state_entries[str(entry["sequence"])]
        if not isinstance(item, dict):
            raise AnalysisFailure(
                f"run state sequence {entry['sequence']} is not an object"
            )
        expected = {
            "status": "completed",
            "entryId": entry["entryId"],
            "runnerCaseId": entry["runnerCaseId"],
            "variantId": entry["variantId"],
        }
        for key, value in expected.items():
            if item.get(key) != value:
                raise AnalysisFailure(
                    f"run state sequence {entry['sequence']} {key} mismatch"
                )
        timestamp_epoch(
            item.get("startedAt"),
            f"run state sequence {entry['sequence']} startedAt",
        )
        timestamp_epoch(
            item.get("runnerStartedAt"),
            f"run state sequence {entry['sequence']} runnerStartedAt",
        )
        timestamp_epoch(
            item.get("completedAt"),
            f"run state sequence {entry['sequence']} completedAt",
        )
        web_pods = item.get("webPods")
        if (
            not isinstance(web_pods, list)
            or len(web_pods) != entry["replicaCount"]
            or len(set(web_pods)) != len(web_pods)
            or not all(isinstance(pod, str) and pod for pod in web_pods)
        ):
            raise AnalysisFailure(
                f"run state sequence {entry['sequence']} has invalid webPods"
            )
        admission = item.get("admissionPodSpecIdentity")
        expected_digest = expected_admission[entry["variantId"]]
        if (
            not isinstance(admission, dict)
            or admission.get("expected") != expected_digest
            or not isinstance(admission.get("actual"), dict)
            or set(admission["actual"]) != set(web_pods)
            or set(admission["actual"].values()) != {expected_digest}
        ):
            raise AnalysisFailure(
                f"run state sequence {entry['sequence']} admission identity mismatch"
            )
        result = item.get("result")
        exit_status = item.get("runnerExitStatus")
        if result not in {"passed", "failed"}:
            raise AnalysisFailure(
                f"run state sequence {entry['sequence']} has invalid result"
            )
        valid_exit = (
            isinstance(exit_status, int)
            and not isinstance(exit_status, bool)
            and (
                result == "passed"
                and exit_status == 0
                or result == "failed"
                and exit_status != 0
            )
        )
        if not valid_exit:
            raise AnalysisFailure(
                f"run state sequence {entry['sequence']} exit/result disagree"
            )
    return state


def preflight_evidence_inventory(preflight_root: Path) -> dict[str, Any]:
    excluded = {"preflight-evidence.json", "preflight-report.json", "SHA256SUMS"}
    files = []
    for path in sorted(preflight_root.rglob("*")):
        relative = path.relative_to(preflight_root)
        if path.is_symlink():
            raise AnalysisFailure(f"preflight evidence symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise AnalysisFailure(f"preflight evidence is not regular: {relative}")
        if len(relative.parts) == 1 and relative.name in excluded:
            continue
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    return {"formatVersion": 1, "files": files}


def load_strict_ndjson(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise AnalysisFailure(f"{label} is missing or a symlink")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AnalysisFailure(f"cannot read {label}: {error}") from error
    values: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line:
            raise AnalysisFailure(f"{label}:{number} is blank")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise AnalysisFailure(f"{label}:{number} is invalid JSON") from error
        if not isinstance(value, dict):
            raise AnalysisFailure(f"{label}:{number} is not an object")
        values.append(value)
    return values


def validate_preflight_raw_execution(
    preflight_root: Path,
    schedule: dict[str, Any],
    matrix: dict[str, Any],
    matrix_path: Path,
    schedule_path: Path,
    execution_identity: dict[str, Any],
    *,
    admission_digest: str,
    bundle_digest: str,
    orchestrator_digest: str,
    analyzer_digest: str,
    load_generator_sha256: str,
    expected_admission: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state_path = preflight_root / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise AnalysisFailure("preflight state is missing or a symlink")
    state = load_object(state_path)
    expected_header = {
        "formatVersion": 1,
        "status": "completed",
        "nextSequence": 7,
        "matrixSha256": sha256_file(matrix_path),
        "scheduleSha256": sha256_file(schedule_path),
        "manifestSha256": matrix["manifestSha256"],
        "runtimeAdmissionIdentitiesSha256": admission_digest,
        "bundleManifestSha256": bundle_digest,
        "orchestratorSha256": orchestrator_digest,
        "analyzerSha256": analyzer_digest,
        "benchmarkGitRevision": matrix["benchmarkGitRevision"],
        "loadGeneratorSha256": load_generator_sha256,
    }
    for key, expected in expected_header.items():
        if state.get(key) != expected:
            raise AnalysisFailure(f"preflight state {key} differs")
    if state.get("cleanupError") is not None:
        raise AnalysisFailure("preflight candidate cleanup failed")
    if validate_execution_identity(state.get("executionIdentity")) != execution_identity:
        raise AnalysisFailure("preflight execution identity differs from formal run")
    created = timestamp_epoch(state.get("createdAt"), "preflight state createdAt")
    updated = timestamp_epoch(state.get("updatedAt"), "preflight state updatedAt")
    completed = timestamp_epoch(
        state.get("completedAt"), "preflight state completedAt"
    )
    if not created <= completed <= updated:
        raise AnalysisFailure("preflight state timestamps are out of order")
    state_entries = state.get("entries")
    if not isinstance(state_entries, dict) or set(state_entries) != {
        str(value) for value in range(1, 7)
    }:
        raise AnalysisFailure("preflight state must contain exactly sequence 1..6")
    previous_completed = created
    rows: list[dict[str, Any]] = []
    results_root = preflight_root / "results"
    logs_root = preflight_root / "logs"
    if not results_root.is_dir() or results_root.is_symlink():
        raise AnalysisFailure("preflight results directory is missing or a symlink")
    if not logs_root.is_dir() or logs_root.is_symlink():
        raise AnalysisFailure("preflight logs directory is missing or a symlink")
    expected_case_names = {entry["runnerCaseId"] for entry in schedule["entries"]}
    actual_case_names = {
        path.name
        for path in results_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    }
    if actual_case_names != expected_case_names or any(
        not path.is_dir() or path.is_symlink() for path in results_root.iterdir()
    ):
        raise AnalysisFailure("preflight result directories differ from schedule")
    expected_logs = {
        f"{entry['sequence']:03d}-{entry['runnerCaseId']}.log"
        for entry in schedule["entries"]
    }
    actual_logs = {path.name for path in logs_root.iterdir()}
    if actual_logs != expected_logs or any(
        not path.is_file() or path.is_symlink() for path in logs_root.iterdir()
    ):
        raise AnalysisFailure("preflight runner logs differ from schedule")
    for entry in schedule["entries"]:
        item = state_entries[str(entry["sequence"])]
        if not isinstance(item, dict):
            raise AnalysisFailure("preflight state entry is not an object")
        for key, expected in (
            ("status", "completed"),
            ("entryId", entry["entryId"]),
            ("runnerCaseId", entry["runnerCaseId"]),
            ("variantId", entry["variantId"]),
            ("result", "passed"),
            ("runnerExitStatus", 0),
        ):
            if item.get(key) != expected:
                raise AnalysisFailure(
                    f"preflight state sequence {entry['sequence']} {key} differs"
                )
        started = timestamp_epoch(
            item.get("startedAt"), f"preflight sequence {entry['sequence']} startedAt"
        )
        runner_started = timestamp_epoch(
            item.get("runnerStartedAt"),
            f"preflight sequence {entry['sequence']} runnerStartedAt",
        )
        entry_completed = timestamp_epoch(
            item.get("completedAt"),
            f"preflight sequence {entry['sequence']} completedAt",
        )
        if not previous_completed <= started <= runner_started <= entry_completed:
            raise AnalysisFailure("preflight state entry chronology differs")
        previous_completed = entry_completed
        web_pods = item.get("webPods")
        if (
            not isinstance(web_pods, list)
            or len(web_pods) != entry["replicaCount"]
            or len(set(web_pods)) != len(web_pods)
            or not all(isinstance(pod, str) and pod for pod in web_pods)
        ):
            raise AnalysisFailure("preflight state web Pod identity differs")
        expected_digest = expected_admission.get(entry["variantId"])
        admission = item.get("admissionPodSpecIdentity")
        if (
            not isinstance(expected_digest, str)
            or not isinstance(admission, dict)
            or admission.get("expected") != expected_digest
            or not isinstance(admission.get("actual"), dict)
            or set(admission["actual"]) != set(web_pods)
            or set(admission["actual"].values()) != {expected_digest}
        ):
            raise AnalysisFailure("preflight admission Pod identity differs")
        row = analyze_case(
            results_root / entry["runnerCaseId"],
            entry,
            item,
            execution_identity,
            matrix_path,
            schedule_path,
            sha256_file(matrix_path),
            sha256_file(schedule_path),
        )
        if (
            row.get("result") != "passed"
            or row.get("eligibleForFormalComparison") is not True
            or nested(row, ("metrics", "transportProof", "mode"))
            != "ssh-l4-traefik"
            or nested(row, ("metrics", "transportProof", "passed")) is not True
        ):
            raise AnalysisFailure("preflight case semantic replay failed")
        rows.append(row)
    if previous_completed > completed:
        raise AnalysisFailure("preflight completed before its final entry")

    events = load_strict_ndjson(
        preflight_root / "events.ndjson", "preflight orchestrator events"
    )
    if len(events) != 24 or any(
        event.get("event") == "cleanup-failed" for event in events
    ):
        raise AnalysisFailure("preflight must preserve exactly 24 lifecycle events")
    validate_run_chronology(
        preflight_root,
        schedule,
        state,
        rows,
        expected_entries=6,
    )
    return state, rows


def recompute_preflight_relay(
    preflight_root: Path,
    relay: dict[str, Any],
    *,
    controller_pod: str,
    controller_pod_uid: str,
    formal_run_id: str,
) -> None:
    observability = preflight_root / "observability"
    identity = load_regular_object(observability / "relay-identity.json")
    expected_identity = {
        "formatVersion": 1,
        "namespace": "minecraft",
        "pod": controller_pod,
        "podUid": controller_pod_uid,
        "formalRunId": formal_run_id,
        "container": "controller",
        "serviceAccountName": "bluemap-perf-formal-controller",
        "requiredLabels": {
            "app.kubernetes.io/name": "bluemap-perf-formal-controller",
            "app.kubernetes.io/part-of": "bluemap-web-performance",
            "bluemap.guenter.cloud/experiment-id": formal_run_id,
        },
        "owner": {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": "bluemap-perf-formal-controller",
            "uid": identity.get("owner", {}).get("uid")
            if isinstance(identity.get("owner"), dict)
            else None,
        },
        "limits": {"cpuCores": 2.0, "memoryBytes": float(2 * 1024**3)},
        "source": "metrics.k8s.io/v1beta1",
    }
    owner_uid = expected_identity["owner"]["uid"]
    if not isinstance(owner_uid, str) or not owner_uid or identity != expected_identity:
        raise AnalysisFailure("preflight raw relay identity differs")
    readiness = load_regular_object(observability / "relay-readiness.json")
    readiness_started = timestamp_epoch(
        readiness.get("startedAt"), "preflight relay readiness startedAt"
    )
    readiness_completed = timestamp_epoch(
        readiness.get("completedAt"), "preflight relay readiness completedAt"
    )
    transient = readiness.get("transientErrors")
    if (
        readiness.get("formatVersion") != 1
        or readiness.get("ready") is not True
        or not isinstance(readiness.get("attempts"), int)
        or isinstance(readiness.get("attempts"), bool)
        or readiness["attempts"] < 1
        or not isinstance(readiness.get("timeoutSeconds"), int)
        or readiness["timeoutSeconds"] < 30
        or isinstance(readiness.get("pollIntervalSeconds"), bool)
        or not isinstance(readiness.get("pollIntervalSeconds"), (int, float))
        or not 0.1 <= float(readiness["pollIntervalSeconds"]) <= 30
        or not isinstance(transient, list)
        or readiness["attempts"] != len(transient) + 1
        or readiness_completed < readiness_started
    ):
        raise AnalysisFailure("preflight relay readiness evidence differs")
    for error in transient:
        if (
            not isinstance(error, dict)
            or set(error) != {"failedAt", "error"}
            or not isinstance(error.get("error"), str)
            or not error["error"]
        ):
            raise AnalysisFailure("preflight relay readiness error is malformed")
        timestamp_epoch(error["failedAt"], "preflight relay readiness error")
    errors_path = observability / "relay-errors.ndjson"
    errors = load_strict_ndjson(errors_path, "preflight relay errors")
    if errors or errors_path.stat().st_size != 0:
        raise AnalysisFailure("preflight measured relay interval contains errors")
    samples = load_strict_ndjson(
        observability / "relay-samples.ndjson", "preflight relay samples"
    )
    if not samples:
        raise AnalysisFailure("preflight relay samples are empty")
    parsed: list[dict[str, Any]] = []
    prior_fetched = -math.inf
    expected_sample_keys = {
        "requestedAt",
        "fetchedAt",
        "metricsTimestamp",
        "window",
        "pod",
        "podUid",
        "container",
        "cpuCores",
        "memoryBytes",
        "cpuLimitRatio",
        "memoryLimitRatio",
        "metricAgeSeconds",
    }
    for sample in samples:
        if set(sample) != expected_sample_keys:
            raise AnalysisFailure("preflight relay sample shape differs")
        requested = timestamp_epoch(sample["requestedAt"], "relay requestedAt")
        fetched = timestamp_epoch(sample["fetchedAt"], "relay fetchedAt")
        source = timestamp_epoch(sample["metricsTimestamp"], "relay metricsTimestamp")
        if requested > fetched or fetched < prior_fetched or source > fetched + 5:
            raise AnalysisFailure("preflight relay sample timestamps differ")
        prior_fetched = fetched
        match = METRICS_WINDOW.fullmatch(str(sample.get("window")))
        if match is None or float(match.group(1)) <= 0:
            raise AnalysisFailure("preflight relay sample window differs")
        if (
            sample.get("pod") != controller_pod
            or sample.get("podUid") != controller_pod_uid
            or sample.get("container") != "controller"
        ):
            raise AnalysisFailure("preflight relay sample identity differs")
        cpu = finite_number(sample.get("cpuCores"), "relay cpuCores", minimum=0)
        memory = finite_number(
            sample.get("memoryBytes"), "relay memoryBytes", minimum=0
        )
        cpu_ratio = finite_number(
            sample.get("cpuLimitRatio"), "relay cpuLimitRatio", minimum=0
        )
        memory_ratio = finite_number(
            sample.get("memoryLimitRatio"), "relay memoryLimitRatio", minimum=0
        )
        age = finite_number(
            sample.get("metricAgeSeconds"), "relay metricAgeSeconds", minimum=0
        )
        if (
            not math.isclose(cpu_ratio, cpu / 2.0, rel_tol=1e-12, abs_tol=1e-12)
            or not math.isclose(
                memory_ratio,
                memory / float(2 * 1024**3),
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or not math.isclose(age, max(0.0, fetched - source), abs_tol=1e-6)
        ):
            raise AnalysisFailure("preflight relay sample derived values differ")
        parsed.append({**sample, "_source": source})
    relay_started = timestamp_epoch(relay["startedAt"], "preflight relay startedAt")
    relay_stopped = timestamp_epoch(relay["stoppedAt"], "preflight relay stoppedAt")
    if (
        not readiness_started <= relay_started <= readiness_completed
        or not math.isclose(
            timestamp_epoch(samples[0]["fetchedAt"], "first relay fetchedAt"),
            relay_started,
            abs_tol=1e-6,
        )
        or timestamp_epoch(samples[-1]["fetchedAt"], "last relay fetchedAt")
        > relay_stopped
    ):
        raise AnalysisFailure("preflight relay interval does not bracket raw samples")
    unique = {float(sample["_source"]): sample for sample in parsed}
    selected = sorted(unique.values(), key=lambda sample: float(sample["_source"]))
    sources = [float(sample["_source"]) for sample in selected]
    cpu_ratios = [float(sample["cpuLimitRatio"]) for sample in selected]
    memory_ratios = [float(sample["memoryLimitRatio"]) for sample in selected]
    recomputed = {
        "successfulFetches": len(samples),
        "errors": 0,
        "uniqueMetricTimestamps": len(selected),
        "metricWindows": sorted({str(sample["window"]) for sample in selected}),
        "maximumUniqueMetricTimestampGapSeconds": max(
            (right - left for left, right in zip(sources, sources[1:])), default=0.0
        ),
        "maximumMetricAgeSeconds": max(
            float(sample["metricAgeSeconds"]) for sample in selected
        ),
        "initialCoverageGapSeconds": abs(sources[0] - relay_started),
        "finalCoverageGapSeconds": abs(relay_stopped - sources[-1]),
        "p95CpuLimitRatio": sorted(cpu_ratios)[
            max(0, math.ceil(0.95 * len(cpu_ratios)) - 1)
        ],
        "maximumCpuLimitRatio": max(cpu_ratios),
        "maximumMemoryLimitRatio": max(memory_ratios),
    }
    observed = relay.get("observed")
    if not isinstance(observed, dict) or set(observed) != set(recomputed):
        raise AnalysisFailure("preflight relay observed summary shape differs")
    for key, expected in recomputed.items():
        actual = observed[key]
        if isinstance(expected, list):
            equal = actual == expected
        elif isinstance(expected, (int, float)):
            equal = (
                not isinstance(actual, bool)
                and isinstance(actual, (int, float))
                and math.isclose(float(actual), float(expected), abs_tol=1e-6)
            )
        else:
            equal = actual == expected
        if not equal:
            raise AnalysisFailure(f"preflight relay observed {key} was not recomputed")


def validate_preflight_attestation(
    value: Any,
    *,
    run_root: Path,
    matrix: dict[str, Any],
    matrix_digest: str,
    schedule_digest: str,
    admission_digest: str,
    bundle_digest: str,
    orchestrator_digest: str,
    load_generator_sha256: str,
    execution_identity: dict[str, Any],
    expected_admission: dict[str, str],
) -> dict[str, Any]:
    expected_keys = {
        "formatVersion",
        "report",
        "reportSha256",
        "matrixSha256",
        "scheduleSha256",
        "evidenceManifestSha256",
        "controllerPod",
        "controllerPodUid",
        "traffic",
        "loadGeneratorSha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("formatVersion") != 1
    ):
        raise AnalysisFailure("run state preflightAttestation is malformed")
    expected_root = run_root.with_name(f"{run_root.name}-preflight")
    if expected_root.is_symlink():
        raise AnalysisFailure("preflight artifact root must not be a symlink")
    expected_report = expected_root / "preflight-report.json"
    expected_reference = (
        f"../{run_root.name}-preflight/preflight-report.json"
    )
    report_value = value.get("report")
    if (
        report_value != expected_reference
        or (run_root / expected_reference).resolve() != expected_report
    ):
        raise AnalysisFailure("preflight report is not the exact sibling artifact")
    if not expected_report.is_file() or expected_report.is_symlink():
        raise AnalysisFailure("preflight report is missing or a symlink")
    validate_digest(value.get("reportSha256"), "preflight reportSha256")
    if sha256_file(expected_report) != value["reportSha256"]:
        raise AnalysisFailure("preflight report digest differs")
    report = load_object(expected_report)
    evidence_path = expected_root / "preflight-evidence.json"
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise AnalysisFailure("preflight raw-evidence manifest is missing")
    validate_digest(
        value.get("evidenceManifestSha256"),
        "preflight evidenceManifestSha256",
    )
    if (
        sha256_file(evidence_path) != value["evidenceManifestSha256"]
        or report.get("evidenceManifestSha256")
        != value["evidenceManifestSha256"]
        or load_object(evidence_path) != preflight_evidence_inventory(expected_root)
    ):
        raise AnalysisFailure("preflight raw-evidence inventory differs")
    completed_at = timestamp_epoch(
        report.get("completedAt"), "preflight report completedAt"
    )
    if completed_at <= 0:
        raise AnalysisFailure("preflight completion time is invalid")
    formal_state = load_regular_object(run_root / "state.json")
    validate_digest(
        formal_state.get("analyzerSha256"), "formal state analyzerSha256"
    )
    formal_created_at = timestamp_epoch(
        formal_state.get("createdAt"), "formal state createdAt"
    )
    if (
        completed_at > formal_created_at
        or formal_created_at - completed_at > 300.0
    ):
        raise AnalysisFailure("preflight-to-formal handoff is stale or out of order")
    expected_source = {
        "matrixSha256": matrix_digest,
        "scheduleSha256": schedule_digest,
        "runtimeAdmissionIdentitiesSha256": admission_digest,
        "bundleManifestSha256": bundle_digest,
        "manifestSha256": matrix["manifestSha256"],
    }
    derived = report.get("derivedInputs")
    if not isinstance(derived, dict) or set(derived) != {
        "matrixSha256",
        "scheduleSha256",
        "provenanceSha256",
        "sha256SumsSha256",
    }:
        raise AnalysisFailure("preflight derived input identity is malformed")
    for key in ("matrixSha256", "scheduleSha256"):
        validate_digest(derived.get(key), f"preflight derivedInputs.{key}")
        if value.get(key) != derived[key]:
            raise AnalysisFailure(f"preflight attestation {key} differs")
    input_root = expected_root / "inputs"
    derived_files = {
        "matrixSha256": input_root / "matrix.json",
        "scheduleSha256": input_root / "schedule.json",
        "provenanceSha256": input_root / "provenance.json",
        "sha256SumsSha256": input_root / "SHA256SUMS",
    }
    for key, path in derived_files.items():
        if not path.is_file() or path.is_symlink() or sha256_file(path) != derived[key]:
            raise AnalysisFailure(f"preserved preflight {key} differs")
    preflight_matrix = load_object(derived_files["matrixSha256"])
    preflight_schedule = load_object(derived_files["scheduleSha256"])
    validate_digest(report.get("generatorSha256"), "preflight generatorSha256")
    variant_order = (
        "java-new-postgresql",
        "rust-postgresql",
        "java-new-postgresql-r3",
        "rust-postgresql-r3",
    )
    formal_variant_map = {
        variant["id"]: variant
        for variant in matrix["variants"]
        if isinstance(variant, dict) and isinstance(variant.get("id"), str)
    }
    try:
        expected_preflight_variants = [
            formal_variant_map[variant_id] for variant_id in variant_order
        ]
    except KeyError as error:
        raise AnalysisFailure("formal preflight variant is missing") from error
    formal_cases = {
        case["id"]: case
        for case in matrix["cases"]
        if case["id"]
        in {
            "large-object-r1",
            "map-mixed-r15",
            "map-mixed-horizontal-r40",
        }
    }
    expected_preflight_cases: list[dict[str, Any]] = []
    for source_id, expected_case in (
        (
            "large-object-r1",
            {
                "id": "preflight-large-r1",
                "profile": "large-object",
                "rate": 1,
                "viewers": 15,
                "markerIntervalSeconds": 10,
                "latencyP95Milliseconds": 10000,
                "latencyP99Milliseconds": 20000,
                "acceptEncoding": "zstd",
                "storedEncoding": "zstd",
                "variants": ["java-new-postgresql", "rust-postgresql"],
            },
        ),
        (
            "map-mixed-r15",
            {
                "id": "preflight-map-r15",
                "profile": "map-data-mixed",
                "rate": 15,
                "viewers": 15,
                "markerIntervalSeconds": 10,
                "latencyP95Milliseconds": 10000,
                "latencyP99Milliseconds": 20000,
                "acceptEncoding": "zstd",
                "storedEncoding": "zstd",
                "variants": ["java-new-postgresql", "rust-postgresql"],
            },
        ),
        (
            "map-mixed-horizontal-r40",
            {
                "id": "preflight-horizontal-r40",
                "profile": "map-data-mixed",
                "rate": 40,
                "viewers": 40,
                "markerIntervalSeconds": 10,
                "latencyP95Milliseconds": 5000,
                "latencyP99Milliseconds": 10000,
                "acceptEncoding": "zstd",
                "storedEncoding": "zstd",
                "variants": ["java-new-postgresql-r3", "rust-postgresql-r3"],
            },
        ),
    ):
        source = formal_cases.get(source_id)
        if not isinstance(source, dict):
            raise AnalysisFailure("formal preflight source case is missing")
        source_controls = {
            key: item for key, item in source.items() if key not in {"id", "variants"}
        }
        expected_controls = {
            key: item
            for key, item in expected_case.items()
            if key not in {"id", "variants"}
        }
        if source_controls != expected_controls:
            raise AnalysisFailure("formal preflight source case controls differ")
        expected_preflight_cases.append(expected_case)
    expected_preflight_matrix = {
        "formatVersion": 3,
        "benchmarkGitRevision": matrix["benchmarkGitRevision"],
        "manifestSha256": matrix["manifestSha256"],
        "mapIds": matrix["mapIds"],
        "scheduleSeed": "bluemap-web-performance-ssh-l4-preflight-v1",
        "traceSeed": matrix["traceSeed"],
        "repetitions": 1,
        "controls": {
            "warmupDuration": "30s",
            "measurementDuration": "2m",
            "cooldownSeconds": 15,
            "minimumAchievedRateRatio": 0.99,
            "preAllocatedVUs": 256,
            "maxVUs": 512,
        },
        "cases": expected_preflight_cases,
        "variants": expected_preflight_variants,
    }
    expected_preflight_schedule = build_expected_schedule(
        expected_preflight_matrix,
        derived["matrixSha256"],
        "bluemap-web-performance-ssh-l4-preflight-v1",
        expected_repetitions=1,
    )
    entries = preflight_schedule.get("entries")
    if (
        preflight_matrix != expected_preflight_matrix
        or preflight_schedule != expected_preflight_schedule
        or not isinstance(entries, list)
        or len(entries) != 6
        or [entry.get("sequence") for entry in entries] != list(range(1, 7))
    ):
        raise AnalysisFailure("preserved preflight matrix/schedule identity differs")
    preflight_state, preflight_rows = validate_preflight_raw_execution(
        expected_root,
        preflight_schedule,
        preflight_matrix,
        derived_files["matrixSha256"],
        derived_files["scheduleSha256"],
        execution_identity,
        admission_digest=admission_digest,
        bundle_digest=bundle_digest,
        orchestrator_digest=orchestrator_digest,
        analyzer_digest=formal_state["analyzerSha256"],
        load_generator_sha256=load_generator_sha256,
        expected_admission=expected_admission,
    )
    if completed_at < timestamp_epoch(
        preflight_state.get("completedAt"), "preflight state completedAt"
    ):
        raise AnalysisFailure("preflight report predates execution completion")
    report_entries = report.get("entries")
    if (
        not isinstance(report_entries, list)
        or len(report_entries) != 6
        or [entry.get("sequence") for entry in report_entries]
        != list(range(1, 7))
        or any(
            entry.get("status") != "completed"
            or entry.get("result") != "passed"
            or entry.get("runnerExitStatus") != 0
            for entry in report_entries
        )
    ):
        raise AnalysisFailure("preflight report does not contain six passed entries")
    for report_entry, scheduled_entry in zip(report_entries, entries):
        expected_entry = {
            "sequence": scheduled_entry["sequence"],
            "entryId": scheduled_entry["entryId"],
            "runnerCaseId": scheduled_entry["runnerCaseId"],
            "variantId": scheduled_entry["variantId"],
            "status": "completed",
            "result": "passed",
            "runnerExitStatus": 0,
        }
        if report_entry != expected_entry:
            raise AnalysisFailure("preflight report entry identity differs")
    expected_traffic = {
        "mode": "ssh-l4-traefik",
        "baseUrl": TRAFFIC_BASE_URLS["ssh-l4-traefik"],
        "service": "bluemap-perf-public",
        "port": 8100,
        "requiresEdgeBypass": False,
        "tunnel": SSH_L4_TRAEFIK_TUNNEL,
    }
    controller_relay = report.get("controllerRelay")
    if (
        report.get("formatVersion") != 1
        or report.get("kind") != "ssh-l4-traefik-preflight"
        or report.get("passed") is not True
        or report.get("failures") != []
        or report.get("formalRunId") != execution_identity["formalRunId"]
        or report.get("benchmarkGitRevision") != matrix["benchmarkGitRevision"]
        or report.get("sourceFormalInputs") != expected_source
        or report.get("orchestratorSha256") != orchestrator_digest
        or report.get("traffic") != expected_traffic
        or value.get("traffic") != expected_traffic
        or execution_identity.get("traffic") != expected_traffic
        or report.get("loadGeneratorIdentitySha256")
        != execution_identity["loadGeneratorIdentitySha256"]
        or report.get("loadGeneratorSha256") != load_generator_sha256
        or value.get("loadGeneratorSha256") != load_generator_sha256
        or not isinstance(controller_relay, dict)
        or controller_relay.get("passed") is not True
        or controller_relay.get("pod") != value.get("controllerPod")
        or controller_relay.get("podUid") != value.get("controllerPodUid")
    ):
        raise AnalysisFailure("preflight report identity or gate result differs")
    relay_path = expected_root / "observability" / "relay-headroom.json"
    if (
        not relay_path.is_file()
        or relay_path.is_symlink()
        or controller_relay.get("headroomSha256") != sha256_file(relay_path)
    ):
        raise AnalysisFailure("preflight relay headroom evidence differs")
    relay = load_object(relay_path)
    relay_thresholds = {
        "p95CpuLimitRatio": 0.70,
        "maximumCpuLimitRatio": 0.90,
        "maximumMemoryLimitRatio": 0.80,
        "minimumUniqueMetricTimestamps": 6,
        "maximumUniqueMetricTimestampGapSeconds": 30.0,
        "maximumMetricAgeSeconds": 60.0,
        "maximumCoverageGapSeconds": 60.0,
    }
    relay_checks = {
        "noMetricsApiErrors",
        "minimumUniqueMetricTimestamps",
        "maximumUniqueMetricTimestampGapSeconds",
        "maximumMetricAgeSeconds",
        "initialCoverageGapSeconds",
        "finalCoverageGapSeconds",
        "p95CpuLimitRatio",
        "maximumCpuLimitRatio",
        "maximumMemoryLimitRatio",
    }
    relay_observed_keys = {
        "successfulFetches",
        "errors",
        "uniqueMetricTimestamps",
        "metricWindows",
        "maximumUniqueMetricTimestampGapSeconds",
        "maximumMetricAgeSeconds",
        "initialCoverageGapSeconds",
        "finalCoverageGapSeconds",
        "p95CpuLimitRatio",
        "maximumCpuLimitRatio",
        "maximumMemoryLimitRatio",
    }
    relay_limitation = (
        "metrics.k8s.io exposes coarse aggregate controller-container CPU and "
        "memory only; it cannot attribute usage to the SSH relay process or "
        "prove bandwidth and CPU-throttling headroom"
    )
    if (
        relay.get("formatVersion") != 1
        or relay.get("passed") is not True
        or relay.get("namespace") != "minecraft"
        or relay.get("pod") != value.get("controllerPod")
        or relay.get("podUid") != value.get("controllerPodUid")
        or relay.get("container") != "controller"
        or relay.get("source") != "metrics.k8s.io/v1beta1"
        or not isinstance(relay.get("startedAt"), str)
        or not isinstance(relay.get("stoppedAt"), str)
        or relay.get("limits")
        != {"cpuCores": 2.0, "memoryBytes": float(2 * 1024**3)}
        or relay.get("thresholds") != relay_thresholds
        or not isinstance(relay.get("checks"), dict)
        or set(relay["checks"]) != relay_checks
        or any(check is not True for check in relay["checks"].values())
        or not isinstance(relay.get("observed"), dict)
        or set(relay["observed"]) != relay_observed_keys
        or relay["observed"].get("errors") != 0
        or relay.get("limitation") != relay_limitation
    ):
        raise AnalysisFailure("preflight relay headroom gate is invalid")
    relay_started = timestamp_epoch(relay["startedAt"], "preflight relay startedAt")
    relay_stopped = timestamp_epoch(relay["stoppedAt"], "preflight relay stoppedAt")
    if relay_stopped <= relay_started:
        raise AnalysisFailure("preflight relay sampling interval is invalid")
    observed = relay.get("observed")
    if not isinstance(observed, dict):
        raise AnalysisFailure("preflight relay observations are malformed")

    def relay_number(key: str) -> float:
        raw = observed.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise AnalysisFailure(f"preflight relay {key} is malformed")
        result = float(raw)
        if not math.isfinite(result) or result < 0:
            raise AnalysisFailure(f"preflight relay {key} is malformed")
        return result

    if relay_number("uniqueMetricTimestamps") < 6:
        raise AnalysisFailure("preflight relay has insufficient unique samples")
    metric_windows = observed.get("metricWindows")
    if (
        not isinstance(metric_windows, list)
        or not metric_windows
        or metric_windows != sorted(set(metric_windows))
    ):
        raise AnalysisFailure("preflight relay metric windows are malformed")
    for window in metric_windows:
        if not isinstance(window, str):
            raise AnalysisFailure("preflight relay metric window is malformed")
        match = METRICS_WINDOW.fullmatch(window)
        if match is None or float(match.group(1)) <= 0:
            raise AnalysisFailure("preflight relay metric window is malformed")
    for observed_key, limit in (
        ("maximumUniqueMetricTimestampGapSeconds", 30.0),
        ("maximumMetricAgeSeconds", 60.0),
        ("initialCoverageGapSeconds", 60.0),
        ("finalCoverageGapSeconds", 60.0),
        ("p95CpuLimitRatio", 0.70),
        ("maximumCpuLimitRatio", 0.90),
        ("maximumMemoryLimitRatio", 0.80),
    ):
        if relay_number(observed_key) > limit:
            raise AnalysisFailure(f"preflight relay {observed_key} exceeded")
    recompute_preflight_relay(
        expected_root,
        relay,
        controller_pod=value["controllerPod"],
        controller_pod_uid=value["controllerPodUid"],
        formal_run_id=execution_identity["formalRunId"],
    )
    state_completed = timestamp_epoch(
        preflight_state.get("completedAt"), "preflight state completedAt"
    )
    state_created = timestamp_epoch(
        preflight_state.get("createdAt"), "preflight state createdAt"
    )
    relay_started = timestamp_epoch(
        relay.get("startedAt"), "preflight relay startedAt"
    )
    relay_stopped = timestamp_epoch(
        relay.get("stoppedAt"), "preflight relay stoppedAt"
    )
    if not (
        relay_started
        <= state_created
        <= state_completed
        <= relay_stopped
        <= completed_at
        <= formal_created_at
    ):
        raise AnalysisFailure("preflight completion chronology differs")
    traefik = report.get("traefikPrometheus")
    expected_traefik = {
        "formatVersion": 1,
        "available": False,
        "gating": False,
        "metric": "traefik_service_requests_total",
        "serviceLabelRegex": (
            r"^minecraft-bluemap-perf-public-(?:http|8100)@kubernetes$"
        ),
        "reason": (
            "The configured rancher-monitoring Prometheus has no Traefik "
            "series. Traefik's separate three-replica metrics Service "
            "load-balances one endpoint per scrape, so a complete counter "
            "delta cannot be proven without expanding scope. Exact k6 "
            "status/error checks remain the request-scoped 5xx gate."
        ),
    }
    if traefik != expected_traefik:
        raise AnalysisFailure("preflight Traefik observability limitation differs")
    return {
        "validated": True,
        "report": str(expected_report),
        "reportSha256": value["reportSha256"],
        "completedAt": report["completedAt"],
        "traffic": expected_traffic,
        "entries": 6,
        "semanticReplay": {
            "cases": len(preflight_rows),
            "allEligibleForFormalComparison": all(
                row.get("eligibleForFormalComparison") is True
                for row in preflight_rows
            ),
            "rawRelayRecomputed": True,
        },
        "controllerRelay": controller_relay,
        "traefikPrometheus": traefik,
    }


def verify_sha256s(inputs: Path) -> dict[str, str]:
    checksum_path = inputs / "SHA256SUMS"
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AnalysisFailure(f"cannot read {checksum_path}: {error}") from error
    values: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or HEX_64.fullmatch(parts[0]) is None:
            raise AnalysisFailure(
                f"{checksum_path}:{line_number} is not a SHA256SUMS entry"
            )
        name = parts[1].lstrip("*")
        path = Path(name)
        if path.is_absolute() or len(path.parts) != 1 or name in values:
            raise AnalysisFailure(
                f"{checksum_path}:{line_number} has an unsafe or duplicate path"
            )
        target = inputs / name
        if not target.is_file() or target.is_symlink():
            raise AnalysisFailure(f"checksummed input is missing or a symlink: {target}")
        actual = sha256_file(target)
        if actual != parts[0]:
            raise AnalysisFailure(f"input checksum mismatch: {target}")
        values[name] = actual
    missing = RUNNER_INPUT_FILES - set(values)
    if missing:
        raise AnalysisFailure(
            f"SHA256SUMS omits runner inputs: {', '.join(sorted(missing))}"
        )
    return values


def expected_offered_rate(
    entry: dict[str, Any], manifest: dict[str, Any]
) -> float:
    if entry["profile"] != "live-viewers":
        return float(entry["rate"])
    offered = float(entry["viewers"])
    markers = manifest.get("markers")
    if not isinstance(markers, list):
        raise AnalysisFailure("manifest markers must be an array")
    if markers:
        offered += entry["viewers"] / entry["markerIntervalSeconds"]
    return offered


def compare_workload_identity(
    workload: dict[str, Any],
    entry: dict[str, Any],
    matrix_digest: str,
    schedule_digest: str,
    manifest: dict[str, Any],
    checksums: dict[str, str],
) -> None:
    if workload.get("caseId") != entry["runnerCaseId"]:
        raise AnalysisFailure(f"{entry['entryId']}: workload caseId mismatch")
    formal = workload.get("formalSchedule")
    if not isinstance(formal, dict):
        raise AnalysisFailure(f"{entry['entryId']}: no formalSchedule input")
    expected_formal = {
        "enabled": True,
        "entryId": entry["entryId"],
        "matrixSha256": matrix_digest,
        "scheduleSha256": schedule_digest,
        "entry": entry,
    }
    if formal != expected_formal:
        raise AnalysisFailure(f"{entry['entryId']}: formalSchedule identity mismatch")
    variant = workload.get("variant")
    if not isinstance(variant, dict):
        raise AnalysisFailure(f"{entry['entryId']}: no variant identity")
    expected_variant = {
        "enabled": True,
        "id": entry["variantId"],
        "implementation": entry["implementation"],
        "storageType": entry["storageType"],
        "databaseBackend": entry["databaseBackend"],
        "scheduledReplicaCount": entry["replicaCount"],
        "desiredDeploymentReplicaCount": entry["replicaCount"],
        "namedWebPodCount": entry["replicaCount"],
    }
    if variant != expected_variant:
        raise AnalysisFailure(f"{entry['entryId']}: variant identity mismatch")
    work = workload.get("workload")
    if not isinstance(work, dict):
        raise AnalysisFailure(f"{entry['entryId']}: workload settings are missing")
    expected_fields = {
        "profile": entry["profile"],
        "rate": entry["rate"],
        "viewers": entry["viewers"],
        "markerIntervalSeconds": entry["markerIntervalSeconds"],
        "preAllocatedVUs": entry["preAllocatedVUs"],
        "maxVUs": entry["maxVUs"],
        "minimumAchievedRateRatio": entry["minimumAchievedRateRatio"],
        "traceSeed": entry["traceSeed"],
        "acceptEncoding": entry["acceptEncoding"],
        "storedEncoding": entry["storedEncoding"],
        "contractMode": entry["contractMode"],
        "warmup": entry["warmupDuration"],
        "measurement": entry["measurementDuration"],
        "cooldownSeconds": entry["cooldownSeconds"],
        "repetitions": 1,
        "metricsIntervalSeconds": 5,
        "offeredIterationsPerSecond": expected_offered_rate(entry, manifest),
    }
    for key, expected in expected_fields.items():
        if work.get(key) != expected:
            raise AnalysisFailure(
                f"{entry['entryId']}: workload.{key} mismatch "
                f"({work.get(key)!r} != {expected!r})"
            )
    latency = work.get("latencyGates")
    if not isinstance(latency, dict):
        raise AnalysisFailure(f"{entry['entryId']}: latency gate input is missing")
    expected_latency = {
        "p95Milliseconds": entry["latencyP95Milliseconds"],
        "p99Milliseconds": entry["latencyP99Milliseconds"],
        "largeObjectP95Milliseconds": None,
        "largeObjectP99Milliseconds": None,
        "effectiveP95Milliseconds": entry["latencyP95Milliseconds"],
        "effectiveP99Milliseconds": entry["latencyP99Milliseconds"],
    }
    if latency != expected_latency:
        raise AnalysisFailure(f"{entry['entryId']}: latency gate identity mismatch")
    source = workload.get("source")
    if not isinstance(source, dict):
        raise AnalysisFailure(f"{entry['entryId']}: source identity is missing")
    if source.get("benchmarkCommit") != entry["benchmarkGitRevision"]:
        raise AnalysisFailure(f"{entry['entryId']}: benchmark revision mismatch")
    for key, filename in SOURCE_HASH_FILES.items():
        if source.get(key) != checksums[filename]:
            raise AnalysisFailure(f"{entry['entryId']}: source {key} mismatch")
    targets = workload.get("targets")
    if not isinstance(targets, dict):
        raise AnalysisFailure(f"{entry['entryId']}: target identity is missing")
    if targets.get("mapIds") != entry["mapIds"]:
        raise AnalysisFailure(f"{entry['entryId']}: target map IDs mismatch")
    web_pods = targets.get("webPods")
    if (
        not isinstance(web_pods, list)
        or len(web_pods) != entry["replicaCount"]
        or len(set(web_pods)) != len(web_pods)
        or not all(isinstance(pod, str) and pod for pod in web_pods)
    ):
        raise AnalysisFailure(f"{entry['entryId']}: web Pod target set is invalid")


def pod_snapshot_identity(
    case_dir: Path,
    *,
    pod: str,
    namespace: str,
    entry_id: str,
) -> dict[str, Any]:
    identities: dict[str, dict[str, str]] = {}
    captured_epochs: dict[str, float] = {}
    for label in ("before", "after"):
        path = case_dir / "cluster" / label / f"pod-{pod}.json"
        if not path.is_file() or path.is_symlink():
            raise AnalysisFailure(
                f"{entry_id}: {label} snapshot for Pod/{pod} is missing or a symlink"
            )
        snapshot = load_object(path)
        captured_epochs[label] = timestamp_epoch(
            snapshot.get("capturedAt"),
            f"{entry_id}: {label} Pod/{pod} capturedAt",
        )
        resource = snapshot.get("resource")
        if (
            not isinstance(resource, dict)
            or resource.get("apiVersion") != "v1"
            or resource.get("kind") != "Pod"
            or not isinstance(resource.get("metadata"), dict)
            or not isinstance(resource.get("spec"), dict)
        ):
            raise AnalysisFailure(
                f"{entry_id}: {label} snapshot for Pod/{pod} is malformed"
            )
        metadata = resource["metadata"]
        uid = metadata.get("uid")
        if (
            metadata.get("name") != pod
            or metadata.get("namespace") != namespace
            or not isinstance(uid, str)
            or not uid
        ):
            raise AnalysisFailure(
                f"{entry_id}: {label} snapshot for Pod/{pod} has the wrong identity"
            )
        identities[label] = {
            "namespace": namespace,
            "name": pod,
            "uid": uid,
            "specSha256": canonical_sha256(resource["spec"]),
        }
    if identities["before"] != identities["after"]:
        raise AnalysisFailure(
            f"{entry_id}: Pod/{pod} identity or redacted spec changed during the case"
        )
    return {
        **identities["before"],
        "beforeCapturedEpoch": captured_epochs["before"],
        "afterCapturedEpoch": captured_epochs["after"],
    }


def cpu_list_count(value: Any, label: str) -> int:
    if not isinstance(value, str) or not value:
        raise AnalysisFailure(f"{label} must be a non-empty Linux CPU list")
    cpus: set[int] = set()
    for part in value.split(","):
        bounds = part.split("-")
        if (
            len(bounds) not in {1, 2}
            or any(re.fullmatch(r"[0-9]+", bound) is None for bound in bounds)
        ):
            raise AnalysisFailure(f"{label} is not a valid Linux CPU list")
        first = int(bounds[0])
        last = int(bounds[-1])
        if last < first or last > 4095:
            raise AnalysisFailure(f"{label} has an invalid CPU range")
        selected = set(range(first, last + 1))
        if cpus & selected:
            raise AnalysisFailure(f"{label} contains overlapping CPUs")
        cpus.update(selected)
    if not cpus:
        raise AnalysisFailure(f"{label} selects no CPUs")
    return len(cpus)


def validate_runpod_runtime_identity(
    value: Any,
    frozen: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisFailure(f"{label} must be an object")
    expected_keys = {
        "capturedAt",
        "formatVersion",
        "imageDigest",
        "runId",
        "sourceRevision",
        "runpod",
        "runtime",
        "startedAt",
    }
    if set(value) != expected_keys or value.get("formatVersion") != 1:
        raise AnalysisFailure(f"{label} is malformed")
    timestamp_epoch(value.get("capturedAt"), f"{label}.capturedAt")
    timestamp_epoch(value.get("startedAt"), f"{label}.startedAt")
    frozen_runpod = frozen["runpod"]
    runtime_runpod = value.get("runpod")
    if (
        value.get("runId") != frozen["runId"]
        or value.get("sourceRevision") != frozen["sourceRevision"]
        or value.get("imageDigest") != frozen_runpod["imageDigest"]
        or not isinstance(runtime_runpod, dict)
        or set(runtime_runpod)
        != {
            "configuredVcpuCount",
            "cpuFlavor",
            "dataCenterId",
            "podHostname",
            "podId",
            "publicIp",
            "vcpuCount",
        }
        or runtime_runpod.get("podId") != frozen_runpod["podId"]
        or runtime_runpod.get("dataCenterId") != frozen_runpod["dataCenterId"]
        or runtime_runpod.get("publicIp") != frozen_runpod["publicIp"]
        or runtime_runpod.get("cpuFlavor") != frozen_runpod["cpuFlavorId"]
        or runtime_runpod.get("vcpuCount") != frozen_runpod["vcpuCount"]
        or runtime_runpod.get("configuredVcpuCount") != frozen_runpod["vcpuCount"]
        or not isinstance(runtime_runpod.get("podHostname"), str)
        or not runtime_runpod["podHostname"]
    ):
        raise AnalysisFailure(f"{label} differs from the frozen RunPod identity")
    runtime = value.get("runtime")
    cgroup_version = (
        runtime.get("cgroupVersion") if isinstance(runtime, dict) else None
    )
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {
            "cgroupVersion",
            "cpu",
            "hostname",
            "k6Version",
            "kernel",
            "memoryBytes",
            "memoryCapacityBytes",
            "onlineProcessors",
        }
        or not isinstance(cgroup_version, int)
        or isinstance(cgroup_version, bool)
        or cgroup_version not in {1, 2}
        or not isinstance(runtime.get("hostname"), str)
        or not runtime["hostname"]
        or not isinstance(runtime.get("kernel"), str)
        or not runtime["kernel"]
        or not isinstance(runtime.get("k6Version"), str)
        or not runtime["k6Version"].startswith("k6 v2.1.0 ")
    ):
        raise AnalysisFailure(f"{label}.runtime is malformed")
    for key in ("memoryBytes", "memoryCapacityBytes", "onlineProcessors"):
        raw = runtime.get(key)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
            raise AnalysisFailure(f"{label}.runtime.{key} must be positive")
    if runtime["onlineProcessors"] < frozen_runpod["vcpuCount"]:
        raise AnalysisFailure(f"{label} exposes too few online processors")

    cpu = runtime.get("cpu")
    expected_cpu_keys = {
        "affinity",
        "affinityCount",
        "cgroupCpuMax",
        "cpusetEffective",
        "cpusetEffectiveCount",
        "effectiveVcpuCount",
        "periodMicros",
        "quotaMicros",
        "quotaVcpuCount",
    }
    if not isinstance(cpu, dict) or set(cpu) != expected_cpu_keys:
        raise AnalysisFailure(f"{label}.runtime.cpu is malformed")
    cpuset_count = cpu_list_count(
        cpu.get("cpusetEffective"), f"{label}.runtime.cpu.cpusetEffective"
    )
    affinity_count = cpu_list_count(
        cpu.get("affinity"), f"{label}.runtime.cpu.affinity"
    )
    if (
        cpu.get("cpusetEffectiveCount") != cpuset_count
        or cpu.get("affinityCount") != affinity_count
    ):
        raise AnalysisFailure(f"{label}.runtime CPU-list counts do not recompute")

    period = cpu.get("periodMicros")
    quota = cpu.get("quotaMicros")
    quota_vcpus = cpu.get("quotaVcpuCount")
    if (
        not isinstance(period, int)
        or isinstance(period, bool)
        or period < 1
    ):
        raise AnalysisFailure(f"{label}.runtime.cpu.periodMicros is invalid")
    if quota is None:
        if quota_vcpus is not None or cpu.get("cgroupCpuMax") != f"max {period}":
            raise AnalysisFailure(f"{label}.runtime unlimited CPU quota is malformed")
        capacity_candidates = [cpuset_count, affinity_count]
    else:
        if (
            not isinstance(quota, int)
            or isinstance(quota, bool)
            or quota < 1
            or quota % period != 0
            or quota_vcpus != quota // period
            or cpu.get("cgroupCpuMax") != f"{quota} {period}"
        ):
            raise AnalysisFailure(f"{label}.runtime finite CPU quota is malformed")
        capacity_candidates = [cpuset_count, affinity_count, quota_vcpus]

    effective_vcpus = min(capacity_candidates)
    if (
        cpu.get("effectiveVcpuCount") != effective_vcpus
        or effective_vcpus != frozen_runpod["vcpuCount"]
        or effective_vcpus != 8
    ):
        raise AnalysisFailure(
            f"{label}.runtime independently observed CPU capacity is not exactly 8"
        )
    return value


def capacity_percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def load_runpod_resource_samples(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise AnalysisFailure(f"RunPod resource telemetry is missing: {path}")
    samples: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AnalysisFailure(f"cannot read RunPod resource telemetry {path}") from error
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise AnalysisFailure(f"{path}:{number}: invalid JSON") from error
        if not isinstance(value, dict):
            raise AnalysisFailure(f"{path}:{number}: sample is not an object")
        if set(value) != {
            "capturedAt",
            "cpuUsageUsec",
            "cpuThrottledUsec",
            "memoryCurrentBytes",
            "network",
        } or not isinstance(value.get("network"), dict) or set(
            value["network"]
        ) != {"rxBytes", "txBytes"}:
            raise AnalysisFailure(f"{path}:{number}: malformed RunPod sample")
        for key, raw in (
            ("cpuUsageUsec", value.get("cpuUsageUsec")),
            ("cpuThrottledUsec", value.get("cpuThrottledUsec")),
            ("memoryCurrentBytes", value.get("memoryCurrentBytes")),
            ("network.rxBytes", value["network"].get("rxBytes")),
            ("network.txBytes", value["network"].get("txBytes")),
        ):
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise AnalysisFailure(f"{path}:{number}: {key} is invalid")
        timestamp_epoch(value.get("capturedAt"), f"{path}:{number} capturedAt")
        samples.append(value)
    if len(samples) < 2:
        raise AnalysisFailure(f"{path}: fewer than two RunPod resource samples")
    return samples


def recompute_runpod_capacity(
    samples: list[dict[str, Any]],
    frozen: dict[str, Any],
    runtime_identity: dict[str, Any],
) -> dict[str, Any]:
    vcpu_count = float(frozen["runpod"]["vcpuCount"])
    download_mbps = float(frozen["runpod"]["minDownloadMbps"])
    upload_mbps = float(frozen["runpod"]["minUploadMbps"])
    memory_capacity = float(runtime_identity["runtime"]["memoryCapacityBytes"])
    cpu_ratios: list[float] = []
    throttled_ratios: list[float] = []
    gaps: list[float] = []
    rx_mbps: list[float] = []
    tx_mbps: list[float] = []
    memory_bytes = [float(samples[0]["memoryCurrentBytes"])]
    previous = samples[0]
    for current in samples[1:]:
        elapsed = (
            parse_timestamp(current["capturedAt"], "RunPod resource capturedAt")
            - parse_timestamp(previous["capturedAt"], "RunPod resource capturedAt")
        ).total_seconds()
        if elapsed <= 0:
            raise AnalysisFailure(
                "RunPod resource telemetry timestamps are not increasing"
            )
        gaps.append(elapsed)
        usage_delta = current["cpuUsageUsec"] - previous["cpuUsageUsec"]
        throttle_delta = (
            current["cpuThrottledUsec"] - previous["cpuThrottledUsec"]
        )
        rx_delta = current["network"]["rxBytes"] - previous["network"]["rxBytes"]
        tx_delta = current["network"]["txBytes"] - previous["network"]["txBytes"]
        if min(usage_delta, throttle_delta, rx_delta, tx_delta) < 0:
            raise AnalysisFailure("a RunPod cumulative resource counter decreased")
        cpu_ratios.append(usage_delta / (elapsed * 1_000_000 * vcpu_count))
        throttled_ratios.append(throttle_delta / max(float(usage_delta), 1.0))
        rx_mbps.append(rx_delta * 8 / elapsed / 1_000_000)
        tx_mbps.append(tx_delta * 8 / elapsed / 1_000_000)
        memory_bytes.append(float(current["memoryCurrentBytes"]))
        previous = current
    receive_p95 = capacity_percentile(rx_mbps, 0.95)
    transmit_p95 = capacity_percentile(tx_mbps, 0.95)
    return {
        "sampleCount": len(samples),
        "maximumSampleGapSeconds": max(gaps),
        "cpuRatio": {
            "p95": capacity_percentile(cpu_ratios, 0.95),
            "maximum": max(cpu_ratios),
        },
        "throttledCpuRatio": {
            "p95": capacity_percentile(throttled_ratios, 0.95),
            "maximum": max(throttled_ratios),
        },
        "memory": {
            "maximumBytes": max(memory_bytes),
            "capacityBytes": memory_capacity,
            "maximumRatio": max(memory_bytes) / memory_capacity,
        },
        "networkMbps": {
            "receiveP95": receive_p95,
            "receiveMaximum": max(rx_mbps),
            "receiveCapacity": download_mbps,
            "receiveP95Ratio": receive_p95 / download_mbps,
            "transmitP95": transmit_p95,
            "transmitMaximum": max(tx_mbps),
            "transmitCapacity": upload_mbps,
            "transmitP95Ratio": transmit_p95 / upload_mbps,
        },
    }


def equal_json_numbers(left: Any, right: Any) -> bool:
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return close_number(left, right)
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(
            equal_json_numbers(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            equal_json_numbers(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def validate_runpod_capacity_artifact(
    phase_dir: Path,
    frozen: dict[str, Any],
    runtime_identity: dict[str, Any],
    label: str,
    phase_window: Sequence[float] | None,
    configured_duration_seconds: float,
) -> dict[str, Any]:
    telemetry_path = phase_dir / "load-generator-resources.ndjson"
    capacity_path = phase_dir / "load-generator-capacity.json"
    samples = load_runpod_resource_samples(telemetry_path)
    if phase_window is None or len(phase_window) != 2:
        raise AnalysisFailure(f"{label}: phase window is missing")
    sample_epochs = [
        timestamp_epoch(sample["capturedAt"], f"{label}: resource capturedAt")
        for sample in samples
    ]
    start, end = float(phase_window[0]), float(phase_window[1])
    if (
        sample_epochs[0] < start - 2
        or sample_epochs[-1] > end + 2
        or sample_epochs[0] > start + 15
        or sample_epochs[-1] - sample_epochs[0]
        < configured_duration_seconds - 2
    ):
        raise AnalysisFailure(
            f"{label}: RunPod telemetry does not cover its phase window"
        )
    capacity = load_regular_object(capacity_path)
    limits = {
        "maximumSampleGapSeconds": 5.0,
        "maximumP95CpuRatio": 0.70,
        "maximumP95ThrottledCpuRatio": 0.01,
        "maximumMemoryRatio": 0.80,
        "maximumP95NetworkRatio": 0.70,
    }
    if (
        set(capacity) != {"formatVersion", "limits", "observed", "passed"}
        or capacity.get("formatVersion") != 1
        or not equal_json_numbers(capacity.get("limits"), limits)
    ):
        raise AnalysisFailure(f"{label}: RunPod capacity artifact is malformed")
    observed = recompute_runpod_capacity(samples, frozen, runtime_identity)
    if not equal_json_numbers(capacity.get("observed"), observed):
        raise AnalysisFailure(f"{label}: RunPod capacity evidence does not recompute")
    expected_passed = (
        observed["maximumSampleGapSeconds"] <= limits["maximumSampleGapSeconds"]
        and observed["cpuRatio"]["p95"] <= limits["maximumP95CpuRatio"]
        and observed["throttledCpuRatio"]["p95"]
        <= limits["maximumP95ThrottledCpuRatio"]
        and observed["memory"]["maximumRatio"] <= limits["maximumMemoryRatio"]
        and observed["networkMbps"]["receiveP95Ratio"]
        <= limits["maximumP95NetworkRatio"]
        and observed["networkMbps"]["transmitP95Ratio"]
        <= limits["maximumP95NetworkRatio"]
    )
    if capacity.get("passed") is not expected_passed:
        raise AnalysisFailure(
            f"{label}: reported RunPod capacity result does not recompute"
        )
    return {
        "limits": limits,
        "capacity": {
            "vcpuCount": frozen["runpod"]["vcpuCount"],
            "memoryCapacityBytes": runtime_identity["runtime"][
                "memoryCapacityBytes"
            ],
            "minimumDownloadMbps": frozen["runpod"]["minDownloadMbps"],
            "minimumUploadMbps": frozen["runpod"]["minUploadMbps"],
        },
        "observed": observed,
        "telemetrySha256": sha256_file(telemetry_path),
        "artifactSha256": sha256_file(capacity_path),
        "passed": expected_passed,
    }


def validate_runpod_capacity_phases(
    repetition: Path,
    frozen: dict[str, Any],
    runtime_identity: dict[str, Any],
    entry: dict[str, Any],
    timing: dict[str, Any],
    case_result: str,
) -> dict[str, dict[str, Any]]:
    phases = ("warmup", "measurement")
    phase_windows = timing["phaseWindows"]
    available = {phase for phase in phases if phase in phase_windows}
    if case_result == "passed" and available != set(phases):
        missing = ", ".join(phase for phase in phases if phase not in available)
        raise AnalysisFailure(
            f"{entry['entryId']}: passed result lacks RunPod capacity "
            f"phase evidence: {missing}"
        )

    capacity: dict[str, dict[str, Any]] = {}
    for phase in phases:
        phase_dir = repetition / phase
        telemetry_path = phase_dir / "load-generator-resources.ndjson"
        capacity_path = phase_dir / "load-generator-capacity.json"
        if phase not in available:
            if any(
                path.exists() or path.is_symlink()
                for path in (telemetry_path, capacity_path)
            ):
                raise AnalysisFailure(
                    f"{entry['entryId']}: {phase} RunPod capacity evidence "
                    "exists without a phase window"
                )
            continue
        if telemetry_path.is_symlink() or capacity_path.is_symlink():
            raise AnalysisFailure(
                f"{entry['entryId']}: {phase} RunPod capacity evidence is a symlink"
            )
        capacity[phase] = validate_runpod_capacity_artifact(
            phase_dir,
            frozen,
            runtime_identity,
            f"{entry['entryId']}: {phase}",
            phase_windows[phase],
            duration_seconds(
                entry[f"{phase}Duration"],
                f"{entry['entryId']}: {phase} duration",
            ),
        )

    stable_capacity = [
        {
            "limits": evidence["limits"],
            "capacity": evidence["capacity"],
        }
        for evidence in capacity.values()
    ]
    if any(control != stable_capacity[0] for control in stable_capacity[1:]):
        raise AnalysisFailure(
            f"{entry['entryId']}: RunPod capacity controls differ between phases"
        )
    return capacity


def workload_control_identity(
    case_dir: Path,
    workload: dict[str, Any],
    entry: dict[str, Any],
    execution_identity: dict[str, Any],
    timing: dict[str, Any],
    case_result: str,
) -> dict[str, Any]:
    entry_id = entry["entryId"]
    namespace = workload.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise AnalysisFailure(f"{entry_id}: workload namespace is invalid")
    origin = workload.get("origin")
    if (
        not isinstance(origin, dict)
        or set(origin)
        != {"service", "port", "baseUrl", "correctnessTransport"}
        or origin.get("correctnessTransport") != "direct-cluster-dns"
    ):
        raise AnalysisFailure(f"{entry_id}: workload origin identity is malformed")
    service = origin.get("service")
    port = origin.get("port")
    if (
        not isinstance(service, str)
        or not service
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        raise AnalysisFailure(f"{entry_id}: workload origin identity is invalid")
    expected_origin = f"http://{service}.{namespace}.svc.cluster.local:{port}"
    if normalized_http_url(
        origin.get("baseUrl"), f"{entry_id}: workload origin baseUrl"
    ) != normalized_http_url(expected_origin, f"{entry_id}: expected origin"):
        raise AnalysisFailure(f"{entry_id}: workload origin baseUrl differs from Service")

    traffic = validate_traffic_identity(
        workload.get("traffic"),
        f"{entry_id}: workload traffic identity",
        formal_run_id=execution_identity["formalRunId"],
    )
    expected_traffic = {
        **execution_identity["traffic"],
        "formalRunId": execution_identity["formalRunId"],
    }
    if traffic != expected_traffic:
        raise AnalysisFailure(
            f"{entry_id}: traffic identity differs from execution identity"
        )
    load_generator = workload.get("loadGenerator")
    if (
        not isinstance(load_generator, dict)
        or set(load_generator) != {"backend", "identity"}
        or load_generator.get("backend") != "runpod-ssh"
    ):
        raise AnalysisFailure(f"{entry_id}: loadGenerator identity is malformed")
    generator_identity = validate_runpod_identity(
        load_generator.get("identity"),
        f"{entry_id}: workload loadGeneratorIdentity",
    )
    if not equal_json_numbers(
        generator_identity,
        execution_identity["loadGeneratorIdentity"],
    ):
        raise AnalysisFailure(
            f"{entry_id}: frozen RunPod generator differs from execution identity"
        )
    archived_generator_identity = load_regular_object(
        case_dir / "inputs" / "runpod-load-generator-identity.json"
    )
    case_generator_identity = load_regular_object(
        case_dir / "generator" / "frozen-identity.json"
    )
    if (
        archived_generator_identity != generator_identity
        or case_generator_identity != generator_identity
    ):
        raise AnalysisFailure(
            f"{entry_id}: archived RunPod generator identity differs"
        )

    targets = workload.get("targets")
    assert isinstance(targets, dict)
    database_pods = targets.get("databasePods")
    nodes = targets.get("nodes")
    for label, values in (("databasePods", database_pods), ("nodes", nodes)):
        if (
            not isinstance(values, list)
            or values != sorted(set(values))
            or not all(isinstance(value, str) and value for value in values)
        ):
            raise AnalysisFailure(f"{entry_id}: target {label} identity is invalid")
    if entry["storageType"] == "sql" and not database_pods:
        raise AnalysisFailure(f"{entry_id}: SQL workload has no database Pod")
    if not nodes:
        raise AnalysisFailure(f"{entry_id}: target node identity is empty")

    runtime = workload.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {"pythonCommand"}:
        raise AnalysisFailure(f"{entry_id}: workload runtime identity is malformed")
    python_command = runtime.get("pythonCommand")
    if (
        not isinstance(python_command, str)
        or not Path(python_command).is_absolute()
    ):
        raise AnalysisFailure(
            f"{entry_id}: workload runtime pythonCommand is not absolute"
        )

    observability = workload.get("observability")
    if not isinstance(observability, dict):
        raise AnalysisFailure(f"{entry_id}: observability identity is missing")
    kubernetes = observability.get("metricsKubernetes")
    if (
        not isinstance(kubernetes, dict)
        or kubernetes
        != {
            "enabled": True,
            "intervalSeconds": workload["workload"]["metricsIntervalSeconds"],
        }
    ):
        raise AnalysisFailure(
            f"{entry_id}: Kubernetes observability identity is malformed"
        )
    prometheus = observability.get("prometheus")
    expected_prometheus_keys = {
        "enabled",
        "baseUrl",
        "clusterServiceTransport",
        "stepSeconds",
        "maximumNonTargetNodeCpuRangeCores",
        "maximumNonTargetNodeCpuMeanCores",
        "maximumNonTargetNodeCpuLevelCores",
    }
    if not isinstance(prometheus, dict) or set(prometheus) != expected_prometheus_keys:
        raise AnalysisFailure(f"{entry_id}: Prometheus observability identity is malformed")
    enabled = prometheus.get("enabled")
    if not isinstance(enabled, bool):
        raise AnalysisFailure(f"{entry_id}: Prometheus enabled identity is invalid")
    normalized_url: str | None
    if enabled:
        if prometheus.get("clusterServiceTransport") != "direct-cluster-dns":
            raise AnalysisFailure(
                f"{entry_id}: Prometheus must use direct cluster DNS"
            )
        normalized_url = normalized_http_url(
            prometheus.get("baseUrl"),
            f"{entry_id}: Prometheus baseUrl",
        )
        finite_number(
            prometheus.get("stepSeconds"),
            f"{entry_id}: Prometheus stepSeconds",
            minimum=1,
        )
        for key in (
            "maximumNonTargetNodeCpuRangeCores",
            "maximumNonTargetNodeCpuMeanCores",
            "maximumNonTargetNodeCpuLevelCores",
        ):
            finite_number(
                prometheus.get(key),
                f"{entry_id}: Prometheus {key}",
                minimum=0,
            )
    else:
        normalized_url = None
        if any(
            prometheus.get(key) is not None
            for key in expected_prometheus_keys - {"enabled"}
        ):
            raise AnalysisFailure(
                f"{entry_id}: disabled Prometheus identity has non-null controls"
            )
    normalized_prometheus = dict(prometheus)
    normalized_prometheus["baseUrl"] = normalized_url
    database_pod_identity = pod_snapshot_identity(
        case_dir,
        pod=execution_identity["databasePod"],
        namespace=namespace,
        entry_id=entry_id,
    )
    for label, identity in (("database", database_pod_identity),):
        if (
            identity["beforeCapturedEpoch"] > timing["resultStartedEpoch"] + 1
            or identity["afterCapturedEpoch"] + 1 < timing["resultEndedEpoch"]
            or identity["beforeCapturedEpoch"] > identity["afterCapturedEpoch"]
        ):
            raise AnalysisFailure(
                f"{entry_id}: {label} Pod snapshots do not bracket the runner case"
            )
    live_before = validate_runpod_runtime_identity(
        load_regular_object(case_dir / "generator" / "live-identity-before.json"),
        generator_identity,
        f"{entry_id}: generator live identity before",
    )
    live_after = validate_runpod_runtime_identity(
        load_regular_object(case_dir / "generator" / "live-identity-after.json"),
        generator_identity,
        f"{entry_id}: generator live identity after",
    )
    stable_before = {
        key: value for key, value in live_before.items() if key != "capturedAt"
    }
    stable_after = {
        key: value for key, value in live_after.items() if key != "capturedAt"
    }
    if stable_before != stable_after:
        raise AnalysisFailure(
            f"{entry_id}: RunPod runtime identity changed during the case"
        )
    generator_diff = case_dir / "generator" / "live-identity.diff"
    if generator_diff.is_symlink() or not empty_file(generator_diff):
        raise AnalysisFailure(
            f"{entry_id}: RunPod runtime identity diff is not empty"
        )
    before_epoch = timestamp_epoch(
        live_before["capturedAt"], f"{entry_id}: generator before capturedAt"
    )
    after_epoch = timestamp_epoch(
        live_after["capturedAt"], f"{entry_id}: generator after capturedAt"
    )
    if (
        before_epoch > timing["resultStartedEpoch"] + 1
        or after_epoch + 1 < timing["resultEndedEpoch"]
        or before_epoch > after_epoch
    ):
        raise AnalysisFailure(
            f"{entry_id}: RunPod identity captures do not bracket the runner case"
        )
    repetition = case_dir / "repetitions" / "01"
    capacity = validate_runpod_capacity_phases(
        repetition,
        generator_identity,
        live_before,
        entry,
        timing,
        case_result,
    )
    return {
        "namespace": namespace,
        "origin": {
            "service": service,
            "port": port,
            "baseUrl": normalized_http_url(
                origin["baseUrl"], f"{entry_id}: workload origin baseUrl"
            ),
            "correctnessTransport": origin["correctnessTransport"],
        },
        "traffic": {
            **traffic,
            "baseUrl": normalized_http_url(
                traffic["baseUrl"], f"{entry_id}: traffic baseUrl"
            ),
        },
        "databasePods": database_pods,
        "databasePodIdentity": database_pod_identity,
        "loadGeneratorBackend": "runpod-ssh",
        "loadGeneratorIdentity": generator_identity,
        "loadGeneratorIdentitySha256": execution_identity[
            "loadGeneratorIdentitySha256"
        ],
        "loadGeneratorRuntimeIdentity": stable_before,
        "loadGeneratorCapacity": capacity,
        "nodes": nodes,
        "runtime": {"pythonCommand": python_command},
        "observability": {
            "metricsKubernetes": kubernetes,
            "prometheus": normalized_prometheus,
        },
    }


def validate_runtime_identity(
    case_dir: Path, entry: dict[str, Any], workload: dict[str, Any]
) -> tuple[bool, str | None]:
    path = case_dir / "cluster" / "runtime-identity-before.json"
    identity = load_optional_object(path)
    if identity is None:
        raise AnalysisFailure(
            f"{entry['entryId']}: runtime identity artifact is missing"
        )
    if identity.get("benchmarkGitRevision") != entry["benchmarkGitRevision"]:
        raise AnalysisFailure(f"{entry['entryId']}: runtime revision mismatch")
    configuration = identity.get("configuration")
    runtime_spec = identity.get("runtimeSpec")
    if not isinstance(configuration, dict) or not isinstance(runtime_spec, dict):
        raise AnalysisFailure(f"{entry['entryId']}: malformed runtime identity")
    expected_config = entry["expectedSanitizedConfigSha256"]
    expected_runtime = entry["expectedSanitizedRuntimeSpecSha256"]
    if (
        configuration.get("expectedSanitizedConfigSha256") != expected_config
        or configuration.get("actualSanitizedConfigSha256") != expected_config
        or runtime_spec.get("expectedSanitizedRuntimeSpecSha256")
        != expected_runtime
        or runtime_spec.get("actualSanitizedRuntimeSpecSha256") != expected_runtime
    ):
        raise AnalysisFailure(f"{entry['entryId']}: frozen runtime hash mismatch")
    identities = identity.get("webPods")
    target_pods = workload["targets"]["webPods"]
    if not isinstance(identities, list) or len(identities) != len(target_pods):
        raise AnalysisFailure(f"{entry['entryId']}: runtime Pod identity count mismatch")
    by_pod = {
        item.get("pod"): item for item in identities if isinstance(item, dict)
    }
    if set(by_pod) != set(target_pods):
        raise AnalysisFailure(f"{entry['entryId']}: runtime Pod identity mismatch")
    for pod, item in by_pod.items():
        if (
            item.get("expectedImages") != entry["expectedImages"]
            or item.get("actualImages") != entry["expectedImages"]
        ):
            raise AnalysisFailure(
                f"{entry['entryId']}: image identity mismatch for {pod}"
            )
    passed = (
        identity.get("passed") is True
        and configuration.get("passed") is True
        and runtime_spec.get("passed") is True
        and all(item.get("passed") is True for item in by_pod.values())
    )
    if not passed:
        raise AnalysisFailure(f"{entry['entryId']}: runtime identity did not pass")
    return True, None


def metric_value(
    summary: dict[str, Any] | None, metric: str, field: str
) -> float | None:
    if summary is None:
        return None
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        return None
    data = metrics.get(metric)
    if not isinstance(data, dict):
        return None
    values = data.get("values")
    raw = values.get(field) if isinstance(values, dict) else data.get(field)
    if raw is None and field == "rate":
        raw = data.get("value")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) and value >= 0 else None


def trend(summary: dict[str, Any] | None, metric: str) -> dict[str, float | None]:
    return {
        "p50": metric_value(summary, metric, "med"),
        "p90": metric_value(summary, metric, "p(90)"),
        "p95": metric_value(summary, metric, "p(95)"),
        "p99": metric_value(summary, metric, "p(99)"),
    }


def close_number(left: Any, right: Any) -> bool:
    if (
        isinstance(left, bool)
        or not isinstance(left, (int, float))
        or isinstance(right, bool)
        or not isinstance(right, (int, float))
    ):
        return False
    return math.isclose(
        float(left),
        float(right),
        rel_tol=1e-9,
        abs_tol=1e-9,
    )


def require_summary_metric(
    summary: dict[str, Any] | None,
    metric: str,
    field: str,
    entry_id: str,
) -> float:
    value = metric_value(summary, metric, field)
    if value is None:
        raise AnalysisFailure(
            f"{entry_id}: measurement summary is missing {metric}.{field}"
        )
    return value


def scenario_definitions(
    entry: dict[str, Any], manifest: dict[str, Any]
) -> list[tuple[str, float]]:
    if entry["profile"] != "live-viewers":
        return [("workload", float(entry["rate"]))]
    definitions = [("playerPolling", float(entry["viewers"]))]
    markers = manifest.get("markers")
    if not isinstance(markers, list):
        raise AnalysisFailure(f"{entry['entryId']}: manifest markers is not an array")
    if markers:
        definitions.append(
            (
                "markerPolling",
                entry["viewers"] / entry["markerIntervalSeconds"],
            )
        )
    return definitions


def validate_arrival_identity(
    artifact: dict[str, Any],
    summary: dict[str, Any],
    *,
    entry: dict[str, Any],
    manifest: dict[str, Any],
    duration: str,
) -> None:
    entry_id = entry["entryId"]
    seconds = duration_seconds(duration, f"{entry_id} phase duration")
    minimum_ratio = float(entry["minimumAchievedRateRatio"])
    definitions = scenario_definitions(entry, manifest)
    expected_scenarios: list[dict[str, Any]] = []
    for name, offered in definitions:
        metric_name = f"iterations{{scenario:{name}}}"
        completed = require_summary_metric(
            summary, metric_name, "count", entry_id
        )
        expected = offered * seconds
        minimum = expected * minimum_ratio
        expected_scenarios.append(
            {
                "scenario": name,
                "metric": metric_name,
                "offeredIterationsPerSecond": offered,
                "expectedScheduledIterations": expected,
                "minimumCompletedIterations": minimum,
                "completedIterations": completed,
                "achievedIterationsPerSecondOverConfiguredDuration": (
                    completed / seconds
                ),
                "passed": completed >= minimum,
            }
        )
    overall = require_summary_metric(summary, "iterations", "count", entry_id)
    wall_clock = require_summary_metric(summary, "iterations", "rate", entry_id)
    dropped = require_summary_metric(
        summary, "dropped_iterations", "count", entry_id
    )
    scenario_total = sum(
        scenario["completedIterations"] for scenario in expected_scenarios
    )
    offered_total = sum(
        scenario["offeredIterationsPerSecond"] for scenario in expected_scenarios
    )
    expected_total = offered_total * seconds
    computed_passed = (
        dropped == 0
        and close_number(scenario_total, overall)
        and all(scenario["passed"] for scenario in expected_scenarios)
    )
    expected_scalars = {
        "formatVersion": 1,
        "configuredDuration": duration,
        "configuredDurationSeconds": seconds,
        "minimumAchievedRatio": minimum_ratio,
        "droppedIterations": dropped,
        "scenarioCompletedIterations": scenario_total,
        "scenarioCountsEqualOverall": close_number(scenario_total, overall),
        "passed": computed_passed,
    }
    for key, expected in expected_scalars.items():
        actual = artifact.get(key)
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            matches = close_number(actual, expected)
        else:
            matches = actual == expected
        if not matches:
            raise AnalysisFailure(
                f"{entry_id}: arrival gate {key} does not match summary/config"
            )
    actual_scenarios = artifact.get("scenarios")
    if not isinstance(actual_scenarios, list) or len(actual_scenarios) != len(
        expected_scenarios
    ):
        raise AnalysisFailure(f"{entry_id}: arrival gate scenarios mismatch")
    for actual, expected in zip(actual_scenarios, expected_scenarios, strict=True):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            raise AnalysisFailure(f"{entry_id}: arrival gate scenario is malformed")
        for key, expected_value in expected.items():
            actual_value = actual.get(key)
            if isinstance(expected_value, (int, float)) and not isinstance(
                expected_value, bool
            ):
                matches = close_number(actual_value, expected_value)
            else:
                matches = actual_value == expected_value
            if not matches:
                raise AnalysisFailure(
                    f"{entry_id}: arrival scenario {expected['scenario']} "
                    f"{key} mismatch"
                )
    totals = artifact.get("totals")
    if not isinstance(totals, dict):
        raise AnalysisFailure(f"{entry_id}: arrival gate totals are missing")
    expected_totals = {
        "offeredIterationsPerSecond": offered_total,
        "expectedScheduledIterations": expected_total,
        "minimumCompletedIterations": expected_total * minimum_ratio,
        "completedIterations": overall,
        "achievedIterationsPerSecondOverConfiguredDuration": overall / seconds,
        "k6WallClockIterationsPerSecond": wall_clock,
    }
    if set(totals) != set(expected_totals):
        raise AnalysisFailure(f"{entry_id}: arrival gate totals are malformed")
    for key, expected in expected_totals.items():
        if not close_number(totals.get(key), expected):
            raise AnalysisFailure(f"{entry_id}: arrival total {key} mismatch")


def validate_latency_identity(
    artifact: dict[str, Any],
    summary: dict[str, Any],
    entry: dict[str, Any],
) -> None:
    entry_id = entry["entryId"]
    observed_p95 = require_summary_metric(
        summary, "http_req_duration{traffic:workload}", "p(95)", entry_id
    )
    observed_p99 = require_summary_metric(
        summary, "http_req_duration{traffic:workload}", "p(99)", entry_id
    )
    maximum_p95 = float(entry["latencyP95Milliseconds"])
    maximum_p99 = float(entry["latencyP99Milliseconds"])
    expected = {
        "maximumP95Milliseconds": maximum_p95,
        "maximumP99Milliseconds": maximum_p99,
        "observedP95Milliseconds": observed_p95,
        "observedP99Milliseconds": observed_p99,
        "passed": observed_p95 < maximum_p95 and observed_p99 < maximum_p99,
    }
    if set(artifact) != set(expected):
        raise AnalysisFailure(f"{entry_id}: latency gate is malformed")
    for key, expected_value in expected.items():
        actual = artifact.get(key)
        if isinstance(expected_value, (int, float)) and not isinstance(
            expected_value, bool
        ):
            matches = close_number(actual, expected_value)
        else:
            matches = actual == expected_value
        if not matches:
            raise AnalysisFailure(f"{entry_id}: latency gate {key} mismatch")


def manifest_route_list(manifest: dict[str, Any], key: str) -> list[str]:
    routes = manifest.get(key)
    if not isinstance(routes, list) or not all(
        isinstance(route, str) and route.startswith("/") for route in routes
    ):
        raise AnalysisFailure(f"manifest {key} is not a route array")
    return routes


def is_stored_compressed_route(path: Any, manifest: dict[str, Any]) -> bool:
    if not isinstance(path, str):
        return False
    tiles = manifest_route_list(manifest, "tiles")
    textures = manifest_route_list(manifest, "textures")
    return path in textures or (path in tiles and "/tiles/0/" in path)


def stored_compression_proof_applicable(
    entry: dict[str, Any], manifest: dict[str, Any]
) -> bool:
    if entry.get("contractMode") != "enhanced":
        return False
    profile = entry.get("profile")
    if profile in {"hot-tile", "conditional"}:
        return is_stored_compressed_route(manifest.get("hotTile"), manifest)
    if profile == "random-tiles":
        return any(
            is_stored_compressed_route(path, manifest)
            for path in manifest_route_list(manifest, "tiles")
        )
    if profile == "large-tile":
        return is_stored_compressed_route(manifest.get("largeTile"), manifest)
    if profile == "textures":
        return any(
            is_stored_compressed_route(path, manifest)
            for path in manifest_route_list(manifest, "textures")
        )
    if profile == "large-object":
        return is_stored_compressed_route(manifest.get("largeObject"), manifest)
    if profile in {"map-data-mixed", "browser-mixed"}:
        return any(
            is_stored_compressed_route(path, manifest)
            for key in ("tiles", "textures")
            for path in manifest_route_list(manifest, key)
        )
    return False


def direct_transport_metrics(
    entry: dict[str, Any], traffic_mode: str, manifest: dict[str, Any]
) -> tuple[str, ...]:
    if traffic_mode != "ssh-l4-traefik":
        return ()
    metrics = ["bluemap_prohibited_edge_header"]
    if stored_compression_proof_applicable(entry, manifest):
        metrics.append("bluemap_stored_content_encoding_violation")
    return tuple(metrics)


def rate_metric_proof(
    summary: dict[str, Any] | None,
    metric: str,
    *,
    applicable: bool,
) -> dict[str, Any]:
    if not applicable:
        return {
            "applicable": False,
            "samples": None,
            "passes": None,
            "fails": None,
            "violationRate": None,
            "passed": None,
        }
    rate = metric_value(summary, metric, "rate")
    passes = metric_value(summary, metric, "passes")
    fails = metric_value(summary, metric, "fails")
    samples = passes + fails if passes is not None and fails is not None else None
    consistent = bool(
        samples is not None
        and samples > 0
        and rate is not None
        and rate <= 1
        and close_number(rate, passes / samples)
    )
    return {
        "applicable": True,
        "samples": samples,
        "passes": passes,
        "fails": fails,
        "violationRate": rate,
        "passed": bool(consistent and rate == 0),
    }


def transport_phase_proof(
    summary: dict[str, Any] | None,
    entry: dict[str, Any],
    traffic_mode: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    direct = traffic_mode == "ssh-l4-traefik"
    stored_applicable = bool(
        direct and stored_compression_proof_applicable(entry, manifest)
    )
    edge = rate_metric_proof(
        summary,
        "bluemap_prohibited_edge_header",
        applicable=direct,
    )
    stored = rate_metric_proof(
        summary,
        "bluemap_stored_content_encoding_violation",
        applicable=stored_applicable,
    )
    return {
        "applicable": direct,
        "passed": (
            bool(
                edge["passed"] is True
                and (
                    not stored_applicable
                    or stored["passed"] is True
                )
            )
            if direct
            else None
        ),
        "prohibitedEdgeHeaders": edge,
        "storedContentEncoding": stored,
    }


def require_transport_phase_proof(
    proof: dict[str, Any], entry_id: str, phase: str
) -> None:
    if proof["applicable"] is not True:
        return
    failures = [
        name
        for name in ("prohibitedEdgeHeaders", "storedContentEncoding")
        if proof[name]["applicable"] is True and proof[name]["passed"] is not True
    ]
    if failures:
        raise AnalysisFailure(
            f"{entry_id}: {phase} direct transport proof failed: "
            + ", ".join(failures)
        )


def validate_required_measurement_metrics(
    summary: dict[str, Any],
    entry: dict[str, Any],
    traffic_mode: str,
    manifest: dict[str, Any],
) -> None:
    entry_id = entry["entryId"]
    required = {
        "http_req_failed{traffic:workload}": ("rate",),
        "bluemap_unexpected_status": ("rate",),
        "iterations": ("count", "rate"),
        "dropped_iterations": ("count",),
        "http_reqs": ("count",),
        "data_received": ("count",),
        "data_sent": ("count",),
        "http_req_duration{traffic:workload}": (
            "med",
            "p(90)",
            "p(95)",
            "p(99)",
        ),
        "bluemap_ttfb": ("med", "p(90)", "p(95)", "p(99)"),
    }
    for metric_name, fields in required.items():
        for field in fields:
            require_summary_metric(summary, metric_name, field, entry_id)
    require_transport_phase_proof(
        transport_phase_proof(summary, entry, traffic_mode, manifest),
        entry_id,
        "measurement",
    )


def measurement_metrics_available(
    summary: dict[str, Any] | None,
    entry: dict[str, Any],
    traffic_mode: str,
    manifest: dict[str, Any],
) -> bool:
    if summary is None:
        return False
    try:
        validate_required_measurement_metrics(
            summary, entry, traffic_mode, manifest
        )
    except AnalysisFailure:
        return False
    return True


def validate_status_metrics(
    summary: dict[str, Any],
    entry: dict[str, Any],
    phase: str,
    traffic_mode: str,
    manifest: dict[str, Any],
) -> None:
    entry_id = entry["entryId"]
    unexpected = require_summary_metric(
        summary, "bluemap_unexpected_status", "rate", entry_id
    )
    failures = require_summary_metric(
        summary, "http_req_failed{traffic:workload}", "rate", entry_id
    )
    if unexpected != 0 or failures >= 0.001:
        raise AnalysisFailure(
            f"{entry_id}: {phase} status/failure metrics violate the gate"
        )
    require_transport_phase_proof(
        transport_phase_proof(summary, entry, traffic_mode, manifest),
        entry_id,
        phase,
    )


def parse_phase_windows(path: Path) -> dict[str, tuple[float, float]]:
    if not path.is_file():
        return {}
    events: defaultdict[tuple[int, str], dict[str, float]] = defaultdict(dict)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AnalysisFailure(f"cannot read phase events {path}: {error}") from error
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise AnalysisFailure(f"{path}:{number}: invalid JSON: {error}") from error
        repetition = event.get("repetition")
        phase = event.get("phase")
        event_name = event.get("event")
        valid_phase = (
            repetition == 0
            and phase == "case"
            or repetition == 1
            and phase in {"correctness", "warmup", "measurement", "cooldown"}
        )
        if (
            not valid_phase
            or event_name not in {"start", "end", "failed"}
            or not isinstance(event.get("timestamp"), str)
        ):
            raise AnalysisFailure(f"{path}:{number}: malformed phase event")
        key = (repetition, phase)
        if event_name in events[key]:
            raise AnalysisFailure(f"{path}: duplicate {phase} {event_name} event")
        events[key][event_name] = timestamp_epoch(
            event["timestamp"], f"{path}:{number} timestamp"
        )
    windows: dict[str, tuple[float, float]] = {}
    for (repetition, phase), pair in events.items():
        if "start" not in pair:
            raise AnalysisFailure(f"{path}: incomplete {phase} phase window")
        if "end" in pair and "failed" in pair:
            raise AnalysisFailure(f"{path}: {phase} has both end and failed events")
        terminal = "end" if "end" in pair else ("failed" if "failed" in pair else None)
        if terminal is None or pair[terminal] < pair["start"]:
            raise AnalysisFailure(f"{path}: incomplete {phase} phase window")
        key = phase if repetition == 1 else "case"
        windows[key] = (pair["start"], pair[terminal])
        if terminal == "failed":
            windows[f"{key}Failed"] = windows[key]
    return windows


def validate_case_timing(
    result: dict[str, Any],
    windows: dict[str, tuple[float, float]],
    entry: dict[str, Any],
) -> dict[str, Any]:
    entry_id = entry["entryId"]
    started = timestamp_epoch(result.get("startedAt"), f"{entry_id}: result startedAt")
    ended = timestamp_epoch(result.get("endedAt"), f"{entry_id}: result endedAt")
    completed = timestamp_epoch(
        result.get("completedAt"), f"{entry_id}: result completedAt"
    )
    if not started < ended <= completed:
        raise AnalysisFailure(f"{entry_id}: result timestamps are not ordered")
    result_range = result["range"]
    if (
        abs(float(result_range["startEpoch"]) - started) > 1.1
        or abs(float(result_range["endEpoch"]) - ended) > 1.1
    ):
        raise AnalysisFailure(f"{entry_id}: result epoch and timestamp ranges differ")
    case_window = windows.get("case")
    if (
        case_window is None
        or case_window[0] + 1 < started
        or case_window[0] > started + 2
        or case_window[1] + 1 < ended
        or case_window[1] > completed + 2
    ):
        raise AnalysisFailure(f"{entry_id}: case phase and result ranges differ")

    ordered_names = [
        name
        for name in ("correctness", "warmup", "measurement", "cooldown")
        if name in windows
    ]
    previous = case_window[0]
    for name in ordered_names:
        start, end = windows[name]
        if start < previous or end < start or end > case_window[1]:
            raise AnalysisFailure(f"{entry_id}: phase {name} is out of order")
        previous = end
    for name, configured in (
        ("warmup", entry["warmupDuration"]),
        ("measurement", entry["measurementDuration"]),
    ):
        window = windows.get(name)
        if (
            window is not None
            and window[1] - window[0]
            < duration_seconds(configured, f"{entry_id}: {name} duration") - 1
        ):
            raise AnalysisFailure(f"{entry_id}: {name} phase is shorter than configured")
    cooldown = windows.get("cooldown")
    if (
        cooldown is not None
        and cooldown[1] - cooldown[0] < float(entry["cooldownSeconds"]) - 0.1
    ):
        raise AnalysisFailure(f"{entry_id}: runner cooldown is shorter than configured")
    return {
        "resultStartedEpoch": started,
        "resultEndedEpoch": ended,
        "resultCompletedEpoch": completed,
        "caseWindow": list(case_window),
        "phaseWindows": {
            name: list(window)
            for name, window in windows.items()
            if not name.endswith("Failed")
        },
        "runnerCooldownSatisfied": cooldown is not None
        and cooldown[1] - cooldown[0] >= float(entry["cooldownSeconds"]) - 0.1,
    }


def parse_cpu(value: Any) -> float:
    if not isinstance(value, str):
        raise AnalysisFailure("Kubernetes CPU quantity is not a string")
    suffixes = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
    suffix = value[-1:] if value else ""
    factor = suffixes.get(suffix, 1.0)
    number = value[:-1] if suffix in suffixes else value
    try:
        result = float(number) * factor
    except ValueError as error:
        raise AnalysisFailure(f"invalid Kubernetes CPU quantity {value!r}") from error
    if not math.isfinite(result) or result < 0:
        raise AnalysisFailure(f"invalid Kubernetes CPU quantity {value!r}")
    return result


def parse_memory(value: Any) -> float:
    if not isinstance(value, str):
        raise AnalysisFailure("Kubernetes memory quantity is not a string")
    suffixes = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "Pi": 1024**5,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
        "P": 1000**5,
    }
    factor = 1
    number = value
    for suffix, candidate in suffixes.items():
        if value.endswith(suffix):
            factor = candidate
            number = value[: -len(suffix)]
            break
    try:
        result = float(number) * factor
    except ValueError as error:
        raise AnalysisFailure(
            f"invalid Kubernetes memory quantity {value!r}"
        ) from error
    if not math.isfinite(result) or result < 0:
        raise AnalysisFailure(f"invalid Kubernetes memory quantity {value!r}")
    return result


def percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def describe(values: Iterable[float | int | None]) -> dict[str, Any] | None:
    clean = [
        float(value)
        for value in values
        if value is not None
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ]
    if not clean:
        return None
    median = statistics.median(clean)
    q1 = percentile(clean, 0.25)
    q3 = percentile(clean, 0.75)
    return {
        "n": len(clean),
        "minimum": min(clean),
        "q1": q1,
        "median": median,
        "q3": q3,
        "maximum": max(clean),
        "iqr": q3 - q1,
        "mad": statistics.median(abs(value - median) for value in clean),
        "p95": percentile(clean, 0.95),
    }


def summarize_resource_samples(
    path: Path,
    phase: str,
    workload: dict[str, Any],
    measurement_window: tuple[float, float] | None = None,
) -> dict[str, dict[str, Any]]:
    configured_seconds = duration_seconds(
        workload["workload"]["measurement"],
        "workload measurement duration",
    )
    interval_seconds = finite_number(
        workload["workload"]["metricsIntervalSeconds"],
        "metrics interval",
        minimum=0,
    )
    if interval_seconds <= 0:
        raise AnalysisFailure("metrics interval must be positive")
    by_role_time: defaultdict[str, defaultdict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"cpu": 0.0, "memory": 0.0})
    )
    seen: set[tuple[str, str, str]] = set()
    seen_metric_samples: set[tuple[str, str, float]] = set()
    pods_by_role_time: defaultdict[str, defaultdict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    containers_by_role_time_pod: defaultdict[
        str, defaultdict[str, dict[str, set[str]]]
    ] = defaultdict(lambda: defaultdict(dict))
    windows_by_role: defaultdict[str, list[float]] = defaultdict(list)
    captured_epochs: dict[str, float] = {}
    rejected_freshness: defaultdict[str, int] = defaultdict(int)
    excluded_outside_measurement: defaultdict[str, int] = defaultdict(int)
    roles_seen: set[str] = set()
    targets = workload["targets"]
    expected_pods = {
        "web": set(targets["webPods"]),
        "database": set(targets.get("databasePods", [])),
    }
    formal_entry = workload.get("formalSchedule", {}).get("entry")
    if not isinstance(formal_entry, dict):
        raise AnalysisFailure("workload formal schedule entry is missing")
    expected_web_containers = {
        image["name"]
        for image in formal_entry.get("expectedImages", [])
        if isinstance(image, dict) and image.get("kind") == "container"
    }
    if not expected_web_containers:
        raise AnalysisFailure("frozen web container identity is empty")
    observed_pods: defaultdict[str, set[str]] = defaultdict(set)
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AnalysisFailure(f"cannot read resource samples {path}: {error}") from error
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError as error:
            raise AnalysisFailure(f"{path}:{number}: invalid JSON") from error
        if sample.get("phase") != phase:
            continue
        role = sample.get("role")
        captured = sample.get("capturedAt")
        pod = sample.get("pod")
        containers = sample.get("containers")
        if (
            role not in {"web", "database"}
            or not isinstance(captured, str)
            or not isinstance(pod, str)
            or not isinstance(containers, list)
        ):
            raise AnalysisFailure(f"{path}:{number}: malformed resource sample")
        roles_seen.add(role)
        captured_epoch = timestamp_epoch(
            captured,
            f"{path}:{number} capturedAt",
        )
        metric_epoch = timestamp_epoch(
            sample.get("metricTimestamp"),
            f"{path}:{number} metricTimestamp",
        )
        window = sample.get("window")
        if (
            not isinstance(window, str)
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s", window) is None
        ):
            raise AnalysisFailure(f"{path}:{number}: invalid metrics window")
        if float(window[:-1]) <= 0:
            raise AnalysisFailure(f"{path}:{number}: nonpositive metrics window")
        window_seconds = float(window[:-1])
        if sample.get("expectedPod") != pod:
            raise AnalysisFailure(f"{path}:{number}: metrics Pod identity differs")
        if role in expected_pods and pod not in expected_pods[role]:
            raise AnalysisFailure(
                f"{path}:{number}: unexpected {role} metrics Pod {pod!r}"
            )
        if not containers:
            raise AnalysisFailure(f"{path}:{number}: empty container metrics")
        container_names: set[str] = set()
        sample_cpu = 0.0
        sample_memory = 0.0
        for container in containers:
            if not isinstance(container, dict):
                raise AnalysisFailure(f"{path}:{number}: malformed container sample")
            name = container.get("name")
            if not isinstance(name, str) or not name or name in container_names:
                raise AnalysisFailure(
                    f"{path}:{number}: invalid or duplicate container metric"
                )
            container_names.add(name)
            sample_cpu += parse_cpu(container.get("cpu"))
            sample_memory += parse_memory(container.get("memory"))

        maximum_age = max(30.0, interval_seconds * 4, window_seconds * 2)
        age = captured_epoch - metric_epoch
        if age < -2 or age > maximum_age:
            rejected_freshness[role] += 1
            continue
        if measurement_window is not None and (
            metric_epoch < measurement_window[0]
            or metric_epoch > measurement_window[1] + 2
        ):
            # A fresh sample may still describe the pre-measurement scrape.
            # Exclude it instead of counting it as measurement telemetry.
            excluded_outside_measurement[role] += 1
            continue
        metric_identity = (role, pod, metric_epoch)
        if metric_identity in seen_metric_samples:
            # metrics-server commonly repeats one scrape across several polls.
            continue
        seen_metric_samples.add(metric_identity)
        windows_by_role[role].append(window_seconds)
        captured_epochs[captured] = captured_epoch
        key = (role, captured, pod)
        if key in seen:
            raise AnalysisFailure(f"{path}:{number}: duplicate resource sample")
        seen.add(key)
        observed_pods[role].add(pod)
        pods_by_role_time[role][captured].add(pod)
        by_role_time[role][captured]["cpu"] += sample_cpu
        by_role_time[role][captured]["memory"] += sample_memory
        containers_by_role_time_pod[role][captured][pod] = container_names
    result: dict[str, dict[str, Any]] = {}
    for role in sorted(roles_seen | set(expected_pods)):
        timestamps = by_role_time[role]
        effective_interval = max(
            interval_seconds,
            max(windows_by_role[role], default=interval_seconds),
        )
        minimum_timestamps = max(
            2, math.floor(configured_seconds / (effective_interval * 2))
        )
        complete_timestamps = {
            captured: value
            for captured, value in timestamps.items()
            if (
                role not in expected_pods
                or pods_by_role_time[role][captured] == expected_pods[role]
            )
        }
        if role == "web":
            complete_timestamps = {
                captured: value
                for captured, value in complete_timestamps.items()
                if all(
                    containers_by_role_time_pod[role][captured].get(pod)
                    == expected_web_containers
                    for pod in expected_pods["web"]
                )
            }
        cpu = describe(value["cpu"] for value in complete_timestamps.values())
        memory = describe(value["memory"] for value in complete_timestamps.values())
        epochs = sorted(captured_epochs[captured] for captured in complete_timestamps)
        time_coverage = len(epochs) >= minimum_timestamps
        if measurement_window is not None and epochs:
            allowance = interval_seconds * 2
            expected_end = measurement_window[0] + configured_seconds
            time_coverage = time_coverage and (
                all(
                    measurement_window[0] - allowance
                    <= epoch
                    <= measurement_window[1] + allowance
                    for epoch in epochs
                )
                and epochs[0] <= measurement_window[0] + allowance
                and epochs[-1] >= expected_end - allowance
            )
        complete_target_coverage = (
            time_coverage
            and observed_pods[role] == expected_pods[role]
            if role in expected_pods
            else None
        )
        complete_container_coverage = (
            all(
                containers_by_role_time_pod[role][captured].get(pod)
                == expected_web_containers
                for captured in timestamps
                for pod in expected_pods["web"]
            )
            if role == "web"
            else (True if role == "database" else None)
        )
        if role == "web":
            complete_target_coverage = (
                complete_target_coverage and complete_container_coverage
            )
        freshness_passed = rejected_freshness[role] == 0
        if role in expected_pods:
            complete_target_coverage = (
                complete_target_coverage and freshness_passed
            )
        result[role] = {
            "available": bool(timestamps),
            "timestamps": len(complete_timestamps),
            "capturedTimestamps": len(timestamps),
            "minimumRequiredTimestamps": minimum_timestamps,
            "timeCoverage": time_coverage,
            "pods": sorted(observed_pods[role]),
            "completeTargetCoverage": complete_target_coverage,
            "expectedContainers": (
                sorted(expected_web_containers) if role == "web" else None
            ),
            "completeContainerCoverage": complete_container_coverage,
            "freshnessPassed": freshness_passed,
            "rejectedStaleOrFutureSamples": rejected_freshness[role],
            "excludedOutsideMeasurementSamples": (
                excluded_outside_measurement[role]
            ),
            "cpuCores": cpu,
            "memoryBytes": memory,
        }
    return result


def prometheus_series_values(
    query: dict[str, Any],
    *,
    pods: set[str],
    allowed_pods: set[str],
    window: tuple[float, float] | None,
    aggregation: str = "sum",
    minimum_timestamps: int = 1,
    expected_containers: set[str] | None = None,
) -> dict[str, Any]:
    response = query.get("response")
    if (
        not isinstance(response, dict)
        or response.get("status") != "success"
        or response.get("data", {}).get("resultType") != "matrix"
    ):
        raise AnalysisFailure(
            f"Prometheus query {query.get('name')} did not capture a matrix success"
        )
    result = (
        response.get("data", {}).get("result")
        if isinstance(response, dict)
        else None
    )
    if not isinstance(result, list):
        raise AnalysisFailure(f"Prometheus query {query.get('name')} is malformed")
    if aggregation not in {"sum", "maximum"}:
        raise AnalysisFailure(f"unsupported Prometheus aggregation {aggregation!r}")
    by_timestamp: defaultdict[float, list[float]] = defaultdict(list)
    pods_by_timestamp: defaultdict[float, set[str]] = defaultdict(set)
    containers_by_timestamp_pod: defaultdict[
        float, defaultdict[str, set[str]]
    ] = defaultdict(lambda: defaultdict(set))
    observed_pods: set[str] = set()
    observed_containers: defaultdict[str, set[str]] = defaultdict(set)
    for series in result:
        if not isinstance(series, dict):
            raise AnalysisFailure(f"Prometheus query {query.get('name')} has bad series")
        metric = series.get("metric")
        values = series.get("values")
        if not isinstance(metric, dict) or not isinstance(values, list):
            raise AnalysisFailure(f"Prometheus query {query.get('name')} has bad series")
        pod = metric.get("pod")
        if not isinstance(pod, str) or pod not in allowed_pods:
            raise AnalysisFailure(
                f"Prometheus query {query.get('name')} has an unexpected Pod label"
            )
        if pods and pod not in pods:
            continue
        container_name: str | None = None
        if expected_containers is not None:
            container_name = metric.get("container")
            if not isinstance(container_name, str):
                raise AnalysisFailure(
                    f"Prometheus query {query.get('name')} has no container label"
                )
            # Stopped init-container series are irrelevant to steady-state web
            # resource use and are intentionally ignored.
            if container_name not in expected_containers:
                continue
        for pair in values:
            if not isinstance(pair, list) or len(pair) != 2:
                raise AnalysisFailure(
                    f"Prometheus query {query.get('name')} has malformed sample"
                )
            try:
                timestamp = float(pair[0])
                value = float(pair[1])
            except (TypeError, ValueError) as error:
                raise AnalysisFailure(
                    f"Prometheus query {query.get('name')} has nonnumeric sample"
                ) from error
            if not math.isfinite(timestamp) or not math.isfinite(value) or value < 0:
                continue
            if window is None or window[0] <= timestamp <= window[1]:
                by_timestamp[timestamp].append(value)
                pods_by_timestamp[timestamp].add(pod)
                observed_pods.add(pod)
                if container_name is not None:
                    containers_by_timestamp_pod[timestamp][pod].add(
                        container_name
                    )
                    observed_containers[pod].add(container_name)
    complete_container_coverage = (
        all(
            containers_by_timestamp_pod[timestamp].get(pod)
            == expected_containers
            for timestamp in by_timestamp
            for pod in pods
        )
        if expected_containers is not None
        else True
    )
    complete_pod_coverage = (
        len(by_timestamp) >= minimum_timestamps
        and observed_pods == pods
        and all(observed == pods for observed in pods_by_timestamp.values())
    )
    complete = complete_pod_coverage and complete_container_coverage
    if aggregation == "maximum":
        values = [max(values) for values in by_timestamp.values() if values]
    else:
        values = [sum(values) for values in by_timestamp.values()]
    return {
        "values": values,
        "timestamps": len(by_timestamp),
        "minimumRequiredTimestamps": minimum_timestamps,
        "observedPods": sorted(observed_pods),
        "expectedPods": sorted(pods),
        "completePodCoverage": complete_pod_coverage,
        "expectedContainers": (
            sorted(expected_containers)
            if expected_containers is not None
            else None
        ),
        "observedContainers": {
            pod: sorted(containers) for pod, containers in observed_containers.items()
        },
        "completeContainerCoverage": complete_container_coverage,
        "completeCoverage": complete,
    }


_CAPTURE_HELPER: Any = None


def capture_helper(expected_sha256: str) -> Any:
    global _CAPTURE_HELPER
    validate_digest(expected_sha256, "archived Prometheus helper")
    path = Path(__file__).resolve().parents[2] / "tools" / "capture_prometheus.py"
    if not path.is_file() or path.is_symlink():
        raise AnalysisFailure(f"committed Prometheus helper is missing: {path}")
    if sha256_file(path) != expected_sha256:
        raise AnalysisFailure(
            "active Prometheus helper differs from the runner-archived helper"
        )
    if _CAPTURE_HELPER is not None:
        return _CAPTURE_HELPER
    specification = importlib.util.spec_from_file_location(
        "_bluemap_formal_capture_prometheus", path
    )
    if specification is None or specification.loader is None:
        raise AnalysisFailure(f"cannot load committed Prometheus helper: {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    _CAPTURE_HELPER = module
    return module


def summarize_prometheus(
    path: Path,
    workload: dict[str, Any],
    window: tuple[float, float] | None,
    result_range: dict[str, Any],
    capture_helper_sha256: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    observability = workload.get("observability", {}).get("prometheus", {})
    enabled = isinstance(observability, dict) and observability.get("enabled") is True
    if not enabled:
        return (
            {"enabled": False, "available": False},
            {"enabled": False, "available": False, "metrics": {}},
            {"enabled": False, "available": False, "passed": None},
        )
    bundle = load_optional_object(path)
    if bundle is None:
        return (
            {"enabled": True, "available": False},
            {"enabled": True, "available": False, "metrics": {}},
            {"enabled": True, "available": False, "passed": False},
        )
    if window is None:
        raise AnalysisFailure(f"{path}: measurement window is missing")
    if capture_helper_sha256 is None:
        raise AnalysisFailure(
            f"{path}: runner checksum omits the Prometheus capture helper"
        )
    helper = capture_helper(capture_helper_sha256)
    if bundle.get("namespace") != workload.get("namespace"):
        raise AnalysisFailure(f"{path}: namespace identity mismatch")
    expected_targets = {
        *(
            ("web", pod)
            for pod in workload["targets"]["webPods"]
        ),
        *(
            ("database", pod)
            for pod in workload["targets"].get("databasePods", [])
        ),
    }
    raw_targets = bundle.get("targets")
    if not isinstance(raw_targets, list):
        raise AnalysisFailure(f"{path}: target identities are missing")
    actual_targets = {
        (item.get("role"), item.get("pod"))
        for item in raw_targets
        if isinstance(item, dict)
    }
    if len(actual_targets) != len(raw_targets) or actual_targets != expected_targets:
        raise AnalysisFailure(f"{path}: Prometheus target identities mismatch")
    if bundle.get("nodes") != workload["targets"].get("nodes"):
        raise AnalysisFailure(f"{path}: Prometheus node identities mismatch")
    capture_range = bundle.get("range")
    expected_step = finite_number(
        observability.get("stepSeconds"),
        "Prometheus stepSeconds",
        minimum=1,
    )
    if (
        not isinstance(capture_range, dict)
        or capture_range.get("start") != result_range.get("startEpoch")
        or capture_range.get("end") != result_range.get("endEpoch")
        or capture_range.get("stepSeconds") != expected_step
    ):
        raise AnalysisFailure(f"{path}: Prometheus capture range mismatch")
    prometheus_identity = bundle.get("prometheus")
    try:
        expected_prometheus_url = helper.inspect_url(
            observability.get("baseUrl")
        )["baseUrl"]
    except (TypeError, ValueError) as error:
        raise AnalysisFailure(
            f"{path}: workload Prometheus URL is invalid"
        ) from error
    if (
        not isinstance(prometheus_identity, dict)
        or prometheus_identity.get("baseUrl") != expected_prometheus_url
    ):
        raise AnalysisFailure(f"{path}: Prometheus source URL mismatch")
    queries_raw = bundle.get("queries")
    if not isinstance(queries_raw, list):
        raise AnalysisFailure(f"{path}: queries must be an array")
    queries: dict[str, dict[str, Any]] = {}
    for query in queries_raw:
        if not isinstance(query, dict) or not isinstance(query.get("name"), str):
            raise AnalysisFailure(f"{path}: malformed query entry")
        response = query.get("response")
        if (
            not isinstance(response, dict)
            or response.get("status") != "success"
            or response.get("data", {}).get("resultType") != "matrix"
            or not isinstance(response.get("data", {}).get("result"), list)
        ):
            raise AnalysisFailure(
                f"{path}: query {query.get('name')} has no successful matrix response"
            )
        if query["name"] in queries:
            raise AnalysisFailure(f"{path}: duplicate query {query['name']}")
        queries[query["name"]] = query
    expected_queries = helper.build_queries(
        workload["namespace"],
        [
            {"role": role, "pod": pod}
            for role, pod in (
                [("web", pod) for pod in workload["targets"]["webPods"]]
                + [
                    ("database", pod)
                    for pod in workload["targets"].get("databasePods", [])
                ]
            )
        ],
        workload["targets"]["nodes"],
    )
    actual_query_definitions = [
        {
            "name": query.get("name"),
            "scope": query.get("scope"),
            "query": query.get("query"),
        }
        for query in queries_raw
    ]
    if actual_query_definitions != expected_queries:
        raise AnalysisFailure(f"{path}: Prometheus query definitions mismatch")
    targets = workload["targets"]
    web_pods = set(targets["webPods"])
    database_pods = set(targets.get("databasePods", []))
    formal_entry = workload.get("formalSchedule", {}).get("entry")
    if not isinstance(formal_entry, dict):
        raise AnalysisFailure(f"{path}: frozen schedule entry is missing")
    expected_web_containers = {
        image["name"]
        for image in formal_entry.get("expectedImages", [])
        if isinstance(image, dict) and image.get("kind") == "container"
    }
    if not expected_web_containers:
        raise AnalysisFailure(f"{path}: frozen web container identity is empty")
    all_target_pods = {
        *web_pods,
        *database_pods,
    }

    names = {
        "cpuCores": ("container_cpu_cores", "sum"),
        "memoryBytes": ("container_memory_working_set_bytes", "sum"),
        "throttledSecondsRate": (
            "container_cpu_throttled_seconds_rate",
            "sum",
        ),
        # A sum of per-container ratios would mechanically penalize replicas.
        # Report the worst container at each timestamp instead.
        "throttledPeriodRatio": (
            "container_cpu_throttled_period_ratio",
            "maximum",
        ),
    }
    web: dict[str, Any] = {"enabled": True, "available": True}
    minimum_prometheus_timestamps = max(
        2,
        math.floor((window[1] - window[0]) / float(expected_step)) - 1,
    )
    for label, (name, aggregation) in names.items():
        query = queries.get(name)
        coverage = (
            prometheus_series_values(
                query,
                pods=web_pods,
                allowed_pods=all_target_pods,
                window=window,
                aggregation=aggregation,
                minimum_timestamps=minimum_prometheus_timestamps,
                expected_containers=expected_web_containers,
            )
            if query is not None
            else {
                "values": [],
                "timestamps": 0,
                "minimumRequiredTimestamps": minimum_prometheus_timestamps,
                "observedPods": [],
                "expectedPods": sorted(web_pods),
                "completePodCoverage": False,
                "expectedContainers": sorted(expected_web_containers),
                "observedContainers": {},
                "completeContainerCoverage": False,
                "completeCoverage": False,
            }
        )
        web[label] = (
            describe(coverage["values"])
            if coverage["completeCoverage"]
            else None
        )
        web[f"{label}Coverage"] = {
            key: value for key, value in coverage.items() if key != "values"
        }
        web[f"{label}Aggregation"] = aggregation
    web["available"] = any(web[label] is not None for label in names)

    database_metrics: dict[str, Any] = {}
    for name, query in queries.items():
        if not (
            name.startswith("postgres_")
            or name.startswith("mariadb_")
            or name.startswith("mysql_")
        ):
            continue
        coverage = prometheus_series_values(
            query,
            pods=database_pods,
            allowed_pods=all_target_pods,
            window=window,
            minimum_timestamps=minimum_prometheus_timestamps,
        )
        values = coverage["values"] if coverage["completeCoverage"] else []
        database_metrics[name] = {
            "available": bool(values) and coverage["completeCoverage"],
            "samples": len(values),
            "values": describe(values),
            "coverage": {
                key: value for key, value in coverage.items() if key != "values"
            },
        }
    database = {
        "enabled": True,
        "available": any(
            item["available"] for item in database_metrics.values()
        ),
        "metrics": database_metrics,
    }
    node_noise = bundle.get("nodeNoise")
    if not isinstance(node_noise, dict):
        raise AnalysisFailure(f"{path}: nodeNoise result is missing")
    noise_repetitions = node_noise.get("repetitions")
    if (
        not isinstance(noise_repetitions, list)
        or len(noise_repetitions) != 1
        or not isinstance(noise_repetitions[0], dict)
        or noise_repetitions[0].get("repetition") != 1
    ):
        raise AnalysisFailure(f"{path}: nodeNoise repetition identity mismatch")
    thresholds = {
        "maximum_range_cores": observability.get(
            "maximumNonTargetNodeCpuRangeCores"
        ),
        "maximum_mean_cores": observability.get(
            "maximumNonTargetNodeCpuMeanCores"
        ),
        "maximum_level_cores": observability.get(
            "maximumNonTargetNodeCpuLevelCores"
        ),
    }
    try:
        expected_node_noise = helper.assess_node_noise(
            queries_raw,
            workload["targets"]["nodes"],
            [
                {
                    "repetition": 1,
                    "start": window[0],
                    "end": window[1],
                }
            ],
            **thresholds,
        )
    except (TypeError, ValueError) as error:
        raise AnalysisFailure(f"{path}: cannot recompute node noise") from error
    if node_noise != expected_node_noise:
        raise AnalysisFailure(f"{path}: nodeNoise result does not recompute")
    noise = {
        "enabled": True,
        "available": True,
        "passed": node_noise.get("passed") is True,
        "metric": node_noise.get("metric"),
        "noisyRepetitions": node_noise.get("noisyRepetitions"),
        "repetitions": node_noise.get("repetitions"),
        "thresholds": {
            "maximumRangeCores": node_noise.get("maximumRangeCores"),
            "maximumMeanCores": node_noise.get("maximumMeanCores"),
            "maximumLevelCores": node_noise.get("maximumLevelCores"),
        },
        "raw": node_noise,
    }
    return web, database, noise


def read_exit_status(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
        return int(value)
    except (OSError, ValueError):
        return None


def is_finished_phase(current_phase: str | None, completed: int) -> bool:
    return current_phase == f"repetition-{completed:02d}/finished"


def empty_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size == 0


def preferred_resource(
    prometheus: dict[str, Any], kubernetes: dict[str, Any] | None
) -> dict[str, Any]:
    kubernetes = kubernetes if isinstance(kubernetes, dict) else {}
    cpu_prometheus = prometheus.get("cpuCores")
    memory_prometheus = prometheus.get("memoryBytes")
    cpu_kubernetes = kubernetes.get("cpuCores")
    memory_kubernetes = kubernetes.get("memoryBytes")
    cpu_source = (
        "metrics.k8s.io"
        if cpu_kubernetes is not None
        else ("prometheus" if cpu_prometheus is not None else None)
    )
    memory_source = (
        "metrics.k8s.io"
        if memory_kubernetes is not None
        else ("prometheus" if memory_prometheus is not None else None)
    )
    sources = {source for source in (cpu_source, memory_source) if source}
    return {
        "source": next(iter(sources)) if len(sources) == 1 else (
            "mixed" if sources else None
        ),
        "cpuSource": cpu_source,
        "memorySource": memory_source,
        "cpuCores": cpu_kubernetes
        if cpu_kubernetes is not None
        else cpu_prometheus,
        "memoryBytes": memory_kubernetes
        if memory_kubernetes is not None
        else memory_prometheus,
    }


def validate_public_ingress(case_dir: Path, entry_id: str) -> dict[str, Any]:
    identities: dict[str, dict[str, Any]] = {}
    for label in ("before", "after"):
        snapshot = load_regular_object(
            case_dir
            / "cluster"
            / label
            / "ingress-bluemap-perf-public.json"
        )
        resource = snapshot.get("resource")
        metadata = resource.get("metadata") if isinstance(resource, dict) else None
        spec = resource.get("spec") if isinstance(resource, dict) else None
        expected_rules = [
            {
                "host": "bluemap-test.guenter.cloud",
                "http": {
                    "paths": [
                        {
                            "backend": {
                                "service": {
                                    "name": "bluemap-perf-public",
                                    "port": {"name": "http"},
                                }
                            },
                            "path": "/",
                            "pathType": "Prefix",
                        }
                    ]
                },
            }
        ]
        if (
            not isinstance(resource, dict)
            or resource.get("apiVersion") != "networking.k8s.io/v1"
            or resource.get("kind") != "Ingress"
            or not isinstance(metadata, dict)
            or metadata.get("name") != "bluemap-perf-public"
            or metadata.get("namespace") != "minecraft"
            or not isinstance(metadata.get("uid"), str)
            or not metadata["uid"]
            or metadata.get("labels", {}).get("app.kubernetes.io/part-of")
            != "bluemap-web-performance"
            or metadata.get("labels", {}).get(
                "bluemap.guenter.cloud/experiment-id"
            )
            != "runpod-public-route"
            or not isinstance(spec, dict)
            or spec.get("ingressClassName") != "traefik"
            or spec.get("defaultBackend") is not None
            or spec.get("tls", []) != []
            or spec.get("rules") != expected_rules
        ):
            raise AnalysisFailure(
                f"{entry_id}: public Ingress does not exactly bind the benchmark host"
            )
        identities[label] = {
            "uid": metadata["uid"],
            "specSha256": canonical_sha256(spec),
        }
    if identities["before"] != identities["after"]:
        raise AnalysisFailure(
            f"{entry_id}: public Ingress identity changed during the case"
        )
    return identities["before"]


def analyze_case(
    case_dir: Path,
    entry: dict[str, Any],
    state_item: dict[str, Any],
    execution_identity: dict[str, Any],
    matrix_path: Path,
    schedule_path: Path,
    matrix_digest: str,
    schedule_digest: str,
) -> dict[str, Any]:
    inputs = case_dir / "inputs"
    checksums = verify_sha256s(inputs)
    if checksums["matrix.json"] != matrix_digest:
        raise AnalysisFailure(f"{entry['entryId']}: copied matrix bytes differ")
    if checksums["schedule.json"] != schedule_digest:
        raise AnalysisFailure(f"{entry['entryId']}: copied schedule bytes differ")
    if (inputs / "matrix.json").read_bytes() != matrix_path.read_bytes():
        raise AnalysisFailure(f"{entry['entryId']}: copied matrix is not byte-identical")
    if (inputs / "schedule.json").read_bytes() != schedule_path.read_bytes():
        raise AnalysisFailure(
            f"{entry['entryId']}: copied schedule is not byte-identical"
        )
    if load_object(inputs / "schedule-entry.json") != entry:
        raise AnalysisFailure(f"{entry['entryId']}: copied schedule entry mismatch")
    manifest = load_object(inputs / "manifest.json")
    if checksums["manifest.json"] != entry["manifestSha256"]:
        raise AnalysisFailure(f"{entry['entryId']}: manifest identity mismatch")
    workload = load_object(inputs / "workload.json")
    compare_workload_identity(
        workload,
        entry,
        matrix_digest,
        schedule_digest,
        manifest,
        checksums,
    )
    traffic_mode = workload["traffic"]["mode"]
    public_ingress_identity = validate_public_ingress(
        case_dir, entry["entryId"]
    )
    if state_item.get("webPods") != workload["targets"]["webPods"]:
        raise AnalysisFailure(f"{entry['entryId']}: state/workload web Pods mismatch")
    result = load_object(case_dir / "result.json")
    if result.get("caseId") != entry["runnerCaseId"]:
        raise AnalysisFailure(f"{entry['entryId']}: result caseId mismatch")
    if result.get("result") != state_item["result"]:
        raise AnalysisFailure(f"{entry['entryId']}: result/state status mismatch")
    result_range = result.get("range")
    if (
        not isinstance(result_range, dict)
        or not isinstance(result_range.get("startEpoch"), int)
        or isinstance(result_range.get("startEpoch"), bool)
        or not isinstance(result_range.get("endEpoch"), int)
        or isinstance(result_range.get("endEpoch"), bool)
        or result_range["endEpoch"] <= result_range["startEpoch"]
    ):
        raise AnalysisFailure(f"{entry['entryId']}: invalid result time range")
    requested = result.get("requestedRepetitions")
    completed = result.get("completedRepetitions")
    if requested != 1 or not isinstance(completed, int) or completed not in {0, 1}:
        raise AnalysisFailure(f"{entry['entryId']}: invalid repetition accounting")
    if result["result"] == "passed" and completed != 1:
        raise AnalysisFailure(f"{entry['entryId']}: passed result is incomplete")

    runtime_passed, runtime_reason = validate_runtime_identity(
        case_dir, entry, workload
    )
    windows = parse_phase_windows(case_dir / "phases.ndjson")
    timing = validate_case_timing(result, windows, entry)
    repetition = case_dir / "repetitions" / "01"
    warmup = repetition / "warmup"
    measurement = repetition / "measurement"
    warmup_summary = load_optional_object(warmup / "summary.json")
    warmup_arrival_artifact = load_optional_object(warmup / "arrival-gate.json")
    arrival = load_optional_object(measurement / "arrival-gate.json")
    latency_artifact = load_optional_object(measurement / "latency-gate.json")
    warmup_arrival = (
        warmup_arrival_artifact.get("passed") is True
        if warmup_arrival_artifact is not None
        else None
    )
    measurement_arrival = (
        arrival.get("passed") is True if arrival is not None else None
    )
    latency = (
        latency_artifact.get("passed") is True
        if latency_artifact is not None
        else None
    )
    warmup_exit = read_exit_status(warmup / "exit-status.txt")
    measurement_exit = read_exit_status(measurement / "exit-status.txt")
    summary = load_optional_object(measurement / "summary.json")
    warmup_transport_proof = transport_phase_proof(
        warmup_summary, entry, traffic_mode, manifest
    )
    measurement_transport_proof = transport_phase_proof(
        summary, entry, traffic_mode, manifest
    )
    warmup_raw_present = (warmup / "raw.ndjson").is_file() and (
        warmup / "raw.ndjson"
    ).stat().st_size > 0
    raw_present = (measurement / "raw.ndjson").is_file() and (
        measurement / "raw.ndjson"
    ).stat().st_size > 0
    sampler_failed = (case_dir / ".sampler-failed").exists()
    endpoint_failed = (case_dir / ".endpoint-sample-failed").exists()
    failures_log_empty = empty_file(case_dir / "failures.log")
    resource_errors_empty = empty_file(
        case_dir / "samples" / "resource-errors.ndjson"
    )
    contract_log_present = (repetition / "contract.log").is_file() and (
        repetition / "contract.log"
    ).stat().st_size > 0
    try:
        current_phase = (case_dir / ".current-phase").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        current_phase = None
    restart_diffs = [
        case_dir / "cluster" / "restarts-case.diff",
        repetition / "restarts.diff",
        case_dir / "cluster" / "config-digests.diff",
        case_dir / "cluster" / "runtime-spec-digests.diff",
        case_dir / "cluster" / "traffic-service.diff",
        case_dir / "cluster" / "traffic-ingress.diff",
    ]
    stable_diffs = all(empty_file(path) for path in restart_diffs)

    warmup_arrival_valid = False
    measurement_arrival_valid = False
    latency_identity_valid = False
    if result["result"] == "passed":
        if warmup_summary is None or summary is None:
            raise AnalysisFailure(
                f"{entry['entryId']}: passed result lacks a k6 summary"
            )
        if warmup_arrival_artifact is None or arrival is None:
            raise AnalysisFailure(
                f"{entry['entryId']}: passed result lacks an arrival gate"
            )
        if latency_artifact is None:
            raise AnalysisFailure(
                f"{entry['entryId']}: passed result lacks a latency gate"
            )
        validate_arrival_identity(
            warmup_arrival_artifact,
            warmup_summary,
            entry=entry,
            manifest=manifest,
            duration=entry["warmupDuration"],
        )
        warmup_arrival_valid = True
        validate_status_metrics(
            warmup_summary, entry, "warmup", traffic_mode, manifest
        )
        validate_arrival_identity(
            arrival,
            summary,
            entry=entry,
            manifest=manifest,
            duration=entry["measurementDuration"],
        )
        measurement_arrival_valid = True
        validate_latency_identity(latency_artifact, summary, entry)
        latency_identity_valid = True
        validate_required_measurement_metrics(
            summary, entry, traffic_mode, manifest
        )
        validate_status_metrics(
            summary, entry, "measurement", traffic_mode, manifest
        )
    else:
        if (
            warmup_summary is not None
            and isinstance(warmup_arrival_artifact, dict)
            and isinstance(warmup_arrival_artifact.get("totals"), dict)
        ):
            validate_arrival_identity(
                warmup_arrival_artifact,
                warmup_summary,
                entry=entry,
                manifest=manifest,
                duration=entry["warmupDuration"],
            )
            warmup_arrival_valid = True
        if (
            summary is not None
            and isinstance(arrival, dict)
            and isinstance(arrival.get("totals"), dict)
        ):
            validate_arrival_identity(
                arrival,
                summary,
                entry=entry,
                manifest=manifest,
                duration=entry["measurementDuration"],
            )
            measurement_arrival_valid = True
        if (
            summary is not None
            and isinstance(latency_artifact, dict)
            and "observedP95Milliseconds" in latency_artifact
        ):
            validate_latency_identity(latency_artifact, summary, entry)
            latency_identity_valid = True

    resource = summarize_resource_samples(
        case_dir / "samples" / "resource-usage.ndjson",
        "repetition-01/measurement",
        workload,
        windows.get("measurement"),
    )
    prometheus_web, database_prometheus, node_noise = summarize_prometheus(
        case_dir / "samples" / "prometheus-query-range.json",
        workload,
        windows.get("measurement"),
        result_range,
        checksums.get("capture_prometheus.py"),
    )
    database_kubernetes = resource.get("database")
    database_metrics = {
        "kubernetesSampler": database_kubernetes
        or {"available": False, "timestamps": 0},
        "prometheus": database_prometheus,
    }
    preferred = preferred_resource(prometheus_web, resource.get("web"))
    control_identity = workload_control_identity(
        case_dir,
        workload,
        entry,
        execution_identity,
        timing,
        result["result"],
    )
    load_generator_capacity = control_identity["loadGeneratorCapacity"]

    gates = {
        "runnerResult": result["result"] == "passed",
        "completedRepetition": completed == 1,
        "runtimeIdentity": runtime_passed,
        "finishedPhase": is_finished_phase(current_phase, completed),
        "contractLog": contract_log_present,
        "warmupPhaseWindow": "warmup" in windows,
        "measurementPhaseWindow": "measurement" in windows,
        "warmupExit": warmup_exit == 0,
        "warmupArrival": warmup_arrival is True,
        "warmupSummary": warmup_summary is not None,
        "warmupRawOutput": warmup_raw_present,
        "measurementExit": measurement_exit == 0,
        "measurementArrival": measurement_arrival is True,
        "latency": latency is True,
        "measurementSummary": summary is not None,
        "measurementRawOutput": raw_present,
        "warmupTransportProof": (
            warmup_transport_proof["applicable"] is not True
            or warmup_transport_proof["passed"] is True
        ),
        "measurementTransportProof": (
            measurement_transport_proof["applicable"] is not True
            or measurement_transport_proof["passed"] is True
        ),
        "warmupLoadGeneratorCapacity": (
            load_generator_capacity.get("warmup", {}).get("passed") is True
        ),
        "measurementLoadGeneratorCapacity": (
            load_generator_capacity.get("measurement", {}).get("passed") is True
        ),
        "metricsSampler": not sampler_failed,
        "resourceErrorsEmpty": resource_errors_empty,
        "kubernetesWebMetrics": bool(
            resource.get("web", {}).get("available")
            and resource.get("web", {}).get("completeTargetCoverage")
        ),
        "kubernetesDatabaseMetrics": bool(
            resource.get("database", {}).get("available")
            and resource.get("database", {}).get("completeTargetCoverage")
        ),
        "endpointSampler": not endpoint_failed,
        "stableRuntime": stable_diffs,
        "failuresLogEmpty": failures_log_empty,
        "nodeNoise": (
            node_noise.get("passed") is True
            if node_noise.get("enabled")
            else True
        ),
    }
    failed_gates = [name for name, passed in gates.items() if not passed]
    if runtime_reason and "runtimeIdentity" in failed_gates:
        failed_gates.append(runtime_reason)
    analyzer_only_telemetry_gates = {
        "kubernetesWebMetrics",
        "kubernetesDatabaseMetrics",
    }
    runner_consistency_failures = [
        name for name in failed_gates if name not in analyzer_only_telemetry_gates
    ]
    if result["result"] == "passed" and runner_consistency_failures:
        raise AnalysisFailure(
            f"{entry['entryId']}: runner passed but analyzer gates failed: "
            + ", ".join(runner_consistency_failures)
        )

    metrics_complete = measurement_metrics_available(
        summary, entry, traffic_mode, manifest
    )
    http_evidence = bool(
        runtime_passed
        and gates["finishedPhase"]
        and contract_log_present
        and "warmup" in windows
        and "measurement" in windows
        and warmup_summary is not None
        and summary is not None
        and warmup_raw_present
        and raw_present
        and warmup_arrival_valid
        and measurement_arrival_valid
        and latency_identity_valid
        and gates["warmupTransportProof"]
        and gates["measurementTransportProof"]
        and gates["warmupLoadGeneratorCapacity"]
        and gates["measurementLoadGeneratorCapacity"]
        and metrics_complete
        and not endpoint_failed
        and stable_diffs
    )
    web_resource_evidence = bool(
        http_evidence
        and not sampler_failed
        and resource_errors_empty
        and gates["kubernetesWebMetrics"]
    )
    web_prometheus_evidence = bool(
        http_evidence
        and prometheus_web.get("enabled")
        and prometheus_web.get("available")
    )

    totals = arrival.get("totals", {}) if isinstance(arrival, dict) else {}
    offered = (
        finite_number(
            workload["workload"]["offeredIterationsPerSecond"],
            "offered throughput",
            minimum=0,
        )
        if workload.get("workload")
        else None
    )
    achieved = totals.get("achievedIterationsPerSecondOverConfiguredDuration")
    achieved = (
        finite_number(achieved, "achieved throughput", minimum=0)
        if achieved is not None
        else None
    )
    completed_iterations = totals.get("completedIterations")
    completed_iterations = (
        finite_number(completed_iterations, "completed iterations", minimum=0)
        if completed_iterations is not None
        else metric_value(summary, "iterations", "count")
    )
    dropped = (
        finite_number(arrival.get("droppedIterations"), "dropped iterations", minimum=0)
        if isinstance(arrival, dict) and arrival.get("droppedIterations") is not None
        else metric_value(summary, "dropped_iterations", "count")
    )
    expected_iterations = totals.get("expectedScheduledIterations")
    expected_iterations = (
        finite_number(expected_iterations, "expected iterations", minimum=0)
        if expected_iterations is not None
        else None
    )
    failure_rate = metric_value(
        summary, "http_req_failed{traffic:workload}", "rate"
    )
    request_count = metric_value(summary, "http_reqs", "count")
    received = metric_value(summary, "data_received", "count")
    sent = metric_value(summary, "data_sent", "count")
    metrics = {
        "throughput": {
            "offeredIterationsPerSecond": offered,
            "achievedIterationsPerSecond": achieved,
            "achievedRateRatio": (
                achieved / offered
                if achieved is not None and offered not in {None, 0}
                else None
            ),
            "completedIterations": completed_iterations,
            "expectedScheduledIterations": expected_iterations,
            "droppedIterations": dropped,
            "droppedRate": (
                dropped / expected_iterations
                if dropped is not None and expected_iterations not in {None, 0}
                else None
            ),
        },
        "requests": {
            "count": request_count,
            "failureRate": failure_rate,
            "unexpectedStatusRate": metric_value(
                summary, "bluemap_unexpected_status", "rate"
            ),
        },
        "transportProof": {
            "mode": traffic_mode,
            "passed": (
                bool(
                    warmup_transport_proof["passed"] is True
                    and measurement_transport_proof["passed"] is True
                )
                if traffic_mode == "ssh-l4-traefik"
                else None
            ),
            "warmup": warmup_transport_proof,
            "measurement": measurement_transport_proof,
        },
        "latencyMilliseconds": trend(
            summary, "http_req_duration{traffic:workload}"
        ),
        "ttfbMilliseconds": trend(summary, "bluemap_ttfb"),
        "bytes": {
            "received": received,
            "sent": sent,
            "receivedPerCompletedIteration": (
                received / completed_iterations
                if received is not None and completed_iterations not in {None, 0}
                else None
            ),
            "sentPerCompletedIteration": (
                sent / completed_iterations
                if sent is not None and completed_iterations not in {None, 0}
                else None
            ),
        },
        "webResources": {
            "preferred": preferred,
            "kubernetesSampler": resource.get("web")
            or {"available": False, "timestamps": 0},
            "prometheus": prometheus_web,
        },
        "databaseMetrics": database_metrics,
        "nodeNoise": node_noise,
    }
    return {
        "entryId": entry["entryId"],
        "sequence": entry["sequence"],
        "block": entry["block"],
        "caseId": entry["matrixCaseId"],
        "runnerCaseId": entry["runnerCaseId"],
        "variantId": entry["variantId"],
        "implementation": entry["implementation"],
        "storageType": entry["storageType"],
        "databaseBackend": entry["databaseBackend"],
        "replicaCount": entry["replicaCount"],
        "result": result["result"],
        "completedRepetitions": completed,
        "eligibleForFormalComparison": http_evidence,
        "metricEligibility": {
            "http": http_evidence,
            "webResource": web_resource_evidence,
            "webPrometheus": web_prometheus_evidence,
        },
        "gates": gates,
        "failedGates": failed_gates,
        "inputSha256": checksums,
        "controlIdentity": control_identity,
        "timing": timing,
        "metrics": metrics,
    }


def nested(value: dict[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def load_orchestrator_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise AnalysisFailure(f"orchestrator event log is missing: {path}")
    events: list[dict[str, Any]] = []
    previous = -math.inf
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise AnalysisFailure(f"{path}:{number}: invalid JSON") from error
        if not isinstance(event, dict):
            raise AnalysisFailure(f"{path}:{number}: event is not an object")
        epoch = timestamp_epoch(event.get("timestamp"), f"{path}:{number} timestamp")
        if epoch < previous:
            raise AnalysisFailure(f"{path}:{number}: event timestamps are out of order")
        previous = epoch
        event["_epoch"] = epoch
        events.append(event)
    return events


def validate_run_chronology(
    run_root: Path,
    schedule: dict[str, Any],
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    expected_entries: int = EXPECTED_ENTRIES,
) -> dict[str, Any]:
    events = load_orchestrator_events(run_root / "events.ndjson")
    relevant = {
        "activation-start",
        "runner-started",
        "inter-entry-cooldown-completed",
        "runner-completed",
    }
    by_sequence: defaultdict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for event in events:
        name = event.get("event")
        if name == "cleanup-failed":
            continue
        sequence = event.get("sequence")
        if (
            name not in relevant
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 1 <= sequence <= expected_entries
            or name in by_sequence[sequence]
        ):
            raise AnalysisFailure("orchestrator event log has an invalid event identity")
        by_sequence[sequence][name] = event
    if set(by_sequence) != set(range(1, expected_entries + 1)):
        raise AnalysisFailure(
            "orchestrator event log does not cover every expected entry"
        )

    rows_by_sequence = {row["sequence"]: row for row in rows}
    previous_completed = timestamp_epoch(
        state.get("createdAt"), "run state createdAt"
    )
    chronology: list[dict[str, Any]] = []
    for entry in schedule["entries"]:
        sequence = entry["sequence"]
        item = state["entries"][str(sequence)]
        row = rows_by_sequence[sequence]
        item_events = by_sequence[sequence]
        if set(item_events) != relevant:
            raise AnalysisFailure(
                f"{entry['entryId']}: incomplete orchestrator event sequence"
            )
        if any(
            event.get("entryId") != entry["entryId"]
            for event in item_events.values()
        ):
            raise AnalysisFailure(
                f"{entry['entryId']}: orchestrator event entry identity mismatch"
            )
        started = timestamp_epoch(item["startedAt"], f"{entry['entryId']} startedAt")
        runner_started = timestamp_epoch(
            item["runnerStartedAt"], f"{entry['entryId']} runnerStartedAt"
        )
        completed = timestamp_epoch(
            item["completedAt"], f"{entry['entryId']} completedAt"
        )
        activation_event = item_events["activation-start"]["_epoch"]
        runner_event = item_events["runner-started"]["_epoch"]
        cooldown_event = item_events["inter-entry-cooldown-completed"]["_epoch"]
        completed_event = item_events["runner-completed"]["_epoch"]
        timing = row["timing"]
        ordered = (
            previous_completed,
            started,
            activation_event,
            runner_started,
            runner_event,
            timing["resultStartedEpoch"],
            timing["resultCompletedEpoch"],
        )
        if any(right + 0.01 < left for left, right in zip(ordered, ordered[1:])):
            raise AnalysisFailure(
                f"{entry['entryId']}: state, runner, result, or event chronology differs"
            )
        if (
            item_events["runner-started"].get("webPods") != item["webPods"]
            or item_events["runner-completed"].get("result") != item["result"]
            or item_events["runner-completed"].get("exitStatus")
            != item["runnerExitStatus"]
        ):
            raise AnalysisFailure(
                f"{entry['entryId']}: orchestrator event payload mismatch"
            )

        cooldown = item.get("interEntryCooldown")
        if not isinstance(cooldown, dict) or set(cooldown) != {
            "requiredSeconds",
            "runnerSatisfied",
            "orchestratorWaitedSeconds",
            "waitStartedAt",
            "completedAt",
        }:
            raise AnalysisFailure(
                f"{entry['entryId']}: inter-entry cooldown evidence is malformed"
            )
        required = entry["cooldownSeconds"]
        waited = finite_number(
            cooldown.get("orchestratorWaitedSeconds"),
            f"{entry['entryId']}: orchestratorWaitedSeconds",
            minimum=0,
        )
        wait_started = timestamp_epoch(
            cooldown.get("waitStartedAt"),
            f"{entry['entryId']}: cooldown waitStartedAt",
        )
        wait_completed = timestamp_epoch(
            cooldown.get("completedAt"),
            f"{entry['entryId']}: cooldown completedAt",
        )
        runner_satisfied = cooldown.get("runnerSatisfied")
        if (
            cooldown.get("requiredSeconds") != required
            or not isinstance(runner_satisfied, bool)
            or wait_completed < wait_started
            or wait_started + 0.01 < timing["resultCompletedEpoch"]
            or cooldown_event + 0.01 < wait_completed
            or completed + 0.01 < cooldown_event
            or completed_event + 0.01 < completed
        ):
            raise AnalysisFailure(
                f"{entry['entryId']}: inter-entry cooldown identity mismatch"
            )
        if runner_satisfied:
            if not timing["runnerCooldownSatisfied"]:
                raise AnalysisFailure(
                    f"{entry['entryId']}: claimed runner cooldown is not in phases"
                )
        elif (
            waited < required - 0.1
            or wait_completed - wait_started < required - 0.1
        ):
            raise AnalysisFailure(
                f"{entry['entryId']}: orchestrator fallback cooldown is too short"
            )
        cooldown_event_payload = {
            key: value
            for key, value in item_events[
                "inter-entry-cooldown-completed"
            ].items()
            if key
            not in {"timestamp", "sequence", "event", "entryId", "_epoch"}
        }
        if cooldown_event_payload != cooldown:
            raise AnalysisFailure(
                f"{entry['entryId']}: cooldown event/state evidence differs"
            )
        previous_completed = completed_event
        chronology.append(
            {
                "sequence": sequence,
                "startedEpoch": started,
                "runnerStartedEpoch": runner_started,
                "resultStartedEpoch": timing["resultStartedEpoch"],
                "resultCompletedEpoch": timing["resultCompletedEpoch"],
                "cooldownCompletedEpoch": wait_completed,
                "completedEpoch": completed,
            }
        )
    run_completed = timestamp_epoch(
        state.get("completedAt"), "run state completedAt"
    )
    run_updated = timestamp_epoch(state.get("updatedAt"), "run state updatedAt")
    if (
        run_completed + 0.01 < previous_completed
        or run_updated + 0.01 < run_completed
    ):
        raise AnalysisFailure(
            "run completion timestamps precede the final orchestrator entry"
        )
    return {
        "validated": True,
        "runCompletedEpoch": run_completed,
        "entries": chronology,
    }


def summarize_runpod_capacity_control_continuity(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    phases = {"warmup", "measurement"}
    controls_by_phase: dict[str, dict[str, Any]] = {}
    stable_control: dict[str, Any] | None = None
    evidence_count = 0
    all_passed = True
    for row in rows:
        capacity = row["controlIdentity"]["loadGeneratorCapacity"]
        if not isinstance(capacity, dict) or not set(capacity) <= phases:
            raise AnalysisFailure(
                "RunPod load-generator capacity evidence is malformed"
            )
        if row["result"] == "passed" and set(capacity) != phases:
            raise AnalysisFailure(
                "passed result lacks complete RunPod load-generator capacity evidence"
            )
        for phase, evidence in capacity.items():
            control = {
                "limits": evidence["limits"],
                "capacity": evidence["capacity"],
            }
            if stable_control is None:
                stable_control = control
            elif control != stable_control:
                raise AnalysisFailure(
                    "RunPod load-generator capacity controls changed across cases "
                    "or phases"
                )
            controls_by_phase[phase] = control
            evidence_count += 1
            all_passed = all_passed and evidence["passed"] is True
    return {
        "controls": controls_by_phase,
        "casePhaseEvidenceCount": evidence_count,
        "allPassed": evidence_count > 0 and all_passed,
    }


def validate_run_control_continuity(
    rows: list[dict[str, Any]], state: dict[str, Any]
) -> dict[str, Any]:
    execution = validate_execution_identity(state["executionIdentity"])
    controls = [row["controlIdentity"] for row in rows]
    first = controls[0]
    capacity_continuity = summarize_runpod_capacity_control_continuity(rows)
    for row, control in zip(rows, controls, strict=True):
        if control["namespace"] != execution["namespace"]:
            raise AnalysisFailure("workload namespaces differ from execution identity")
        if control["databasePods"] != [execution["databasePod"]]:
            raise AnalysisFailure("database Pod identity is not continuous across run")
        database_identity = control["databasePodIdentity"]
        if (
            database_identity["name"] != execution["databasePod"]
            or database_identity["namespace"] != execution["namespace"]
        ):
            raise AnalysisFailure(
                "database snapshot name differs from execution identity"
            )
        stable_keys = ("namespace", "name", "uid", "specSha256")
        database_stable = {
            key: database_identity[key] for key in stable_keys
        }
        if (
            database_stable
            != {
                key: first["databasePodIdentity"][key]
                for key in stable_keys
            }
        ):
            raise AnalysisFailure(
                "database Pod identity or spec changed across the run"
            )
        if (
            control["loadGeneratorBackend"] != "runpod-ssh"
            or control["loadGeneratorIdentity"]
            != execution["loadGeneratorIdentity"]
            or control["loadGeneratorIdentitySha256"]
            != execution["loadGeneratorIdentitySha256"]
            or control["loadGeneratorRuntimeIdentity"]
            != first["loadGeneratorRuntimeIdentity"]
        ):
            raise AnalysisFailure(
                "RunPod load-generator identity changed across the run"
            )
        if control["traffic"] != first["traffic"]:
            raise AnalysisFailure("traffic identity changed across the run")
        if (
            control["traffic"]["formalRunId"] != execution["formalRunId"]
            or control["traffic"]["mode"] != execution["traffic"]["mode"]
            or control["traffic"]["service"] != execution["traffic"]["service"]
            or control["traffic"]["port"] != execution["traffic"]["port"]
            or control["traffic"]["requiresEdgeBypass"]
            != execution["traffic"]["requiresEdgeBypass"]
            or control["traffic"]["tunnel"] != execution["traffic"]["tunnel"]
            or control["traffic"]["baseUrl"]
            != normalized_http_url(
                execution["traffic"]["baseUrl"],
                "execution identity traffic baseUrl",
            )
        ):
            raise AnalysisFailure(
                "traffic controls differ from execution identity"
            )
        if (
            row["inputSha256"].get("run_origin_case.sh")
            != execution["runnerSha256"]
        ):
            raise AnalysisFailure(
                "archived runner digest differs from execution identity"
            )
        if (
            control["runtime"]["pythonCommand"]
            != execution["benchmarkPython"]
        ):
            raise AnalysisFailure(
                "workload Python command differs from execution identity"
            )
        workload_prometheus = control["observability"]["prometheus"]
        state_prometheus = execution["prometheus"]
        if workload_prometheus["enabled"] != state_prometheus["enabled"]:
            raise AnalysisFailure("Prometheus enablement differs from execution identity")
        if state_prometheus["enabled"] and workload_prometheus["baseUrl"] != (
            normalized_http_url(
                state_prometheus["url"], "execution identity Prometheus URL"
            )
        ):
            raise AnalysisFailure("Prometheus source differs from execution identity")
        if control["nodes"] != first["nodes"]:
            raise AnalysisFailure("target nodes changed across the formal run")
        if control["observability"] != first["observability"]:
            raise AnalysisFailure("observability controls changed across the formal run")
    origins_by_variant: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        origins_by_variant[row["variantId"]].append(row["controlIdentity"]["origin"])
    for variant, origins in origins_by_variant.items():
        if any(origin != origins[0] for origin in origins[1:]):
            raise AnalysisFailure(f"origin identity changed for variant {variant}")
    return {
        "validated": True,
        "executionIdentity": execution,
        "namespace": first["namespace"],
        "databasePods": first["databasePods"],
        "databasePodIdentity": {
            key: first["databasePodIdentity"][key] for key in stable_keys
        },
        "loadGeneratorBackend": first["loadGeneratorBackend"],
        "loadGeneratorIdentity": first["loadGeneratorIdentity"],
        "loadGeneratorIdentitySha256": first[
            "loadGeneratorIdentitySha256"
        ],
        "loadGeneratorRuntimeIdentity": first[
            "loadGeneratorRuntimeIdentity"
        ],
        "loadGeneratorCapacity": capacity_continuity,
        "traffic": first["traffic"],
        "nodes": first["nodes"],
        "runtime": first["runtime"],
        "observability": first["observability"],
        "originsByVariant": {
            variant: origins[0] for variant, origins in origins_by_variant.items()
        },
    }


def apply_block_noise_comparability(
    rows: list[dict[str, Any]], control_identity: dict[str, Any]
) -> dict[str, Any]:
    for row in rows:
        row["preBlockMetricEligibility"] = dict(row["metricEligibility"])
    prometheus = control_identity["observability"]["prometheus"]
    if not prometheus["enabled"]:
        return {
            "enabled": False,
            "excludedCaseBlocks": [],
            "caseBlocks": [],
        }
    expected_nodes = set(control_identity["nodes"])
    spread_limit = float(prometheus["maximumNonTargetNodeCpuRangeCores"])
    expected_variants: defaultdict[str, set[str]] = defaultdict(set)
    grouped: defaultdict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        expected_variants[row["caseId"]].add(row["variantId"])
        grouped[(row["caseId"], row["block"])].append(row)
    expected_groups = {
        (case_id, block)
        for case_id in expected_variants
        for block in range(1, EXPECTED_BLOCKS + 1)
    }
    if set(grouped) != expected_groups:
        raise AnalysisFailure("node-noise case/block groups are incomplete")

    case_block_reports: list[dict[str, Any]] = []
    excluded_case_blocks: list[dict[str, Any]] = []
    for case_id, block in sorted(grouped):
        block_rows = grouped[(case_id, block)]
        if {row["variantId"] for row in block_rows} != expected_variants[case_id]:
            raise AnalysisFailure(
                f"node-noise variants are incomplete for {case_id} block {block}"
            )
        values: defaultdict[str, dict[str, list[float]]] = defaultdict(
            lambda: {"mean": [], "maximum": []}
        )
        reasons: list[str] = []
        for row in block_rows:
            noise = row["metrics"]["nodeNoise"]
            repetitions = noise.get("repetitions")
            if (
                noise.get("enabled") is not True
                or noise.get("available") is not True
                or noise.get("passed") is not True
                or not isinstance(repetitions, list)
                or len(repetitions) != 1
                or not isinstance(repetitions[0], dict)
                or not isinstance(repetitions[0].get("nodes"), list)
            ):
                reasons.append(f"{row['entryId']}: unavailable-or-noisy")
                continue
            nodes = repetitions[0]["nodes"]
            by_node = {
                item.get("node"): item for item in nodes if isinstance(item, dict)
            }
            if set(by_node) != expected_nodes:
                reasons.append(f"{row['entryId']}: node-set-mismatch")
                continue
            for node, item in by_node.items():
                mean = finite_number(
                    item.get("meanCores"), f"{row['entryId']}: {node} mean", minimum=0
                )
                maximum = finite_number(
                    item.get("maximumCores"),
                    f"{row['entryId']}: {node} maximum",
                    minimum=0,
                )
                values[node]["mean"].append(mean)
                values[node]["maximum"].append(maximum)
        node_reports = []
        if not reasons:
            for node in sorted(expected_nodes):
                means = values[node]["mean"]
                maxima = values[node]["maximum"]
                mean_spread = max(means) - min(means)
                maximum_spread = max(maxima) - min(maxima)
                comparable = (
                    len(means) == len(block_rows)
                    and mean_spread <= spread_limit
                    and maximum_spread <= spread_limit
                )
                node_reports.append(
                    {
                        "node": node,
                        "samples": len(means),
                        "meanSpreadCores": mean_spread,
                        "maximumSpreadCores": maximum_spread,
                        "maximumAllowedSpreadCores": spread_limit,
                        "comparable": comparable,
                    }
                )
                if not comparable:
                    reasons.append(f"{node}: cross-run-background-spread")
        comparable = not reasons
        if not comparable:
            excluded_case_blocks.append({"caseId": case_id, "block": block})
            for row in block_rows:
                for key in row["metricEligibility"]:
                    row["metricEligibility"][key] = False
                row["eligibleForFormalComparison"] = False
                if "blockNodeNoiseComparability" not in row["failedGates"]:
                    row["failedGates"].append("blockNodeNoiseComparability")
                row["gates"]["blockNodeNoiseComparability"] = False
        else:
            for row in block_rows:
                row["gates"]["blockNodeNoiseComparability"] = True
        case_block_reports.append(
            {
                "caseId": case_id,
                "block": block,
                "comparable": comparable,
                "reasons": reasons,
                "nodes": node_reports,
            }
        )
    return {
        "enabled": True,
        "maximumAllowedSpreadCores": spread_limit,
        "excludedCaseBlocks": excluded_case_blocks,
        "caseBlocks": case_block_reports,
    }


def metric_eligibility_counts(
    rows: list[dict[str, Any]], field: str
) -> dict[str, int]:
    return {
        key: sum(row[field][key] is True for row in rows)
        for key in ("http", "webResource", "webPrometheus")
    }


def aggregate_rows(
    matrix: dict[str, Any], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["caseId"], row["variantId"])].append(row)
    output = []
    for case in matrix["cases"]:
        for variant in case["variants"]:
            group = sorted(groups[(case["id"], variant)], key=lambda row: row["block"])
            metrics = {
                name: describe(
                    nested(row, path)
                    for row in group
                    if row["metricEligibility"][METRIC_ELIGIBILITY[name]]
                )
                for name, path in AGGREGATE_METRICS.items()
            }
            eligible = [
                row for row in group if row["metricEligibility"]["http"]
            ]
            eligible_metric_blocks = {
                name: [
                    row["block"]
                    for row in group
                    if row["metricEligibility"][METRIC_ELIGIBILITY[name]]
                ]
                for name in AGGREGATE_METRICS
            }
            database_metric_names = sorted(
                {
                    name
                    for row in group
                    for name in nested(
                        row,
                        (
                            "metrics",
                            "databaseMetrics",
                            "prometheus",
                            "metrics",
                        ),
                    )
                    or {}
                }
            )
            observability = {
                "webKubernetesMetricBlocks": [
                    row["block"]
                    for row in group
                    if nested(
                        row,
                        (
                            "metrics",
                            "webResources",
                            "kubernetesSampler",
                            "available",
                        ),
                    )
                    is True
                ],
                "webPrometheusMetricBlocks": [
                    row["block"]
                    for row in group
                    if nested(
                        row,
                        (
                            "metrics",
                            "webResources",
                            "prometheus",
                            "available",
                        ),
                    )
                    is True
                ],
                "databaseKubernetesMetricBlocks": [
                    row["block"]
                    for row in group
                    if nested(
                        row,
                        (
                            "metrics",
                            "databaseMetrics",
                            "kubernetesSampler",
                            "available",
                        ),
                    )
                    is True
                ],
                "databasePrometheusMetricBlocks": [
                    row["block"]
                    for row in group
                    if nested(
                        row,
                        (
                            "metrics",
                            "databaseMetrics",
                            "prometheus",
                            "available",
                        ),
                    )
                    is True
                ],
                "databasePrometheusMetrics": {
                    name: [
                        row["block"]
                        for row in group
                        if nested(
                            row,
                            (
                                "metrics",
                                "databaseMetrics",
                                "prometheus",
                                "metrics",
                                name,
                                "available",
                            ),
                        )
                        is True
                    ]
                    for name in database_metric_names
                },
                "nodeNoiseAvailableBlocks": [
                    row["block"]
                    for row in group
                    if nested(
                        row, ("metrics", "nodeNoise", "available")
                    )
                    is True
                ],
                "nodeNoisePassedBlocks": [
                    row["block"]
                    for row in group
                    if nested(row, ("metrics", "nodeNoise", "passed")) is True
                ],
            }
            output.append(
                {
                    "caseId": case["id"],
                    "variantId": variant,
                    "scheduledBlocks": [row["block"] for row in group],
                    "eligibleBlocks": [row["block"] for row in eligible],
                    "eligibleMetricBlocks": eligible_metric_blocks,
                    "scheduledRepetitions": len(group),
                    "eligibleRepetitions": len(eligible),
                    "completeFiveBlockEstimate": len(eligible) == EXPECTED_BLOCKS,
                    "metrics": metrics,
                    "observability": observability,
                }
            )
    return output


def paired_comparisons(
    matrix: dict[str, Any], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    row_index = {
        (row["caseId"], row["variantId"], row["block"]): row for row in rows
    }
    comparisons: list[dict[str, Any]] = []
    for case in matrix["cases"]:
        variants = case["variants"]
        for reference, candidate in itertools.combinations(variants, 2):
            metric_results: dict[str, Any] = {}
            for metric_name, path in AGGREGATE_METRICS.items():
                block_values = []
                for block in range(1, EXPECTED_BLOCKS + 1):
                    reference_row = row_index[(case["id"], reference, block)]
                    candidate_row = row_index[(case["id"], candidate, block)]
                    reference_value = nested(reference_row, path)
                    candidate_value = nested(candidate_row, path)
                    eligibility_key = METRIC_ELIGIBILITY[metric_name]
                    eligible = (
                        reference_row["metricEligibility"][eligibility_key]
                        and candidate_row["metricEligibility"][eligibility_key]
                        and isinstance(reference_value, (int, float))
                        and not isinstance(reference_value, bool)
                        and isinstance(candidate_value, (int, float))
                        and not isinstance(candidate_value, bool)
                    )
                    difference = (
                        float(candidate_value) - float(reference_value)
                        if eligible
                        else None
                    )
                    ratio = (
                        float(candidate_value) / float(reference_value)
                        if eligible and float(reference_value) != 0
                        else None
                    )
                    block_values.append(
                        {
                            "block": block,
                            "eligible": eligible,
                            "reference": reference_value,
                            "candidate": candidate_value,
                            "difference": difference,
                            "ratio": ratio,
                        }
                    )
                eligible_blocks = [
                    item["block"] for item in block_values if item["eligible"]
                ]
                metric_results[metric_name] = {
                    "blocks": block_values,
                    "eligibleBlocks": eligible_blocks,
                    "completeFiveBlockPair": len(eligible_blocks) == EXPECTED_BLOCKS,
                    "difference": describe(
                        item["difference"] for item in block_values
                    ),
                    "ratio": describe(item["ratio"] for item in block_values),
                }
            comparisons.append(
                {
                    "caseId": case["id"],
                    "referenceVariantId": reference,
                    "candidateVariantId": candidate,
                    "metrics": metric_results,
                    "inference": (
                        "Descriptive paired estimates only. Five scheduled blocks "
                        "do not justify a claim of statistical significance."
                    ),
                }
            )
    return comparisons


def fmt(value: Any, digits: int = 2) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"
    return f"{value:.{digits}f}"


def fmt_percent(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.3f}%"


def median_metric(group: dict[str, Any], name: str) -> Any:
    value = group["metrics"].get(name)
    return value.get("median") if isinstance(value, dict) else None


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# BlueMap formal web-performance analysis",
        "",
        f"- Analysis status: **{report['analysisStatus']}**",
        f"- Formal run state: **{report['run']['stateStatus']}**",
        f"- Scheduled/result entries: {report['run']['scheduledEntries']}/"
        f"{report['run']['resultEntries']}",
        f"- Eligible formal measurements: {report['run']['eligibleEntries']}/"
        f"{report['run']['resultEntries']}",
        "- Statistical scope: descriptive five-block estimates only; no "
        "statistical-significance claim is made at n=5.",
        "",
    ]
    if report["failedEntries"]:
        lines.extend(
            [
                "## Failed gates",
                "",
                "These entries remain in the raw table. Eligibility is "
                "metric-specific: complete adverse HTTP outcomes remain in "
                "HTTP aggregates, while unusable telemetry is excluded only "
                "from its affected metric family.",
                "",
            ]
        )
        for item in report["failedEntries"]:
            lines.append(
                f"- `{item['entryId']}`: {', '.join(item['failedGates'])}"
            )
        lines.append("")
    if report["run"]["warnings"]:
        lines.extend(["## Run warnings", ""])
        lines.extend(f"- {warning}" for warning in report["run"]["warnings"])
        lines.append("")

    lines.extend(
        [
            "## Per case and variant",
            "",
            "| Case | Variant | Eligible | Offered/s | Achieved/s | Failure | "
            "Dropped | Latency p50/p95/p99 ms | TTFB p95 ms | KiB/iteration | "
            "Web CPU p95 | Web memory p95 MiB | Throttle p95 | "
            "DB k8s/exporter blocks | Noise pass/available |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group in report["caseVariantSummaries"]:
        latency = "/".join(
            fmt(median_metric(group, name), 1)
            for name in (
                "latencyP50Milliseconds",
                "latencyP95Milliseconds",
                "latencyP99Milliseconds",
            )
        )
        bytes_per = median_metric(group, "receivedBytesPerIteration")
        memory = median_metric(group, "webMemoryP95Bytes")
        observability = group["observability"]
        lines.append(
            f"| {group['caseId']} | {group['variantId']} | "
            f"{group['eligibleRepetitions']}/5 | "
            f"{fmt(median_metric(group, 'offeredThroughput'))} | "
            f"{fmt(median_metric(group, 'achievedThroughput'))} | "
            f"{fmt_percent(median_metric(group, 'failureRate'))} | "
            f"{fmt_percent(median_metric(group, 'droppedRate'))} | "
            f"{latency} | "
            f"{fmt(median_metric(group, 'ttfbP95Milliseconds'), 1)} | "
            f"{fmt(bytes_per / 1024 if bytes_per is not None else None, 1)} | "
            f"{fmt(median_metric(group, 'webCpuP95Cores'), 3)} | "
            f"{fmt(memory / 1024**2 if memory is not None else None, 1)} | "
            f"{fmt_percent(median_metric(group, 'webThrottleP95Ratio'))} | "
            f"{len(observability['databaseKubernetesMetricBlocks'])}/"
            f"{len(observability['databasePrometheusMetricBlocks'])} | "
            f"{len(observability['nodeNoisePassedBlocks'])}/"
            f"{len(observability['nodeNoiseAvailableBlocks'])} |"
        )

    lines.extend(
        [
            "",
            "Each cell above is the median across eligible blocks. The JSON "
            "report also records Q1, Q3, IQR, MAD, minima, and maxima.",
            "",
            "## Block-paired comparisons",
            "",
            "Ratios are candidate/reference within the same block and then "
            "summarized by their median. Values below 1 favor the candidate for "
            "latency and resource use; values above 1 favor it for throughput. "
            "Paired counts are shown separately for throughput/latency/CPU "
            "(T/L/C).",
            "",
            "| Case | Reference → candidate | Paired blocks T/L/C | Achieved ratio | "
            "p95 latency ratio | CPU p95 ratio |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for comparison in report["pairedComparisons"]:
        def paired_ratio(name: str) -> tuple[int, Any]:
            item = comparison["metrics"][name]
            ratio = item["ratio"]
            return len(item["eligibleBlocks"]), (
                ratio.get("median") if isinstance(ratio, dict) else None
            )

        throughput_n, throughput = paired_ratio("achievedThroughput")
        latency_n, latency = paired_ratio("latencyP95Milliseconds")
        cpu_n, cpu = paired_ratio("webCpuP95Cores")
        lines.append(
            f"| {comparison['caseId']} | {comparison['referenceVariantId']} → "
            f"{comparison['candidateVariantId']} | "
            f"{throughput_n}/{latency_n}/{cpu_n} of 5 | "
            f"{fmt(throughput, 3)} | "
            f"{fmt(latency, 3)} | {fmt(cpu, 3)} |"
        )

    lines.extend(
        [
            "",
            "## Raw repetition table",
            "",
            "| Seq | Block | Case | Variant | Result | Eligible | Offered/s | "
            "Achieved/s | Failure | Dropped | p50/p90/p95/p99 ms | "
            "TTFB p50/p90/p95/p99 ms | Received MiB | Web CPU source | "
            "DB metrics | Node noise |",
            "|---:|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"
            "---|---|---|",
        ]
    )
    for row in report["rawRepetitions"]:
        metrics = row["metrics"]
        latency = "/".join(
            fmt(metrics["latencyMilliseconds"][key], 1)
            for key in ("p50", "p90", "p95", "p99")
        )
        ttfb = "/".join(
            fmt(metrics["ttfbMilliseconds"][key], 1)
            for key in ("p50", "p90", "p95", "p99")
        )
        database = metrics["databaseMetrics"]["prometheus"]
        database_kubernetes = metrics["databaseMetrics"]["kubernetesSampler"]
        node = metrics["nodeNoise"]
        lines.append(
            f"| {row['sequence']} | {row['block']} | {row['caseId']} | "
            f"{row['variantId']} | {row['result']} | "
            f"{'yes' if row['eligibleForFormalComparison'] else 'no'} | "
            f"{fmt(metrics['throughput']['offeredIterationsPerSecond'])} | "
            f"{fmt(metrics['throughput']['achievedIterationsPerSecond'])} | "
            f"{fmt_percent(metrics['requests']['failureRate'])} | "
            f"{fmt_percent(metrics['throughput']['droppedRate'])} | "
            f"{latency} | {ttfb} | "
            f"{fmt(metrics['bytes']['received'] / 1024**2 if metrics['bytes']['received'] is not None else None, 2)} | "
            f"{metrics['webResources']['preferred']['source'] or 'unavailable'} | "
            f"k8s={'yes' if database_kubernetes.get('available') else 'no'}, "
            f"exporter={'yes' if database.get('available') else 'no'} | "
            f"{'pass' if node.get('passed') is True else ('n/a' if not node.get('enabled') else 'fail')} |"
        )
    lines.extend(
        [
            "",
            "The raw JSON is authoritative and contains all gate results, "
            "resource distributions, database metric availability, node-noise "
            "details, and per-block paired values.",
            "",
        ]
    )
    return "\n".join(lines)


def write_invalid(output_dir: Path, errors: list[str]) -> None:
    report = {
        "formatVersion": FORMAT_VERSION,
        "generatedAt": utc_now(),
        "analysisStatus": "invalid",
        "errors": errors,
        "rawRepetitions": [],
        "caseVariantSummaries": [],
        "pairedComparisons": [],
    }
    atomic_json(output_dir / "report.json", report)
    markdown = [
        "# BlueMap formal web-performance analysis",
        "",
        "**Analysis status: invalid. No measurements were included.**",
        "",
        "The fail-closed structural validation rejected this run:",
        "",
        *[f"- {error}" for error in errors],
        "",
    ]
    atomic_text(output_dir / "report.md", "\n".join(markdown))


def analyze(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    matrix_path = args.matrix.resolve()
    schedule_path = args.schedule.resolve()
    admission_path = (
        args.runtime_admission_identities
        if getattr(args, "runtime_admission_identities", None) is not None
        else matrix_path.parent / "runtime-admission-identities.json"
    ).resolve()
    bundle_manifest_path = (
        args.bundle_manifest
        if getattr(args, "bundle_manifest", None) is not None
        else matrix_path.parent / "bundle-manifest.json"
    ).resolve()
    run_root = args.run_root.resolve()
    if not run_root.is_dir():
        raise AnalysisFailure(f"run root does not exist: {run_root}")
    matrix, schedule, matrix_digest, schedule_digest = validate_documents(
        matrix_path, schedule_path
    )
    analyzer_digest = sha256_file(Path(__file__).resolve())
    (
        expected_admission,
        bundle,
        admission_digest,
        bundle_digest,
    ) = validate_frozen_bundle(
        matrix,
        matrix_path,
        schedule_path,
        matrix_digest,
        schedule_digest,
        admission_path,
        bundle_manifest_path,
        analyzer_digest,
    )
    state = validate_state(
        run_root,
        schedule,
        matrix_digest,
        schedule_digest,
        matrix,
        admission_digest=admission_digest,
        bundle_digest=bundle_digest,
        bundle=bundle,
        analyzer_digest=analyzer_digest,
        expected_admission=expected_admission,
    )
    execution_identity = validate_execution_identity(state["executionIdentity"])
    load_generator_sha256 = validate_load_generator_execution_binding(
        bundle["loadGenerator"],
        execution_identity["loadGeneratorIdentity"],
    )
    preflight_attestation = validate_preflight_attestation(
        state.get("preflightAttestation"),
        run_root=run_root,
        matrix=matrix,
        matrix_digest=matrix_digest,
        schedule_digest=schedule_digest,
        admission_digest=admission_digest,
        bundle_digest=bundle_digest,
        orchestrator_digest=bundle["orchestratorSha256"],
        load_generator_sha256=load_generator_sha256,
        execution_identity=execution_identity,
        expected_admission=expected_admission,
    )
    results_root = run_root / "results"
    if not results_root.is_dir():
        raise AnalysisFailure("run root has no results directory")
    expected_directories = {entry["runnerCaseId"] for entry in schedule["entries"]}
    actual_directories = {
        path.name for path in results_root.iterdir() if path.is_dir()
    }
    if actual_directories != expected_directories:
        missing = sorted(expected_directories - actual_directories)
        extra = sorted(actual_directories - expected_directories)
        raise AnalysisFailure(
            "results directories do not exactly match the schedule "
            f"(missing={missing}, extra={extra})"
        )

    rows: list[dict[str, Any]] = []
    for entry in schedule["entries"]:
        try:
            row = analyze_case(
                results_root / entry["runnerCaseId"],
                entry,
                state["entries"][str(entry["sequence"])],
                execution_identity,
                matrix_path,
                schedule_path,
                matrix_digest,
                schedule_digest,
            )
            rows.append(row)
        except AnalysisFailure as error:
            raise AnalysisFailure(
                "result/input identity or artifact validation failed: "
                f"{error}"
            ) from error
    if len(rows) != EXPECTED_ENTRIES:
        raise AnalysisFailure("internal error: not all 80 rows were analyzed")
    identity_keys = {
        (row["caseId"], row["variantId"], row["block"]) for row in rows
    }
    if len(identity_keys) != EXPECTED_ENTRIES:
        raise AnalysisFailure("result identities are not unique case/variant/blocks")
    immutable_inputs = RUNNER_INPUT_FILES - {"workload.json", "schedule-entry.json"}
    for filename in immutable_inputs:
        digests = {row["inputSha256"].get(filename) for row in rows}
        if len(digests) != 1 or None in digests:
            raise AnalysisFailure(
                f"archived input {filename} is not identical across all 80 results"
            )

    control_identity = validate_run_control_continuity(rows, state)
    chronology = validate_run_chronology(run_root, schedule, state, rows)
    noise_comparability = apply_block_noise_comparability(rows, control_identity)
    pre_block_metric_eligible = metric_eligibility_counts(
        rows, "preBlockMetricEligibility"
    )
    metric_eligible = metric_eligibility_counts(rows, "metricEligibility")

    failed = [
        {"entryId": row["entryId"], "failedGates": row["failedGates"]}
        for row in rows
        if row["failedGates"]
    ]
    run_warnings = []
    if state.get("cleanupError"):
        run_warnings.append(f"orchestrator cleanup failed: {state['cleanupError']}")
    status = (
        "complete"
        if not failed and not run_warnings
        else "complete-with-failed-gates"
    )
    report = {
        "formatVersion": FORMAT_VERSION,
        "generatedAt": utc_now(),
        "analysisStatus": status,
        "inputs": {
            "matrix": str(matrix_path),
            "matrixSha256": matrix_digest,
            "schedule": str(schedule_path),
            "scheduleSha256": schedule_digest,
            "runtimeAdmissionIdentities": str(admission_path),
            "runtimeAdmissionIdentitiesSha256": admission_digest,
            "bundleManifest": str(bundle_manifest_path),
            "bundleManifestSha256": bundle_digest,
            "runRoot": str(run_root),
            "benchmarkGitRevision": matrix["benchmarkGitRevision"],
            "orchestratorSha256": bundle["orchestratorSha256"],
            "freezerSha256": bundle["freezerSha256"],
            "controllerLockSha256": bundle["controllerLockSha256"],
            "analyzerSha256": analyzer_digest,
            "loadGenerator": bundle["loadGenerator"],
            "loadGeneratorSha256": load_generator_sha256,
        },
        "run": {
            "stateStatus": state["status"],
            "scheduledEntries": EXPECTED_ENTRIES,
            "resultEntries": len(rows),
            "eligibleEntries": metric_eligible["http"],
            "preBlockMetricEligibleEntries": pre_block_metric_eligible,
            "metricEligibleEntries": metric_eligible,
            "failedGateEntries": len(failed),
            "warnings": run_warnings,
            "cleanupError": state.get("cleanupError"),
        },
        "method": {
            "blocks": EXPECTED_BLOCKS,
            "aggregation": (
                "Median with Q1/Q3, IQR, MAD, minimum, and maximum over "
                "eligible scheduled blocks."
            ),
            "pairing": (
                "All case-local variant pairs are compared within the same "
                "scheduled block before descriptive aggregation."
            ),
            "inference": (
                "No statistical-significance claim is made from five blocks."
            ),
            "failedMeasurements": (
                "Adverse HTTP, arrival, and latency outcomes remain in HTTP "
                "aggregates when their measurement artifacts are complete. "
                "Structurally unusable or telemetry-specific values are "
                "excluded only from the affected metric family."
            ),
        },
        "failedEntries": failed,
        "preflightAttestation": preflight_attestation,
        "controlIdentity": control_identity,
        "chronology": chronology,
        "nodeNoiseComparability": noise_comparability,
        "rawRepetitions": rows,
        "caseVariantSummaries": aggregate_rows(matrix, rows),
        "pairedComparisons": paired_comparisons(matrix, rows),
    }
    return report, 0 if not failed and not run_warnings else 2


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir.resolve()
    results_root = args.run_root.resolve() / "results"
    try:
        output_dir.relative_to(results_root)
    except ValueError:
        pass
    else:
        print(
            "FORMAL ANALYSIS FAILURE: output directory must not be inside "
            f"the immutable result set {results_root}",
            file=sys.stderr,
        )
        return 1
    try:
        report, status = analyze(args)
    except (AnalysisFailure, OSError, TypeError, ValueError, KeyError) as error:
        write_invalid(output_dir, [str(error)])
        print(f"FORMAL ANALYSIS FAILURE: {error}", file=sys.stderr)
        return 1
    atomic_json(output_dir / "report.json", report)
    atomic_text(output_dir / "report.md", render_markdown(report))
    if status == 2:
        print(
            "FORMAL ANALYSIS COMPLETED WITH FAILED GATES; "
            "see report.json and report.md",
            file=sys.stderr,
        )
    else:
        print(
            json.dumps(
                {
                    "analysisStatus": report["analysisStatus"],
                    "entries": report["run"]["resultEntries"],
                    "eligible": report["run"]["eligibleEntries"],
                    "json": str(output_dir / "report.json"),
                    "markdown": str(output_dir / "report.md"),
                },
                sort_keys=True,
            )
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
