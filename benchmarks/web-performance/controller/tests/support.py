from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


TEST_DIR = Path(__file__).resolve().parent
CONTROLLER_DIR = TEST_DIR.parent
FORMAL_DIR = CONTROLLER_DIR / "formal"


def load_module(name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# freeze.py deliberately imports the sibling as ``orchestrate``. Register that
# exact module name before importing the remaining tracked formal controls.
orchestrate = load_module("orchestrate", FORMAL_DIR / "orchestrate.py")
freeze = load_module("formal_freeze_under_test", FORMAL_DIR / "freeze.py")
analyze = load_module("formal_analyze_under_test", FORMAL_DIR / "analyze.py")


RUN_ID = "formal-controller-tests"
START_EPOCH = 1_775_088_000.0
SOURCE_REVISION = "1" * 40


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def formal_matrix() -> dict[str, Any]:
    """Return a complete source-only formal matrix fixture.

    The tracked frozen bundle is intentionally absent while source revision S
    is prepared, so controller unit tests must never import it as test data.
    """
    matrix = orchestrate.load_json(freeze.MATRIX_EXAMPLE)
    matrix["benchmarkGitRevision"] = SOURCE_REVISION
    matrix["manifestSha256"] = "2" * 64
    for variant_index, variant in enumerate(matrix["variants"], start=1):
        digit = format((variant_index % 14) + 1, "x")
        for image_index, image in enumerate(variant["expectedImages"], start=1):
            image_digit = format(
                ((variant_index + image_index) % 14) + 1,
                "x",
            )
            image["digest"] = "sha256:" + image_digit * 64
        variant["expectedSanitizedConfigSha256"] = digit * 64
        runtime_digit = format(((variant_index + 7) % 14) + 1, "x")
        variant["expectedSanitizedRuntimeSpecSha256"] = runtime_digit * 64
    return matrix


def iso(offset_seconds: float = 0.0) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(START_EPOCH + offset_seconds, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def runpod_identity(run_id: str = RUN_ID) -> dict[str, Any]:
    image_digest = "sha256:" + "a" * 64
    return {
        "formatVersion": 1,
        "backend": "runpod-ssh",
        "capturedAt": iso(),
        "runId": run_id,
        "sourceRevision": SOURCE_REVISION,
        "remoteRoot": "/artifacts",
        "runpod": {
            "costPerHour": 0.12,
            "cpuFlavorId": "cpu5c",
            "dataCenterId": "EU-CZ-1",
            "image": (
                "ghcr.io/jan-guenter/bluemap-perf-loadgen@" + image_digest
            ),
            "imageDigest": image_digest,
            "machineId": "machine-controller-tests",
            "maxDownloadMbps": 1_000,
            "maxUploadMbps": 500,
            "minDownloadMbps": 500,
            "minUploadMbps": 100,
            "podId": "pod_controller_tests",
            "publicIp": "203.0.113.10",
            "secureCloud": True,
            "vcpuCount": 8,
        },
        "ssh": {
            "host": "203.0.113.10",
            "hostKey": (
                "ssh-ed25519 "
                "AAAAC3NzaC1lZDI1NTE5AAAAIFRlc3RLZXlGb3JCbHVlTWFw"
            ),
            "hostKeyFingerprint": (
                "SHA256:abcdefghijklmnopqrstuvwxyzABCDEFGH0123456789"
            ),
            "port": 22,
            "user": "loadgen",
        },
    }


def runpod_runtime_identity(run_id: str = RUN_ID) -> dict[str, Any]:
    return {
        "formatVersion": 1,
        "capturedAt": iso(1),
        "startedAt": iso(-60),
        "runId": run_id,
        "sourceRevision": SOURCE_REVISION,
        "imageDigest": "sha256:" + "a" * 64,
        "runpod": {
            "configuredVcpuCount": 8,
            "cpuFlavor": "cpu5c",
            "dataCenterId": "EU-CZ-1",
            "podHostname": "loadgen-controller-tests",
            "podId": "pod_controller_tests",
            "publicIp": "203.0.113.10",
            "vcpuCount": 8,
        },
        "runtime": {
            "cgroupVersion": 2,
            "cpu": {
                "affinity": "0-7",
                "affinityCount": 8,
                "cgroupCpuMax": "800000 100000",
                "cpusetEffective": "0-7",
                "cpusetEffectiveCount": 8,
                "effectiveVcpuCount": 8,
                "periodMicros": 100000,
                "quotaMicros": 800000,
                "quotaVcpuCount": 8,
            },
            "hostname": "loadgen-controller-tests",
            "k6Version": "k6 v2.1.0 (go1.25.0, linux/amd64)",
            "kernel": "Linux 6.12.0",
            "memoryBytes": 16 * 1024**3,
            "memoryCapacityBytes": 16 * 1024**3,
            "onlineProcessors": 8,
        },
    }


def runpod_samples(*, saturated: bool = False) -> list[dict[str, Any]]:
    usage_step = 36_000_000 if saturated else 2_000_000
    throttle_step = 1_000
    receive_step = 10_000_000
    transmit_step = 5_000_000
    return [
        {
            "capturedAt": iso(offset),
            "cpuUsageUsec": index * usage_step,
            "cpuThrottledUsec": index * throttle_step,
            "memoryCurrentBytes": 1024**3,
            "network": {
                "rxBytes": index * receive_step,
                "txBytes": index * transmit_step,
            },
        }
        for index, offset in enumerate((0, 5, 10))
    ]


CAPACITY_LIMITS = {
    "maximumSampleGapSeconds": 5.0,
    "maximumP95CpuRatio": 0.70,
    "maximumP95ThrottledCpuRatio": 0.01,
    "maximumMemoryRatio": 0.80,
    "maximumP95NetworkRatio": 0.70,
}


def write_capacity_phase(
    phase_dir: Path,
    samples: list[dict[str, Any]],
    *,
    identity: dict[str, Any] | None = None,
    runtime_identity: dict[str, Any] | None = None,
    passed: bool = True,
) -> dict[str, Any]:
    phase_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = phase_dir / "load-generator-resources.ndjson"
    telemetry_path.write_text(
        "".join(json.dumps(sample, sort_keys=True) + "\n" for sample in samples),
        encoding="utf-8",
    )
    frozen = identity or runpod_identity()
    runtime = runtime_identity or runpod_runtime_identity()
    observed = analyze.recompute_runpod_capacity(samples, frozen, runtime)
    artifact = {
        "formatVersion": 1,
        "limits": CAPACITY_LIMITS,
        "observed": observed,
        "passed": passed,
    }
    write_json(phase_dir / "load-generator-capacity.json", artifact)
    return artifact


def schedule_entry(variant_id: str = "rust-postgresql-r3") -> dict[str, Any]:
    return {
        "sequence": 1,
        "block": 1,
        "entryId": "b1-rust-postgresql-r3-static-r1",
        "runnerCaseId": "formal-b1-rust-postgresql-r3-static-r1",
        "variantId": variant_id,
        "implementation": "rust",
        "storageType": "sql",
        "databaseBackend": "postgresql",
        "replicaCount": 3,
        "profile": "static",
        "rate": 50,
        "viewers": 0,
        "markerIntervalSeconds": 2,
        "minimumAchievedRateRatio": 0.99,
        "traceSeed": 1001,
        "latencyP95Milliseconds": 250,
        "latencyP99Milliseconds": 500,
        "preAllocatedVUs": 20,
        "maxVUs": 100,
        "acceptEncoding": "zstd",
        "storedEncoding": "zstd",
        "contractMode": "enhanced",
        "overloadPolicy": "forbid",
        "warmupDuration": "10s",
        "measurementDuration": "30s",
        "cooldownSeconds": 5,
        "mapIds": ["world"],
    }
