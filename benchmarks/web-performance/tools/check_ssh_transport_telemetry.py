#!/usr/bin/env python3
"""Fail closed on malformed SSH-L4 telemetry and emit diagnostic summaries."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

HEX_64 = re.compile(r"^[a-f0-9]{64}$")
RUNPOD_CUMULATIVE = (
    "qmax",
    "smax",
    "stot",
    "bin",
    "bout",
    "econ",
    "eresp",
    "wretr",
    "wredis",
)
HAPROXY_KEYS = {
    "id",
    "serverName",
    "qcur",
    "qmax",
    "scur",
    "smax",
    "stot",
    "bin",
    "bout",
    "econ",
    "eresp",
    "wretr",
    "wredis",
    "status",
}


class ValidationError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("transport", type=Path)
    parser.add_argument("controller_samples", type=Path)
    parser.add_argument("runpod_samples", type=Path)
    parser.add_argument("--controller-sampler", required=True, type=Path)
    parser.add_argument("--runpod-sampler", required=True, type=Path)
    parser.add_argument("--expected-remote-address", required=True)
    parser.add_argument("--expected-remote-port", required=True, type=int)
    parser.add_argument("--expected-runpod-image-digest", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--maximum-sample-gap-seconds", type=float, default=5.0)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular(path: Path, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValidationError(f"{label} is missing, not regular, or a symlink")


def load_object(path: Path, label: str) -> dict[str, Any]:
    regular(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot load {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{label} is not an object")
    return value


def load_ndjson(path: Path, label: str) -> list[dict[str, Any]]:
    regular(path, label)
    samples: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValidationError(f"cannot read {label}: {error}") from error
    for line_number, line in enumerate(lines, 1):
        if not line:
            raise ValidationError(f"{label}:{line_number} is blank")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValidationError(f"{label}:{line_number} is invalid JSON") from error
        if not isinstance(value, dict):
            raise ValidationError(f"{label}:{line_number} is not an object")
        samples.append(value)
    if len(samples) < 2:
        raise ValidationError(f"{label} has fewer than two samples")
    return samples


def timestamp(value: Any, label: str) -> float:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError(f"{label} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValidationError(f"{label} is not a timestamp") from error
    return parsed.timestamp()


def uint(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} is not a non-negative integer")
    return value


def exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError(f"{label} has an unexpected schema")
    return value


def verify_chronology(
    samples: list[dict[str, Any]],
    label: str,
    start: float,
    end: float,
    maximum_gap: float,
) -> tuple[list[float], float]:
    epochs = [
        timestamp(sample.get("capturedAt"), f"{label}[{index}].capturedAt")
        for index, sample in enumerate(samples)
    ]
    gaps = [right - left for left, right in zip(epochs, epochs[1:], strict=False)]
    if any(gap <= 0 or gap > maximum_gap for gap in gaps):
        raise ValidationError(
            f"{label} timestamps are not increasing within the gap limit"
        )
    if (
        epochs[0] < start - maximum_gap
        or epochs[0] > start
        or epochs[-1] < end - maximum_gap
        or epochs[-1] > end + maximum_gap
    ):
        raise ValidationError(f"{label} does not cover both workload-window edges")
    return epochs, max(gaps)


def delta(
    first: dict[str, Any], last: dict[str, Any], fields: Iterable[str], label: str
) -> dict[str, int]:
    result: dict[str, int] = {}
    for field in fields:
        before = uint(first.get(field), f"{label}.{field}[first]")
        after = uint(last.get(field), f"{label}.{field}[last]")
        if after < before:
            raise ValidationError(f"{label}.{field} decreased")
        result[field] = after - before
    return result


def validate_descriptor(
    transport: dict[str, Any],
    name: str,
    samples_path: Path,
    samples: list[dict[str, Any]],
    expected_source_sha256: str,
    expected_remote_address: str,
    expected_remote_port: int,
    expected_runpod_image_digest: str,
) -> dict[str, Any]:
    telemetry = exact(
        transport.get("telemetry"),
        {"formatVersion", "required", "intervalSeconds", "controller", "runpod"},
        "transport.telemetry",
    )
    if (
        telemetry.get("formatVersion") != 2
        or telemetry.get("required") is not True
        or isinstance(telemetry.get("intervalSeconds"), bool)
        or not isinstance(telemetry.get("intervalSeconds"), (int, float))
        or telemetry.get("intervalSeconds") != 1
    ):
        raise ValidationError("transport telemetry controls are invalid")
    descriptor = exact(
        telemetry.get(name),
        {"path", "sha256", "sampleCount", "source", "capture"},
        f"transport.telemetry.{name}",
    )
    if descriptor.get("sha256") != sha256(samples_path) or descriptor.get(
        "sampleCount"
    ) != len(samples):
        raise ValidationError(
            f"{name} telemetry hash/count differs from its descriptor"
        )
    if HEX_64.fullmatch(str(descriptor.get("sha256"))) is None:
        raise ValidationError(f"{name} telemetry digest is malformed")
    path = descriptor.get("path")
    if (
        not isinstance(path, str)
        or re.fullmatch(r"/artifacts/[A-Za-z0-9._/-]+", path) is None
        or "/../" in path
        or path.endswith("/..")
        or "//" in path
    ):
        raise ValidationError(f"{name} telemetry path is malformed")
    if name == "controller":
        source = exact(
            descriptor.get("source"),
            {"kind", "samplerSha256", "remoteAddress", "remotePort"},
            "controller telemetry source",
        )
        if (
            source.get("kind") != "controller-procfs-ssh-lanes-v2"
            or source.get("remoteAddress") != expected_remote_address
            or source.get("remotePort") != expected_remote_port
        ):
            raise ValidationError("controller telemetry source endpoint is invalid")
        capture_keys = {
            "attempted",
            "validBeforeWorkload",
            "readyAt",
            "reaped",
            "exitStatus",
            "persisted",
        }
    else:
        source = exact(
            descriptor.get("source"),
            {"kind", "imageDigest", "samplerSha256", "statsSocket"},
            "RunPod telemetry source",
        )
        image_digest = source.get("imageDigest")
        if (
            source.get("kind") != "runpod-haproxy-procfs-v1"
            or source.get("statsSocket") != "/run/haproxy/bluemap-stats.sock"
            or image_digest != expected_runpod_image_digest
        ):
            raise ValidationError("RunPod telemetry source identity is invalid")
        capture_keys = {
            "attempted",
            "validBeforeWorkload",
            "readyAt",
            "workloadReleasedAt",
            "reaped",
            "exitStatus",
            "persisted",
            "observedSourceSha256",
        }
    if source.get("samplerSha256") != expected_source_sha256:
        raise ValidationError(
            f"{name} sampler fingerprint differs from the frozen source"
        )
    capture = exact(
        descriptor.get("capture"), capture_keys, f"{name} telemetry capture"
    )
    required_flags = ("attempted", "validBeforeWorkload", "reaped", "persisted")
    if (
        any(capture.get(field) is not True for field in required_flags)
        or isinstance(capture.get("exitStatus"), bool)
        or not isinstance(capture.get("exitStatus"), int)
        or capture.get("exitStatus") != 0
    ):
        raise ValidationError(f"{name} telemetry capture was not complete")
    timestamp(capture.get("readyAt"), f"{name} capture readyAt")
    return descriptor


def decode_proc_address(address_hex: str, family: str, label: str) -> str:
    expected_length = 8 if family == "ipv4" else 32
    if re.fullmatch(rf"[A-F0-9]{{{expected_length}}}", address_hex) is None:
        raise ValidationError(f"{label} addressHex is malformed")
    raw = bytes.fromhex(address_hex)
    if family == "ipv4":
        decoded = raw[::-1]
        value = socket.inet_ntop(socket.AF_INET, decoded)
    else:
        decoded = b"".join(raw[index : index + 4][::-1] for index in range(0, 16, 4))
        value = socket.inet_ntop(socket.AF_INET6, decoded)
    return str(ipaddress.ip_address(value))


def validate_controller(
    samples: list[dict[str, Any]],
    transport: dict[str, Any],
    expected_source: str,
    start: float,
    end: float,
    maximum_gap: float,
    expected_remote_address: str,
    expected_remote_port: int,
) -> dict[str, Any]:
    _, observed_gap = verify_chronology(
        samples, "controller telemetry", start, end, maximum_gap
    )
    lane_evidence = transport.get("lanes")
    if not isinstance(lane_evidence, list) or len(lane_evidence) != 8:
        raise ValidationError("transport lane identities are unavailable")
    expected_identity: dict[str, tuple[int, int]] = {}
    for index, lane in enumerate(lane_evidence, 1):
        if not isinstance(lane, dict) or lane.get("id") != f"lane-{index}":
            raise ValidationError("transport lane order/identity is invalid")
        process = exact(
            lane.get("process"),
            {"pid", "startTimeTicks"},
            f"transport lane-{index} process",
        )
        pid = uint(process.get("pid"), f"transport lane-{index} pid")
        start_ticks = uint(
            process.get("startTimeTicks"),
            f"transport lane-{index} startTimeTicks",
        )
        if pid < 1 or start_ticks < 1:
            raise ValidationError("transport process identity is not positive")
        expected_identity[f"lane-{index}"] = (pid, start_ticks)
    if len({pid for pid, _ in expected_identity.values()}) != 8:
        raise ValidationError("transport SSH lane PIDs are not distinct")
    previous_monotonic = -1
    first_lanes: dict[str, dict[str, Any]] = {}
    previous_lanes: dict[str, dict[str, Any]] = {}
    maximum_queues = {
        f"lane-{index}": {
            "txBytes": 0,
            "rxBytes": 0,
            "rtoJiffies": 0,
            "unrecoveredRtoCount": 0,
        }
        for index in range(1, 9)
    }
    first_tcp: dict[str, Any] | None = None
    previous_tcp: dict[str, Any] | None = None
    last_tcp: dict[str, Any] | None = None
    clock_ticks: int | None = None
    socket_identities: dict[str, tuple[Any, ...]] = {}
    for sample_index, sample in enumerate(samples):
        sample_socket_inodes: set[int] = set()
        exact(
            sample,
            {
                "formatVersion",
                "kind",
                "capturedAt",
                "monotonicNanoseconds",
                "sourceSha256",
                "clockTicksPerSecond",
                "tcp",
                "lanes",
            },
            f"controller[{sample_index}]",
        )
        if (
            sample.get("formatVersion") != 2
            or sample.get("kind") != "controller-ssh-transport-sample"
            or sample.get("sourceSha256") != expected_source
        ):
            raise ValidationError("controller sample identity/fingerprint is invalid")
        monotonic = uint(
            sample.get("monotonicNanoseconds"), "controller.monotonicNanoseconds"
        )
        if monotonic <= previous_monotonic:
            raise ValidationError("controller monotonic clock did not increase")
        previous_monotonic = monotonic
        current_ticks = uint(
            sample.get("clockTicksPerSecond"), "controller.clockTicksPerSecond"
        )
        if current_ticks < 1 or (
            clock_ticks is not None and current_ticks != clock_ticks
        ):
            raise ValidationError("controller clock-tick identity changed")
        clock_ticks = current_ticks
        tcp = exact(
            sample.get("tcp"),
            {"retransSegs", "tcpTimeouts", "tcpSynRetrans"},
            "controller.tcp",
        )
        for field in tcp:
            uint(tcp[field], f"controller.tcp.{field}")
        if previous_tcp is not None:
            delta(previous_tcp, tcp, tcp.keys(), "controller.tcp")
        if first_tcp is None:
            first_tcp = tcp
        previous_tcp = tcp
        last_tcp = tcp
        lanes = sample.get("lanes")
        if not isinstance(lanes, list) or [
            lane.get("id") if isinstance(lane, dict) else None for lane in lanes
        ] != [f"lane-{index}" for index in range(1, 9)]:
            raise ValidationError("controller lane order/identity is invalid")
        for lane in lanes:
            lane_id = lane["id"]
            exact(lane, {"id", "process", "socket"}, f"controller.{lane_id}")
            process = exact(
                lane.get("process"),
                {"pid", "startTimeTicks", "userTicks", "systemTicks"},
                f"controller.{lane_id}.process",
            )
            identity = (
                uint(process.get("pid"), "pid"),
                uint(process.get("startTimeTicks"), "startTimeTicks"),
            )
            if identity != expected_identity.get(lane_id):
                raise ValidationError(
                    f"{lane_id} process fingerprint differs from transport"
                )
            for field in ("userTicks", "systemTicks"):
                uint(process.get(field), f"controller.{lane_id}.{field}")
            socket = exact(
                lane.get("socket"),
                {
                    "inode",
                    "family",
                    "stateHex",
                    "local",
                    "remote",
                    "txQueueBytes",
                    "rxQueueBytes",
                    "timerActive",
                    "timerExpiresJiffies",
                    "unrecoveredRtoCount",
                    "retransmitTimeoutJiffies",
                },
                f"controller.{lane_id}.socket",
            )
            if (
                socket.get("family") not in {"ipv4", "ipv6"}
                or socket.get("stateHex") != "01"
            ):
                raise ValidationError(f"{lane_id} socket is not ESTABLISHED")
            inode = uint(socket.get("inode"), "inode")
            if inode < 1:
                raise ValidationError("controller SSH control-socket inode is invalid")
            if inode in sample_socket_inodes:
                raise ValidationError("controller SSH control-socket inodes overlap")
            sample_socket_inodes.add(inode)
            endpoint_identity: list[Any] = [inode, socket.get("family")]
            for endpoint_name in ("local", "remote"):
                endpoint = exact(
                    socket.get(endpoint_name),
                    {"address", "addressHex", "port"},
                    f"{lane_id}.{endpoint_name}",
                )
                address = endpoint.get("address")
                address_hex = endpoint.get("addressHex")
                if not isinstance(address, str) or not isinstance(address_hex, str):
                    raise ValidationError(
                        f"{lane_id} {endpoint_name} address is malformed"
                    )
                try:
                    normalized = str(ipaddress.ip_address(address))
                except ValueError as error:
                    raise ValidationError(
                        f"{lane_id} {endpoint_name} address is malformed"
                    ) from error
                if (
                    normalized != address
                    or decode_proc_address(
                        address_hex,
                        socket["family"],
                        f"{lane_id}.{endpoint_name}",
                    )
                    != address
                ):
                    raise ValidationError(
                        f"{lane_id} {endpoint_name} address/hex identity differs"
                    )
                port = uint(endpoint.get("port"), f"{lane_id}.{endpoint_name}.port")
                if port > 65535:
                    raise ValidationError(f"{lane_id} {endpoint_name} port is invalid")
                if endpoint_name == "remote" and (
                    address != expected_remote_address or port != expected_remote_port
                ):
                    raise ValidationError(
                        f"{lane_id} remote endpoint differs from frozen identity"
                    )
                endpoint_identity.extend((address, address_hex, port))
            for field in (
                "txQueueBytes",
                "rxQueueBytes",
                "timerActive",
                "timerExpiresJiffies",
                "unrecoveredRtoCount",
                "retransmitTimeoutJiffies",
            ):
                uint(socket.get(field), f"controller.{lane_id}.{field}")
            stable = tuple(endpoint_identity)
            if lane_id in socket_identities and socket_identities[lane_id] != stable:
                raise ValidationError(f"{lane_id} socket identity changed")
            socket_identities[lane_id] = stable
            if lane_id in previous_lanes:
                delta(
                    previous_lanes[lane_id]["process"],
                    process,
                    ("userTicks", "systemTicks"),
                    f"controller.{lane_id}.cpu",
                )
            if lane_id not in first_lanes:
                first_lanes[lane_id] = lane
            previous_lanes[lane_id] = lane
            maximum_queues[lane_id]["txBytes"] = max(
                maximum_queues[lane_id]["txBytes"], socket["txQueueBytes"]
            )
            maximum_queues[lane_id]["rxBytes"] = max(
                maximum_queues[lane_id]["rxBytes"], socket["rxQueueBytes"]
            )
            maximum_queues[lane_id]["rtoJiffies"] = max(
                maximum_queues[lane_id]["rtoJiffies"],
                socket["retransmitTimeoutJiffies"],
            )
            maximum_queues[lane_id]["unrecoveredRtoCount"] = max(
                maximum_queues[lane_id]["unrecoveredRtoCount"],
                socket["unrecoveredRtoCount"],
            )
    assert first_tcp is not None and last_tcp is not None and clock_ticks is not None
    lane_summary: dict[str, Any] = {}
    for lane_id in sorted(first_lanes):
        before = first_lanes[lane_id]
        after = previous_lanes[lane_id]
        lane_summary[lane_id] = {
            "cpuTickDelta": delta(
                before["process"],
                after["process"],
                ("userTicks", "systemTicks"),
                f"controller.{lane_id}.cpu",
            ),
            "maximumQueueBytes": maximum_queues[lane_id],
        }
    return {
        "sampleCount": len(samples),
        "maximumSampleGapSeconds": observed_gap,
        "clockTicksPerSecond": clock_ticks,
        "tcpCounterDelta": delta(
            first_tcp, last_tcp, first_tcp.keys(), "controller.tcp"
        ),
        "lanes": lane_summary,
    }


def validate_haproxy(
    value: Any, expected_id: str, expected_server: str, label: str
) -> dict[str, Any]:
    stat = exact(value, HAPROXY_KEYS, label)
    if (
        stat.get("id") != expected_id
        or stat.get("serverName") != expected_server
        or not isinstance(stat.get("status"), str)
        or not stat["status"]
    ):
        raise ValidationError(f"{label} identity/status is invalid")
    for field in HAPROXY_KEYS - {"id", "serverName", "status"}:
        uint(stat.get(field), f"{label}.{field}")
    return stat


def validate_runpod(
    samples: list[dict[str, Any]],
    expected_source: str,
    start: float,
    end: float,
    maximum_gap: float,
) -> dict[str, Any]:
    _, observed_gap = verify_chronology(
        samples, "RunPod telemetry", start, end, maximum_gap
    )
    previous_resource: dict[str, Any] | None = None
    first_tcp: dict[str, Any] | None = None
    previous_tcp: dict[str, Any] | None = None
    last_tcp: dict[str, Any] | None = None
    first_stats: dict[str, dict[str, Any]] = {}
    previous_stats: dict[str, dict[str, Any]] = {}
    maximums: dict[str, dict[str, int]] = {}
    statuses: dict[str, set[str]] = {}
    for sample_index, sample in enumerate(samples):
        exact(
            sample,
            {
                "formatVersion",
                "kind",
                "capturedAt",
                "sourceSha256",
                "cpuUsageUsec",
                "cpuThrottledUsec",
                "memoryCurrentBytes",
                "network",
                "transport",
            },
            f"runpod[{sample_index}]",
        )
        if (
            sample.get("formatVersion") != 2
            or sample.get("kind") != "runpod-resource-transport-sample"
            or sample.get("sourceSha256") != expected_source
        ):
            raise ValidationError("RunPod sample identity/fingerprint is invalid")
        resource = {
            "cpuUsageUsec": uint(sample.get("cpuUsageUsec"), "cpuUsageUsec"),
            "cpuThrottledUsec": uint(
                sample.get("cpuThrottledUsec"), "cpuThrottledUsec"
            ),
        }
        uint(sample.get("memoryCurrentBytes"), "memoryCurrentBytes")
        network = exact(sample.get("network"), {"rxBytes", "txBytes"}, "runpod.network")
        resource.update(
            {field: uint(network.get(field), f"network.{field}") for field in network}
        )
        if previous_resource is not None:
            delta(previous_resource, resource, resource.keys(), "runpod.resources")
        previous_resource = resource
        transport = exact(
            sample.get("transport"), {"tcp", "haproxy"}, "runpod.transport"
        )
        tcp = exact(
            transport.get("tcp"),
            {"retransSegs", "tcpTimeouts", "tcpSynRetrans"},
            "runpod.tcp",
        )
        for field in tcp:
            uint(tcp[field], f"runpod.tcp.{field}")
        if previous_tcp is not None:
            delta(previous_tcp, tcp, tcp.keys(), "runpod.tcp")
        if first_tcp is None:
            first_tcp = tcp
        previous_tcp = tcp
        last_tcp = tcp
        haproxy = exact(
            transport.get("haproxy"), {"backend", "lanes"}, "runpod.haproxy"
        )
        stats = [
            validate_haproxy(
                haproxy.get("backend"), "backend", "BACKEND", "haproxy.backend"
            )
        ]
        lanes = haproxy.get("lanes")
        if not isinstance(lanes, list) or len(lanes) != 8:
            raise ValidationError("HAProxy lane stats are incomplete")
        stats.extend(
            validate_haproxy(
                lane, f"lane-{index}", f"lane_{index}", f"haproxy.lane-{index}"
            )
            for index, lane in enumerate(lanes, 1)
        )
        for stat in stats:
            stat_id = stat["id"]
            if stat_id in previous_stats:
                delta(
                    previous_stats[stat_id],
                    stat,
                    RUNPOD_CUMULATIVE,
                    f"haproxy.{stat_id}",
                )
            if stat_id not in first_stats:
                first_stats[stat_id] = stat
                maximums[stat_id] = {"qcur": 0, "scur": 0}
                statuses[stat_id] = set()
            previous_stats[stat_id] = stat
            maximums[stat_id]["qcur"] = max(maximums[stat_id]["qcur"], stat["qcur"])
            maximums[stat_id]["scur"] = max(maximums[stat_id]["scur"], stat["scur"])
            statuses[stat_id].add(stat["status"])
    assert first_tcp is not None and last_tcp is not None
    stats_summary = {
        stat_id: {
            "counterDelta": delta(
                first_stats[stat_id],
                previous_stats[stat_id],
                RUNPOD_CUMULATIVE,
                f"haproxy.{stat_id}",
            ),
            "maximumCurrentQueue": maximums[stat_id]["qcur"],
            "maximumCurrentSessions": maximums[stat_id]["scur"],
            "statuses": sorted(statuses[stat_id]),
        }
        for stat_id in sorted(first_stats)
    }
    lane_connections = [
        stats_summary[f"lane-{index}"]["counterDelta"]["stot"] for index in range(1, 9)
    ]
    return {
        "sampleCount": len(samples),
        "maximumSampleGapSeconds": observed_gap,
        "tcpCounterDelta": delta(first_tcp, last_tcp, first_tcp.keys(), "runpod.tcp"),
        "haproxy": stats_summary,
        "laneConnectionDeltaImbalance": {
            "minimum": min(lane_connections),
            "maximum": max(lane_connections),
            "range": max(lane_connections) - min(lane_connections),
        },
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def main() -> int:
    args = parse_args()
    if args.maximum_sample_gap_seconds != 5.0:
        raise ValidationError("maximum sample gap is fixed at five seconds")
    try:
        expected_remote_address = str(
            ipaddress.ip_address(args.expected_remote_address)
        )
    except ValueError as error:
        raise ValidationError("expected remote address is not an IP address") from error
    if not 1 <= args.expected_remote_port <= 65535:
        raise ValidationError("expected remote port is outside 1..65535")
    if re.fullmatch(r"sha256:[a-f0-9]{64}", args.expected_runpod_image_digest) is None:
        raise ValidationError("expected RunPod image digest is malformed")
    transport = load_object(args.transport, "transport evidence")
    if (
        transport.get("formatVersion") != 2
        or transport.get("kind") != "ssh-l4-traefik-transport"
        or transport.get("passed") is not True
    ):
        raise ValidationError("transport v2 did not pass before telemetry validation")
    receipt = transport.get("commandSession", {}).get("receipt")
    if not isinstance(receipt, dict) or receipt.get("formatVersion") != 2:
        raise ValidationError("transport command receipt v2 is missing")
    receipt_telemetry = exact(
        receipt.get("telemetry"),
        {
            "resourceOutput",
            "readyBeforeWorkload",
            "readyAt",
            "workloadReleasedAt",
            "samplerExitStatus",
        },
        "command receipt telemetry",
    )
    if (
        receipt_telemetry.get("readyBeforeWorkload") is not True
        or isinstance(receipt_telemetry.get("samplerExitStatus"), bool)
        or not isinstance(receipt_telemetry.get("samplerExitStatus"), int)
        or receipt_telemetry.get("samplerExitStatus") != 0
    ):
        raise ValidationError("command receipt telemetry did not pass")
    ready = timestamp(receipt_telemetry.get("readyAt"), "telemetry readyAt")
    start = timestamp(receipt_telemetry.get("workloadReleasedAt"), "workloadReleasedAt")
    end = timestamp(receipt.get("completedAt"), "command completedAt")
    if ready > start or start > end:
        raise ValidationError("telemetry workload window is reversed")
    regular(args.controller_sampler, "controller sampler source")
    regular(args.runpod_sampler, "RunPod sampler source")
    if args.output.exists() or args.output.is_symlink():
        raise ValidationError("validation output already exists or is unsafe")
    controller_source = sha256(args.controller_sampler)
    runpod_source = sha256(args.runpod_sampler)
    controller_samples = load_ndjson(args.controller_samples, "controller telemetry")
    runpod_samples = load_ndjson(args.runpod_samples, "RunPod telemetry")
    controller_descriptor = validate_descriptor(
        transport,
        "controller",
        args.controller_samples,
        controller_samples,
        controller_source,
        expected_remote_address,
        args.expected_remote_port,
        args.expected_runpod_image_digest,
    )
    runpod_descriptor = validate_descriptor(
        transport,
        "runpod",
        args.runpod_samples,
        runpod_samples,
        runpod_source,
        expected_remote_address,
        args.expected_remote_port,
        args.expected_runpod_image_digest,
    )
    if receipt_telemetry.get("resourceOutput") != runpod_descriptor.get("path"):
        raise ValidationError(
            "command receipt resource output differs from RunPod telemetry path"
        )
    controller_ready = timestamp(
        controller_descriptor["capture"]["readyAt"], "controller readyAt"
    )
    runpod_ready = timestamp(runpod_descriptor["capture"]["readyAt"], "RunPod readyAt")
    runpod_release = runpod_descriptor["capture"].get("workloadReleasedAt")
    if (
        controller_ready > start
        or runpod_ready > start
        or runpod_release != receipt["telemetry"]["workloadReleasedAt"]
    ):
        raise ValidationError("telemetry readiness/release chronology is invalid")
    if (
        runpod_descriptor.get("capture", {}).get("observedSourceSha256")
        != runpod_source
    ):
        raise ValidationError(
            "RunPod observed sampler fingerprint differs from frozen source"
        )
    output = {
        "formatVersion": 2,
        "kind": "ssh-l4-transport-telemetry-validation",
        "window": {
            "startedAt": receipt["telemetry"]["workloadReleasedAt"],
            "finishedAt": receipt["completedAt"],
        },
        "limits": {"maximumSampleGapSeconds": args.maximum_sample_gap_seconds},
        "sources": {
            "controllerSamplerSha256": controller_source,
            "runpodSamplerSha256": runpod_source,
            "remoteEndpoint": {
                "address": expected_remote_address,
                "port": args.expected_remote_port,
            },
        },
        "artifacts": {
            "transportSha256": sha256(args.transport),
            "controllerSha256": sha256(args.controller_samples),
            "runpodSha256": sha256(args.runpod_samples),
        },
        "diagnostics": {
            "controller": validate_controller(
                controller_samples,
                transport,
                controller_source,
                start,
                end,
                args.maximum_sample_gap_seconds,
                expected_remote_address,
                args.expected_remote_port,
            ),
            "runpod": validate_runpod(
                runpod_samples,
                runpod_source,
                start,
                end,
                args.maximum_sample_gap_seconds,
            ),
        },
        "capturePassed": True,
        "performanceGateApplied": False,
        "passed": True,
    }
    atomic_json(args.output, output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        KeyError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        ValidationError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
