#!/usr/bin/env python3
"""Capture deterministic, unprivileged telemetry for eight SSH L4 lanes."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LANE_ID = re.compile(r"^lane-([1-8])$")
SOCKET_LINK = re.compile(r"^socket:\[([0-9]+)\]$")
HEX_64 = re.compile(r"^[a-f0-9]{64}$")
ESTABLISHED = "01"


class CaptureError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--stop-file", required=True, type=Path)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--expected-remote-address", required=True)
    parser.add_argument("--expected-remote-port", required=True, type=int)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument(
        "--lane",
        action="append",
        default=[],
        metavar="ID=PID",
        help="exact lane identity and its SSH child PID",
    )
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    return parser.parse_args()


def positive_integer(value: str, label: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise CaptureError(f"{label} is not a decimal integer")
    parsed = int(value)
    if parsed <= 0:
        raise CaptureError(f"{label} must be positive")
    return parsed


def parse_lanes(values: list[str]) -> list[tuple[str, int]]:
    lanes: list[tuple[str, int]] = []
    for value in values:
        lane_id, separator, raw_pid = value.partition("=")
        if separator != "=" or LANE_ID.fullmatch(lane_id) is None:
            raise CaptureError(f"invalid lane argument: {value!r}")
        lanes.append((lane_id, positive_integer(raw_pid, f"{lane_id} PID")))
    expected = [f"lane-{index}" for index in range(1, 9)]
    if [lane_id for lane_id, _ in lanes] != expected:
        raise CaptureError(
            "lanes must be supplied exactly once in lane-1..lane-8 order"
        )
    if len({pid for _, pid in lanes}) != 8:
        raise CaptureError("SSH lane PIDs must be distinct")
    return lanes


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def proc_stat(proc_root: Path, pid: int) -> dict[str, int]:
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="ascii")
    except OSError as error:
        raise CaptureError(f"cannot read stat for SSH PID {pid}: {error}") from error
    try:
        _, tail = raw.rsplit(") ", 1)
        fields = tail.split()
        state = fields[0]
        user_ticks = (
            positive_integer(fields[11], f"PID {pid} user ticks")
            if fields[11] != "0"
            else 0
        )
        system_ticks = (
            positive_integer(fields[12], f"PID {pid} system ticks")
            if fields[12] != "0"
            else 0
        )
        start_time_ticks = positive_integer(fields[19], f"PID {pid} start time")
    except (IndexError, ValueError) as error:
        raise CaptureError(f"malformed stat for SSH PID {pid}") from error
    if state == "Z":
        raise CaptureError(f"SSH PID {pid} is a zombie")
    return {
        "startTimeTicks": start_time_ticks,
        "userTicks": user_ticks,
        "systemTicks": system_ticks,
    }


def socket_inodes(proc_root: Path, pid: int) -> set[int]:
    result: set[int] = set()
    try:
        descriptors = list((proc_root / str(pid) / "fd").iterdir())
    except OSError as error:
        raise CaptureError(f"cannot enumerate descriptors for SSH PID {pid}") from error
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        match = SOCKET_LINK.fullmatch(target)
        if match is not None:
            result.add(int(match.group(1)))
    return result


def parse_endpoint(value: str, family: str, label: str) -> dict[str, Any]:
    address, separator, port = value.partition(":")
    if (
        separator != ":"
        or not address
        or len(address) not in {8, 32}
        or re.fullmatch(r"[A-F0-9]+", address) is None
        or re.fullmatch(r"[A-F0-9]{4}", port) is None
    ):
        raise CaptureError(f"{label} endpoint is malformed")
    raw = bytes.fromhex(address)
    if family == "ipv4":
        decoded = raw[::-1]
        address_value = socket.inet_ntop(socket.AF_INET, decoded)
    else:
        decoded = b"".join(raw[index : index + 4][::-1] for index in range(0, 16, 4))
        address_value = socket.inet_ntop(socket.AF_INET6, decoded)
    return {
        "address": str(ipaddress.ip_address(address_value)),
        "addressHex": address,
        "port": int(port, 16),
    }


def tcp_sockets(proc_root: Path) -> dict[int, list[dict[str, Any]]]:
    sockets: dict[int, list[dict[str, Any]]] = {}
    for name, family in (("tcp", "ipv4"), ("tcp6", "ipv6")):
        path = proc_root / "net" / name
        try:
            lines = path.read_text(encoding="ascii").splitlines()
        except OSError as error:
            raise CaptureError(f"cannot read {path}: {error}") from error
        if not lines or "local_address" not in lines[0]:
            raise CaptureError(f"{path} has an unexpected header")
        for line_number, line in enumerate(lines[1:], start=2):
            fields = line.split()
            if len(fields) < 10:
                raise CaptureError(f"{path}:{line_number} has too few fields")
            if fields[3] != ESTABLISHED:
                continue
            if len(fields) < 13:
                raise CaptureError(
                    f"{path}:{line_number} ESTABLISHED row lacks the documented RTO field"
                )
            try:
                state = fields[3]
                tx_raw, rx_raw = fields[4].split(":", 1)
                timer_raw, expires_raw = fields[5].split(":", 1)
                inode = int(fields[9])
                sample = {
                    "inode": inode,
                    "family": family,
                    "stateHex": state,
                    "local": parse_endpoint(
                        fields[1], family, f"{path}:{line_number} local"
                    ),
                    "remote": parse_endpoint(
                        fields[2], family, f"{path}:{line_number} remote"
                    ),
                    "txQueueBytes": int(tx_raw, 16),
                    "rxQueueBytes": int(rx_raw, 16),
                    "timerActive": int(timer_raw, 16),
                    "timerExpiresJiffies": int(expires_raw, 16),
                    "unrecoveredRtoCount": int(fields[6], 16),
                    # Linux documents this post-inode column as the retransmit
                    # timeout. Preserve the raw jiffy value; do not infer time.
                    "retransmitTimeoutJiffies": int(fields[12]),
                }
            except (ValueError, IndexError) as error:
                raise CaptureError(f"{path}:{line_number} is malformed") from error
            if (
                min(
                    sample["txQueueBytes"],
                    sample["rxQueueBytes"],
                    sample["timerActive"],
                    sample["timerExpiresJiffies"],
                    sample["unrecoveredRtoCount"],
                    sample["retransmitTimeoutJiffies"],
                )
                < 0
            ):
                raise CaptureError(f"{path}:{line_number} contains a negative value")
            sockets.setdefault(inode, []).append(sample)
    return sockets


def named_counters(path: Path, prefix: str, required: set[str]) -> dict[str, int]:
    try:
        rows = [
            line.split()
            for line in path.read_text(encoding="ascii").splitlines()
            if line.startswith(f"{prefix}:")
        ]
    except OSError as error:
        raise CaptureError(f"cannot read {path}: {error}") from error
    if len(rows) != 2 or rows[0][0] != rows[1][0] or len(rows[0]) != len(rows[1]):
        raise CaptureError(f"{path} lacks one exact {prefix} header/value pair")
    counters: dict[str, int] = {}
    for name, raw in zip(rows[0][1:], rows[1][1:], strict=True):
        if name not in required:
            continue
        if not raw.isascii() or not raw.isdigit():
            raise CaptureError(f"{path} counter {name} is not non-negative")
        counters[name] = int(raw)
    return counters


def tcp_counters(proc_root: Path) -> dict[str, int]:
    tcp = named_counters(proc_root / "net" / "snmp", "Tcp", {"RetransSegs"})
    ext = named_counters(
        proc_root / "net" / "netstat",
        "TcpExt",
        {"TCPTimeouts", "TCPSynRetrans"},
    )
    try:
        return {
            "retransSegs": tcp["RetransSegs"],
            "tcpTimeouts": ext["TCPTimeouts"],
            "tcpSynRetrans": ext["TCPSynRetrans"],
        }
    except KeyError as error:
        raise CaptureError(f"required TCP counter is unavailable: {error}") from error


def capture(
    proc_root: Path,
    lanes: list[tuple[str, int]],
    identities: dict[str, int],
    source_sha256: str,
    clock_ticks: int,
    expected_remote_address: str,
    expected_remote_port: int,
) -> dict[str, Any]:
    sockets = tcp_sockets(proc_root)
    lane_samples: list[dict[str, Any]] = []
    control_socket_inodes: set[int] = set()
    for lane_id, pid in lanes:
        stat = proc_stat(proc_root, pid)
        if stat["startTimeTicks"] != identities[lane_id]:
            raise CaptureError(f"{lane_id} PID start-time identity changed")
        owned = socket_inodes(proc_root, pid)
        established = [
            socket_sample
            for inode in sorted(owned)
            for socket_sample in sockets.get(inode, [])
            if socket_sample["stateHex"] == ESTABLISHED
        ]
        endpoint_inodes = {
            value["inode"]
            for value in established
            if value["remote"]["address"] == expected_remote_address
            and value["remote"]["port"] == expected_remote_port
        }
        if len(endpoint_inodes) != 1:
            raise CaptureError(
                f"{lane_id} must own exactly one ESTABLISHED TCP socket to the "
                f"frozen remote SSH endpoint; found {len(endpoint_inodes)}"
            )
        control_inode = next(iter(endpoint_inodes))
        if control_inode < 1:
            raise CaptureError("SSH lane control-socket inode is not positive")
        if len(sockets[control_inode]) != 1:
            raise CaptureError(
                f"{lane_id} control-socket inode {control_inode} resolves to "
                f"{len(sockets[control_inode])} ESTABLISHED TCP rows"
            )
        control_socket = sockets[control_inode][0]
        if control_inode in control_socket_inodes:
            raise CaptureError("SSH lane control-socket inodes are not distinct")
        control_socket_inodes.add(control_inode)
        lane_samples.append(
            {
                "id": lane_id,
                "process": {
                    "pid": pid,
                    "startTimeTicks": stat["startTimeTicks"],
                    "userTicks": stat["userTicks"],
                    "systemTicks": stat["systemTicks"],
                },
                "socket": control_socket,
            }
        )
    return {
        "formatVersion": 2,
        "kind": "controller-ssh-transport-sample",
        "capturedAt": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "monotonicNanoseconds": time.monotonic_ns(),
        "sourceSha256": source_sha256,
        "clockTicksPerSecond": clock_ticks,
        "tcp": tcp_counters(proc_root),
        "lanes": lane_samples,
    }


def atomic_ready(path: Path, sample_count: int, source_sha256: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "formatVersion": 1,
                "kind": "controller-ssh-transport-ready",
                "sampleCount": sample_count,
                "sourceSha256": source_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="ascii",
    )
    os.replace(temporary, path)


def main() -> int:
    args = parse_args()
    lanes = parse_lanes(args.lane)
    if HEX_64.fullmatch(args.source_sha256) is None:
        raise CaptureError("source SHA-256 is malformed")
    if sha256(Path(__file__).resolve()) != args.source_sha256:
        raise CaptureError("sampler source fingerprint differs from the invoked file")
    if not (0.1 <= args.interval_seconds <= 5.0):
        raise CaptureError("interval must be between 0.1 and 5 seconds")
    try:
        expected_remote_address = str(
            ipaddress.ip_address(args.expected_remote_address)
        )
    except ValueError as error:
        raise CaptureError("expected remote address is not an IP address") from error
    if not 1 <= args.expected_remote_port <= 65535:
        raise CaptureError("expected remote port is outside 1..65535")
    if (
        args.output.exists()
        or args.output.is_symlink()
        or args.ready_file.exists()
        or args.ready_file.is_symlink()
    ):
        raise CaptureError("unsafe or stale telemetry output/ready path")
    clock_ticks = os.sysconf("SC_CLK_TCK")
    if not isinstance(clock_ticks, int) or clock_ticks <= 0:
        raise CaptureError("SC_CLK_TCK is unavailable")
    identities = {
        lane_id: proc_stat(args.proc_root, pid)["startTimeTicks"]
        for lane_id, pid in lanes
    }

    stop_requested = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    sample_count = 0
    with args.output.open("w", encoding="utf-8") as destination:
        while not stop_requested and not args.stop_file.exists():
            started = time.monotonic()
            sample = capture(
                args.proc_root,
                lanes,
                identities,
                args.source_sha256,
                clock_ticks,
                expected_remote_address,
                args.expected_remote_port,
            )
            destination.write(json.dumps(sample, sort_keys=True, separators=(",", ":")))
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
            sample_count += 1
            if sample_count == 1:
                atomic_ready(args.ready_file, sample_count, args.source_sha256)
            remaining = args.interval_seconds - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    if sample_count < 1:
        raise CaptureError("no controller transport sample was captured")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CaptureError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
