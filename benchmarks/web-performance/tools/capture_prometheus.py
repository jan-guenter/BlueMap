#!/usr/bin/env python3
"""Capture a bounded Prometheus query_range bundle for one benchmark case."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_RANGE_SECONDS = 24 * 60 * 60
KUBERNETES_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
ROLE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture exact-pod, exact-node, and PostgreSQL metrics with "
            "bounded Prometheus query_range calls."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect-url",
        help="validate a Prometheus URL and describe a Kubernetes Service URL",
    )
    inspect_parser.add_argument("url")

    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--base-url", required=True)
    capture_parser.add_argument(
        "--source-url",
        help="stable source URL to record when base-url is a local port-forward",
    )
    capture_parser.add_argument("--start", required=True, type=int)
    capture_parser.add_argument("--end", required=True, type=int)
    capture_parser.add_argument("--step", default=15, type=int)
    capture_parser.add_argument("--namespace", required=True)
    capture_parser.add_argument(
        "--pod",
        action="append",
        default=[],
        metavar="ROLE=POD",
        help="exact target Pod and its role; repeat for all targets",
    )
    capture_parser.add_argument(
        "--node",
        action="append",
        default=[],
        help="exact Kubernetes node hosting a selected Pod; repeat as needed",
    )
    capture_parser.add_argument(
        "--phase-events",
        required=True,
        type=Path,
        help="runner phases.ndjson used for per-repetition noise assessment",
    )
    capture_parser.add_argument(
        "--max-non-target-node-cpu-range-cores",
        default=0.5,
        type=float,
    )
    capture_parser.add_argument(
        "--max-non-target-node-cpu-mean-cores",
        default=2.0,
        type=float,
    )
    capture_parser.add_argument(
        "--max-non-target-node-cpu-maximum-cores",
        default=3.0,
        type=float,
    )
    capture_parser.add_argument("--output", required=True, type=Path)
    capture_parser.add_argument("--timeout", default=60, type=int)
    return parser.parse_args(argv)


def inspect_url(value: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Prometheus URL scheme must be http or https")
    if not parsed.hostname:
        raise ValueError("Prometheus URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Prometheus URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Prometheus URL must not contain a query or fragment")

    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"Invalid Prometheus URL port: {error}") from error

    path = parsed.path.rstrip("/")
    normalized = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    hostname = parsed.hostname.rstrip(".").lower()
    labels = hostname.split(".")
    cluster_service = None
    if (
        len(labels) in {3, 5}
        and labels[2] == "svc"
        and (len(labels) == 3 or labels[3:] == ["cluster", "local"])
    ):
        service, namespace = labels[:2]
        if not KUBERNETES_NAME.fullmatch(service):
            raise ValueError("Prometheus Service name is invalid")
        if not KUBERNETES_NAME.fullmatch(namespace):
            raise ValueError("Prometheus Service namespace is invalid")
        cluster_service = {
            "service": service,
            "namespace": namespace,
            "port": port or (443 if parsed.scheme == "https" else 80),
            "path": path,
        }

    return {
        "baseUrl": normalized,
        "scheme": parsed.scheme,
        "hostname": parsed.hostname,
        "port": port or (443 if parsed.scheme == "https" else 80),
        "path": path,
        "clusterService": cluster_service,
    }


def parse_pod_targets(values: Sequence[str]) -> list[dict[str, str]]:
    if not values:
        raise ValueError("At least one --pod ROLE=POD target is required")

    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        role, separator, pod = value.partition("=")
        if not separator or not ROLE_NAME.fullmatch(role):
            raise ValueError(f"Invalid Pod target role in {value!r}")
        if not KUBERNETES_NAME.fullmatch(pod):
            raise ValueError(f"Invalid Pod name in {value!r}")
        if not pod.startswith("bluemap-perf-"):
            raise ValueError(f"Pod {pod!r} must be an exact bluemap-perf-* target")
        if pod in seen:
            raise ValueError(f"Duplicate Pod target {pod!r}")
        seen.add(pod)
        targets.append({"role": role, "pod": pod})
    return targets


def parse_nodes(values: Sequence[str]) -> list[str]:
    if not values:
        raise ValueError("At least one exact --node target is required")
    nodes = sorted(set(values))
    if len(nodes) != len(values):
        raise ValueError("Duplicate --node targets are not allowed")
    for node in nodes:
        if not KUBERNETES_NAME.fullmatch(node):
            raise ValueError(f"Invalid Kubernetes node name {node!r}")
    return nodes


def promql_string(value: str) -> str:
    """Return a PromQL-compatible quoted string."""

    return json.dumps(value, ensure_ascii=True)


def exact_label_regex(values: Sequence[str], label: str) -> str:
    if not values:
        raise ValueError(f"At least one {label} is required for a Prometheus query")
    escaped = [re.sub(r"([\\.^$|?*+()[\]{}])", r"\\\1", value) for value in values]
    return rf"^(?:{'|'.join(escaped)})$"


def exact_pod_regex(pods: Sequence[str]) -> str:
    return exact_label_regex(pods, "Pod")


def metric_selector(
    namespace: str,
    pods: Sequence[str],
    *,
    containers: bool = False,
) -> str:
    matchers = [
        f"namespace={promql_string(namespace)}",
        f"pod=~{promql_string(exact_pod_regex(pods))}",
    ]
    if containers:
        matchers.extend(['container!=""', 'container!="POD"'])
    return "{" + ",".join(matchers) + "}"


def build_queries(
    namespace: str,
    targets: Sequence[dict[str, str]],
    nodes: Sequence[str],
) -> list[dict[str, str]]:
    all_pods = [target["pod"] for target in targets]
    database_pods = [
        target["pod"] for target in targets if target["role"] == "database"
    ]
    postgres_pods = [pod for pod in database_pods if "postgres" in pod]
    all_selector = metric_selector(namespace, all_pods, containers=True)
    network_selector = metric_selector(namespace, all_pods)
    node_regex = promql_string(exact_label_regex(nodes, "node"))
    all_pod_regex = promql_string(exact_pod_regex(all_pods))
    node_container_selector = (
        "{"
        f'node=~{node_regex},container!="",container!="POD"'
        "}"
    )
    non_target_node_container_selector = (
        "{"
        f'node=~{node_regex},pod!~{all_pod_regex},'
        'container!="",container!="POD"'
        "}"
    )
    node_network_selector = (
        "{" f'node=~{node_regex},namespace!=""' "}"
    )
    uname_selector = "node_uname_info{" f"nodename=~{node_regex}" "}"

    queries = [
        {
            "name": "container_cpu_cores",
            "scope": "all-target-pods",
            "query": (
                "sum by (pod, container) "
                f"(rate(container_cpu_usage_seconds_total{all_selector}[1m]))"
            ),
        },
        {
            "name": "container_memory_working_set_bytes",
            "scope": "all-target-pods",
            "query": (
                "sum by (pod, container) "
                f"(container_memory_working_set_bytes{all_selector})"
            ),
        },
        {
            "name": "container_cpu_throttled_seconds_rate",
            "scope": "all-target-pods",
            "query": (
                "sum by (pod, container) "
                "(rate(container_cpu_cfs_throttled_seconds_total"
                f"{all_selector}[1m]))"
            ),
        },
        {
            "name": "container_cpu_throttled_period_ratio",
            "scope": "all-target-pods",
            "query": (
                "(sum by (pod, container) "
                "(rate(container_cpu_cfs_throttled_periods_total"
                f"{all_selector}[1m]))) / "
                "(sum by (pod, container) "
                "(rate(container_cpu_cfs_periods_total"
                f"{all_selector}[1m])))"
            ),
        },
        {
            "name": "pod_network_receive_bytes_rate",
            "scope": "all-target-pods",
            "query": (
                "sum by (pod) "
                f"(rate(container_network_receive_bytes_total{network_selector}[1m]))"
            ),
        },
        {
            "name": "pod_network_transmit_bytes_rate",
            "scope": "all-target-pods",
            "query": (
                "sum by (pod) "
                f"(rate(container_network_transmit_bytes_total{network_selector}[1m]))"
            ),
        },
        {
            "name": "node_container_cpu_cores",
            "scope": "selected-nodes",
            "query": (
                "sum by (node) "
                "(rate(container_cpu_usage_seconds_total"
                f"{node_container_selector}[1m]))"
            ),
        },
        {
            "name": "node_non_target_container_cpu_cores",
            "scope": "selected-nodes-excluding-target-pods",
            "query": (
                "sum by (node) "
                "(rate(container_cpu_usage_seconds_total"
                f"{non_target_node_container_selector}[1m]))"
            ),
        },
        {
            "name": "node_container_cpu_throttled_seconds_rate",
            "scope": "selected-nodes",
            "query": (
                "sum by (node) "
                "(rate(container_cpu_cfs_throttled_seconds_total"
                f"{node_container_selector}[1m]))"
            ),
        },
        {
            "name": "node_container_cpu_throttled_period_ratio",
            "scope": "selected-nodes",
            "query": (
                "(sum by (node) "
                "(rate(container_cpu_cfs_throttled_periods_total"
                f"{node_container_selector}[1m]))) / "
                "(sum by (node) "
                "(rate(container_cpu_cfs_periods_total"
                f"{node_container_selector}[1m])))"
            ),
        },
        {
            "name": "node_container_network_receive_bytes_rate",
            "scope": "selected-nodes",
            "query": (
                "sum by (node) "
                "(rate(container_network_receive_bytes_total"
                f"{node_network_selector}[1m]))"
            ),
        },
        {
            "name": "node_container_network_transmit_bytes_rate",
            "scope": "selected-nodes",
            "query": (
                "sum by (node) "
                "(rate(container_network_transmit_bytes_total"
                f"{node_network_selector}[1m]))"
            ),
        },
        {
            "name": "node_cpu_idle_steal_cores",
            "scope": "selected-node-exporters",
            "query": (
                "sum by (instance, nodename, mode) ("
                'rate(node_cpu_seconds_total{mode=~"^(?:idle|steal)$"}[1m]) '
                "* on(instance) group_left(nodename) "
                f"{uname_selector})"
            ),
        },
        {
            "name": "node_disk_read_bytes_rate",
            "scope": "selected-node-exporters",
            "query": (
                "sum by (instance, nodename, device) ("
                "rate(node_disk_read_bytes_total[1m]) "
                "* on(instance) group_left(nodename) "
                f"{uname_selector})"
            ),
        },
        {
            "name": "node_disk_written_bytes_rate",
            "scope": "selected-node-exporters",
            "query": (
                "sum by (instance, nodename, device) ("
                "rate(node_disk_written_bytes_total[1m]) "
                "* on(instance) group_left(nodename) "
                f"{uname_selector})"
            ),
        },
        {
            "name": "node_disk_io_seconds_rate",
            "scope": "selected-node-exporters",
            "query": (
                "sum by (instance, nodename, device) ("
                "rate(node_disk_io_time_seconds_total[1m]) "
                "* on(instance) group_left(nodename) "
                f"{uname_selector})"
            ),
        },
        {
            "name": "node_network_receive_bytes_rate",
            "scope": "selected-node-exporters",
            "query": (
                "sum by (instance, nodename, device) ("
                'rate(node_network_receive_bytes_total{device!="lo"}[1m]) '
                "* on(instance) group_left(nodename) "
                f"{uname_selector})"
            ),
        },
        {
            "name": "node_network_transmit_bytes_rate",
            "scope": "selected-node-exporters",
            "query": (
                "sum by (instance, nodename, device) ("
                'rate(node_network_transmit_bytes_total{device!="lo"}[1m]) '
                "* on(instance) group_left(nodename) "
                f"{uname_selector})"
            ),
        },
    ]

    if not postgres_pods:
        return queries

    database_selector = metric_selector(namespace, postgres_pods)
    postgres_metrics = [
        ("postgres_connections", "pg_stat_database_numbackends", "gauge"),
        ("postgres_xact_commit_rate", "pg_stat_database_xact_commit", "rate"),
        (
            "postgres_xact_rollback_rate",
            "pg_stat_database_xact_rollback",
            "rate",
        ),
        ("postgres_blocks_read_rate", "pg_stat_database_blks_read", "rate"),
        ("postgres_blocks_hit_rate", "pg_stat_database_blks_hit", "rate"),
        (
            "postgres_statements_calls_rate",
            "pg_stat_statements_calls",
            "rate",
        ),
        (
            "postgres_statements_exec_time_rate",
            "pg_stat_statements_total_exec_time",
            "rate",
        ),
        (
            "postgres_statements_exec_seconds_rate",
            "pg_stat_statements_total_exec_time_seconds",
            "rate",
        ),
    ]
    for name, metric, metric_type in postgres_metrics:
        expression = f"{metric}{database_selector}"
        if metric_type == "rate":
            expression = f"rate({expression}[1m])"
        queries.append(
            {
                "name": name,
                "scope": "database-pods",
                "query": f"sum by (pod, datname) ({expression})",
            }
        )
    return queries


def query_range(
    base_url: str,
    query: str,
    start: int,
    end: int,
    step: int,
    timeout: int,
) -> dict[str, Any]:
    parameters = urllib.parse.urlencode(
        {
            "query": query,
            "start": start,
            "end": end,
            "step": step,
        }
    )
    url = f"{base_url.rstrip('/')}/api/v1/query_range?{parameters}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "BlueMap-Performance-Prometheus-Capture/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Prometheus query failed: {error}") from error

    if payload.get("status") != "success":
        error_type = payload.get("errorType", "unknown")
        error_message = payload.get("error", "unknown Prometheus error")
        raise RuntimeError(f"Prometheus returned {error_type}: {error_message}")
    return payload


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def parse_timestamp(value: str) -> float:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized).timestamp()


def measurement_windows(path: Path) -> list[dict[str, Any]]:
    events: dict[int, dict[str, float]] = {}
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid phase event JSON on line {line_number}: {error}"
                ) from error
            if event.get("phase") != "measurement":
                continue
            repetition = event.get("repetition")
            event_name = event.get("event")
            if (
                not isinstance(repetition, int)
                or repetition < 1
                or event_name not in {"start", "end"}
                or not isinstance(event.get("timestamp"), str)
            ):
                raise ValueError(f"Invalid measurement phase event on line {line_number}")
            repetition_events = events.setdefault(repetition, {})
            if event_name in repetition_events:
                raise ValueError(
                    f"Duplicate measurement {event_name} for repetition {repetition}"
                )
            repetition_events[event_name] = parse_timestamp(event["timestamp"])

    windows = []
    for repetition, repetition_events in sorted(events.items()):
        if set(repetition_events) != {"start", "end"}:
            raise ValueError(
                f"Incomplete measurement phase events for repetition {repetition}"
            )
        if repetition_events["end"] <= repetition_events["start"]:
            raise ValueError(
                f"Invalid measurement time range for repetition {repetition}"
            )
        windows.append(
            {
                "repetition": repetition,
                "start": repetition_events["start"],
                "end": repetition_events["end"],
            }
        )
    if not windows:
        raise ValueError("No complete measurement phase windows were recorded")
    return windows


def assess_node_noise(
    results: Sequence[dict[str, Any]],
    nodes: Sequence[str],
    windows: Sequence[dict[str, Any]],
    maximum_range_cores: float,
    maximum_mean_cores: float,
    maximum_level_cores: float,
) -> dict[str, Any]:
    if min(maximum_range_cores, maximum_mean_cores, maximum_level_cores) <= 0:
        raise ValueError("Non-target node CPU thresholds must be positive")
    query = next(
        (
            result
            for result in results
            if result["name"] == "node_non_target_container_cpu_cores"
        ),
        None,
    )
    if query is None:
        raise ValueError("Non-target node CPU query is missing")

    series_by_node: dict[str, list[tuple[float, float]]] = {
        node: [] for node in nodes
    }
    response_series = query["response"].get("data", {}).get("result", [])
    for series in response_series:
        node = series.get("metric", {}).get("node")
        if node not in series_by_node:
            continue
        for raw_timestamp, raw_value in series.get("values", []):
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            series_by_node[node].append((float(raw_timestamp), value))

    repetitions = []
    noisy_repetitions = []
    for window in windows:
        node_results = []
        repetition_noisy = False
        for node in nodes:
            values = [
                value
                for timestamp, value in series_by_node[node]
                if window["start"] <= timestamp <= window["end"]
            ]
            complete = len(values) >= 2
            observed_range = max(values) - min(values) if values else None
            observed_mean = sum(values) / len(values) if values else None
            observed_maximum = max(values) if values else None
            noisy = not complete or (
                observed_range is not None
                and (
                    observed_range > maximum_range_cores
                    or observed_mean > maximum_mean_cores
                    or observed_maximum > maximum_level_cores
                )
            )
            repetition_noisy = repetition_noisy or noisy
            node_results.append(
                {
                    "node": node,
                    "samples": len(values),
                    "minimumCores": min(values) if values else None,
                    "maximumCores": max(values) if values else None,
                    "meanCores": observed_mean,
                    "rangeCores": observed_range,
                    "complete": complete,
                    "noisy": noisy,
                }
            )
        if repetition_noisy:
            noisy_repetitions.append(window["repetition"])
        repetitions.append(
            {
                **window,
                "nodes": node_results,
                "noisy": repetition_noisy,
            }
        )

    return {
        "metric": "node_non_target_container_cpu_cores",
        "maximumRangeCores": maximum_range_cores,
        "maximumMeanCores": maximum_mean_cores,
        "maximumLevelCores": maximum_level_cores,
        "repetitions": repetitions,
        "noisyRepetitions": noisy_repetitions,
        "passed": not noisy_repetitions,
    }


def capture(args: argparse.Namespace) -> None:
    inspected = inspect_url(args.base_url)
    source = inspect_url(args.source_url) if args.source_url else inspected
    if not KUBERNETES_NAME.fullmatch(args.namespace):
        raise ValueError("Invalid Kubernetes namespace")
    if args.start < 0 or args.end < args.start:
        raise ValueError("Prometheus range end must be at or after its start")
    if args.end - args.start > MAX_RANGE_SECONDS:
        raise ValueError(
            f"Prometheus range must not exceed {MAX_RANGE_SECONDS} seconds"
        )
    if not 1 <= args.step <= 3600:
        raise ValueError("Prometheus step must be between 1 and 3600 seconds")
    if not 1 <= args.timeout <= 600:
        raise ValueError("Prometheus timeout must be between 1 and 600 seconds")

    if min(
        args.max_non_target_node_cpu_range_cores,
        args.max_non_target_node_cpu_mean_cores,
        args.max_non_target_node_cpu_maximum_cores,
    ) <= 0:
        raise ValueError("Non-target node CPU thresholds must be positive")

    targets = parse_pod_targets(args.pod)
    nodes = parse_nodes(args.node)
    windows = measurement_windows(args.phase_events)
    queries = build_queries(args.namespace, targets, nodes)
    results = []
    for query in queries:
        results.append(
            {
                **query,
                "response": query_range(
                    inspected["baseUrl"],
                    query["query"],
                    args.start,
                    args.end,
                    args.step,
                    args.timeout,
                ),
            }
        )

    bundle = {
        "capturedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "prometheus": {"baseUrl": source["baseUrl"]},
        "range": {
            "start": args.start,
            "end": args.end,
            "stepSeconds": args.step,
        },
        "namespace": args.namespace,
        "targets": targets,
        "nodes": nodes,
        "nodeNoise": assess_node_noise(
            results,
            nodes,
            windows,
            args.max_non_target_node_cpu_range_cores,
            args.max_non_target_node_cpu_mean_cores,
            args.max_non_target_node_cpu_maximum_cores,
        ),
        "queries": results,
    }
    atomic_write_json(args.output, bundle)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "inspect-url":
            json.dump(inspect_url(args.url), sys.stdout, sort_keys=True)
            sys.stdout.write("\n")
        else:
            capture(args)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"PROMETHEUS CAPTURE FAILURE: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
