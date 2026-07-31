#!/usr/bin/env python3
"""Validate that a RunPod k6 source was not the measured bottleneck."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("samples", type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--runtime-identity", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--maximum-p95-cpu-ratio", type=float, default=0.70)
    parser.add_argument("--maximum-memory-ratio", type=float, default=0.80)
    parser.add_argument("--maximum-throttled-ratio", type=float, default=0.01)
    parser.add_argument("--maximum-p95-network-ratio", type=float, default=0.70)
    parser.add_argument("--maximum-sample-gap-seconds", type=float, default=5.0)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def load_samples(path: Path) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        samples.append(value)
    if len(samples) < 2:
        raise ValueError("resource telemetry contains fewer than two samples")
    return samples


def number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} is not numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} is not a finite non-negative number")
    return result


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("capturedAt is not a string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    for name, value in (
        ("maximum p95 CPU ratio", args.maximum_p95_cpu_ratio),
        ("maximum memory ratio", args.maximum_memory_ratio),
        ("maximum throttled ratio", args.maximum_throttled_ratio),
        ("maximum p95 network ratio", args.maximum_p95_network_ratio),
    ):
        if not math.isfinite(value) or not 0 < value <= 1:
            raise ValueError(f"{name} must be in (0, 1]")
    if (
        not math.isfinite(args.maximum_sample_gap_seconds)
        or args.maximum_sample_gap_seconds <= 0
    ):
        raise ValueError("maximum sample gap must be positive")

    identity = load_json(args.identity)
    vcpu_count = number(identity["runpod"]["vcpuCount"], "runpod.vcpuCount")
    if (
        vcpu_count != 8
        or identity["runpod"].get("cpuFlavorId") != "cpu5c"
    ):
        raise ValueError("RunPod capacity controls require exact cpu5c with 8 vCPU")
    download_capacity_mbps = number(
        identity["runpod"]["minDownloadMbps"],
        "runpod.minDownloadMbps",
    )
    upload_capacity_mbps = number(
        identity["runpod"]["minUploadMbps"],
        "runpod.minUploadMbps",
    )
    if download_capacity_mbps != 500 or upload_capacity_mbps != 100:
        raise ValueError("RunPod capacity controls require exact 500/100 Mbps floors")
    runtime_identity = load_json(args.runtime_identity)
    cgroup_version = runtime_identity["runtime"].get("cgroupVersion")
    if (
        not isinstance(cgroup_version, int)
        or isinstance(cgroup_version, bool)
        or cgroup_version not in {1, 2}
    ):
        raise ValueError("runtime cgroup version must be 1 or 2")
    runtime_cpu = runtime_identity["runtime"]["cpu"]
    if not isinstance(runtime_cpu, dict):
        raise ValueError("runtime.cpu is not an object")
    for field in ("cpusetEffectiveCount", "affinityCount", "effectiveVcpuCount"):
        value = number(runtime_cpu.get(field), f"runtime.cpu.{field}")
        if field == "effectiveVcpuCount" and value != vcpu_count:
            raise ValueError(
                "runtime effective CPU capacity differs from the frozen 8 vCPU"
            )
        if field != "effectiveVcpuCount" and value < vcpu_count:
            raise ValueError(f"runtime.cpu.{field} exposes fewer than 8 CPUs")
    memory_capacity = number(
        runtime_identity["runtime"]["memoryCapacityBytes"],
        "runtime.memoryCapacityBytes",
    )
    if memory_capacity <= 0:
        raise ValueError("runtime.memoryCapacityBytes must be positive")
    samples = load_samples(args.samples)

    cpu_ratios: list[float] = []
    throttled_ratios: list[float] = []
    gaps: list[float] = []
    rx_mbps: list[float] = []
    tx_mbps: list[float] = []
    memory_bytes: list[float] = []

    previous = samples[0]
    memory_bytes.append(
        number(previous["memoryCurrentBytes"], "memoryCurrentBytes")
    )
    for current in samples[1:]:
        before_time = timestamp(previous["capturedAt"])
        after_time = timestamp(current["capturedAt"])
        elapsed = (after_time - before_time).total_seconds()
        if elapsed <= 0:
            raise ValueError("resource telemetry timestamps are not increasing")
        gaps.append(elapsed)

        usage_delta = number(current["cpuUsageUsec"], "cpuUsageUsec") - number(
            previous["cpuUsageUsec"], "cpuUsageUsec"
        )
        throttle_delta = number(
            current["cpuThrottledUsec"], "cpuThrottledUsec"
        ) - number(previous["cpuThrottledUsec"], "cpuThrottledUsec")
        rx_delta = number(current["network"]["rxBytes"], "network.rxBytes") - number(
            previous["network"]["rxBytes"], "network.rxBytes"
        )
        tx_delta = number(current["network"]["txBytes"], "network.txBytes") - number(
            previous["network"]["txBytes"], "network.txBytes"
        )
        if min(usage_delta, throttle_delta, rx_delta, tx_delta) < 0:
            raise ValueError("a cumulative resource counter decreased")

        capacity_usec = elapsed * 1_000_000 * vcpu_count
        cpu_ratios.append(usage_delta / capacity_usec)
        throttled_ratios.append(throttle_delta / max(usage_delta, 1.0))
        rx_mbps.append(rx_delta * 8 / elapsed / 1_000_000)
        tx_mbps.append(tx_delta * 8 / elapsed / 1_000_000)
        memory_bytes.append(
            number(current["memoryCurrentBytes"], "memoryCurrentBytes")
        )
        previous = current

    memory_ratio = max(memory_bytes) / memory_capacity
    receive_p95_mbps = percentile(rx_mbps, 0.95)
    transmit_p95_mbps = percentile(tx_mbps, 0.95)
    receive_p95_ratio = receive_p95_mbps / download_capacity_mbps
    transmit_p95_ratio = transmit_p95_mbps / upload_capacity_mbps

    observed = {
        "sampleCount": len(samples),
        "maximumSampleGapSeconds": max(gaps),
        "cpuRatio": {
            "p95": percentile(cpu_ratios, 0.95),
            "maximum": max(cpu_ratios),
        },
        "throttledCpuRatio": {
            "p95": percentile(throttled_ratios, 0.95),
            "maximum": max(throttled_ratios),
        },
        "memory": {
            "maximumBytes": max(memory_bytes),
            "capacityBytes": memory_capacity,
            "maximumRatio": memory_ratio,
        },
        "networkMbps": {
            "receiveP95": receive_p95_mbps,
            "receiveMaximum": max(rx_mbps),
            "receiveCapacity": download_capacity_mbps,
            "receiveP95Ratio": receive_p95_ratio,
            "transmitP95": transmit_p95_mbps,
            "transmitMaximum": max(tx_mbps),
            "transmitCapacity": upload_capacity_mbps,
            "transmitP95Ratio": transmit_p95_ratio,
        },
    }
    passed = (
        observed["maximumSampleGapSeconds"] <= args.maximum_sample_gap_seconds
        and observed["cpuRatio"]["p95"] <= args.maximum_p95_cpu_ratio
        and observed["throttledCpuRatio"]["p95"]
        <= args.maximum_throttled_ratio
        and memory_ratio <= args.maximum_memory_ratio
        and receive_p95_ratio <= args.maximum_p95_network_ratio
        and transmit_p95_ratio <= args.maximum_p95_network_ratio
    )
    output = {
        "formatVersion": 1,
        "limits": {
            "maximumSampleGapSeconds": args.maximum_sample_gap_seconds,
            "maximumP95CpuRatio": args.maximum_p95_cpu_ratio,
            "maximumP95ThrottledCpuRatio": args.maximum_throttled_ratio,
            "maximumMemoryRatio": args.maximum_memory_ratio,
            "maximumP95NetworkRatio": args.maximum_p95_network_ratio,
        },
        "observed": observed,
        "passed": passed,
    }
    atomic_json(args.output, output)
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
