#!/usr/bin/env python3
"""Run a small, direct-origin BlueMap map-data throughput comparison."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import http.client
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


FORMAT_VERSION = 1
MEBIBYTE = 1024 * 1024
MAX_SETUP_MANIFEST_BYTES = 1024 * 1024
MAX_PATH_LENGTH = 4096
VARIANTS = ("upstream", "upstream-php", "new-java")
DURATION_PATTERN = re.compile(r"^(?:[1-9][0-9]*)(?:ms|s|m|h)$")
ENCODING_PATTERN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
SAFE_PATH_PATTERN = re.compile(r"^/maps/[A-Za-z0-9._~!$&'()*+,;=:@/-]+$")


class BenchmarkError(RuntimeError):
    """Raised when benchmark inputs or evidence are invalid."""


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    artifact_id: str


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare fixed-concurrency map-data throughput for the upstream "
            "server, upstream SQL PHP endpoint, and the new Java server."
        )
    )
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--upstream-php-url", required=True)
    parser.add_argument("--new-java-url", required=True)
    parser.add_argument(
        "--upstream-id",
        required=True,
        help="Exact upstream revision or immutable artifact identifier",
    )
    parser.add_argument(
        "--new-java-id",
        required=True,
        help="Exact new Java revision or immutable artifact identifier",
    )
    parser.add_argument(
        "--dataset-id",
        required=True,
        help="Immutable database snapshot identifier or SHA-256",
    )
    parser.add_argument(
        "--setup-manifest",
        required=True,
        type=Path,
        help="JSON manifest describing the comparable target setup",
    )
    parser.add_argument("--paths", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--vus", type=int, default=12)
    parser.add_argument("--warmup-duration", default="15s")
    parser.add_argument("--duration", default="60s")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--accept-encoding", default="zstd")
    parser.add_argument("--required-content-encoding", default="zstd")
    parser.add_argument("--preflight-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--k6", default="k6", help="Path or name of the k6 binary")
    return parser


def validate_identifier(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 256 or any(
        character in normalized for character in "\r\n\0"
    ):
        raise BenchmarkError(f"{name} must be 1-256 characters without line breaks")
    return normalized


def validate_url(value: str, name: str) -> str:
    if value != value.strip() or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        raise BenchmarkError(f"{name} must not contain whitespace or control characters")
    normalized = value.rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BenchmarkError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise BenchmarkError(f"{name} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise BenchmarkError(f"{name} must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise BenchmarkError(f"{name} must be an origin URL without a path")
    try:
        parsed.port
    except ValueError as error:
        raise BenchmarkError(f"{name} has an invalid port") from error
    return normalized


def origin_key(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    assert parsed.hostname is not None
    default_port = 80 if parsed.scheme == "http" else 443
    return parsed.scheme, parsed.hostname.lower(), parsed.port or default_port


def validate_duration(value: str, name: str) -> str:
    if not DURATION_PATTERN.fullmatch(value):
        raise BenchmarkError(f"{name} must be an integer followed by ms, s, m, or h")
    return value


def duration_seconds(value: str) -> float:
    match = re.fullmatch(r"([1-9][0-9]*)(ms|s|m|h)", value)
    if match is None:
        raise BenchmarkError(f"invalid duration: {value}")
    magnitude = int(match.group(1))
    return magnitude * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def validate_encoding(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if not ENCODING_PATTERN.fullmatch(normalized):
        raise BenchmarkError(f"{name} must be one HTTP content-coding token")
    return normalized


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value {value!r} is not allowed")


def _required_manifest_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise BenchmarkError(f"setup manifest {name} must be a nonempty string")
    if any(character in value for character in "\0"):
        raise BenchmarkError(f"setup manifest {name} must not contain NUL")
    return value.strip()


def load_setup_manifest(path: Path, dataset_id: str) -> tuple[dict[str, Any], bytes]:
    try:
        if path.is_symlink():
            raise BenchmarkError("--setup-manifest must not be a symbolic link")
        stat = path.stat()
        if not path.is_file():
            raise BenchmarkError("--setup-manifest must be a regular file")
        if stat.st_size <= 0 or stat.st_size > MAX_SETUP_MANIFEST_BYTES:
            raise BenchmarkError(
                f"--setup-manifest must be 1-{MAX_SETUP_MANIFEST_BYTES} bytes"
            )
        raw = path.read_bytes()
        if len(raw) <= 0 or len(raw) > MAX_SETUP_MANIFEST_BYTES:
            raise BenchmarkError(
                f"--setup-manifest must be 1-{MAX_SETUP_MANIFEST_BYTES} bytes"
            )
    except BenchmarkError:
        raise
    except OSError as error:
        raise BenchmarkError(f"failed to read setup manifest {path}: {error}") from error

    try:
        manifest = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise BenchmarkError(f"invalid setup manifest {path}: {error}") from error
    if not isinstance(manifest, dict):
        raise BenchmarkError("setup manifest root must be a JSON object")
    if manifest.get("formatVersion") != 1:
        raise BenchmarkError("setup manifest formatVersion must be 1")

    _required_manifest_string(manifest.get("environment"), "environment")
    _required_manifest_string(manifest.get("protocol"), "protocol")

    database = manifest.get("database")
    if not isinstance(database, dict):
        raise BenchmarkError("setup manifest database must be an object")
    snapshot_id = _required_manifest_string(
        database.get("snapshotId"), "database.snapshotId"
    )
    if snapshot_id != dataset_id:
        raise BenchmarkError(
            "setup manifest database.snapshotId must equal --dataset-id"
        )
    connection_ceiling = database.get("aggregateConnectionCeiling")
    if (
        isinstance(connection_ceiling, bool)
        or not isinstance(connection_ceiling, int)
        or connection_ceiling <= 0
    ):
        raise BenchmarkError(
            "setup manifest database.aggregateConnectionCeiling must be a positive integer"
        )

    resource_limits = manifest.get("resourceLimits")
    if not isinstance(resource_limits, dict):
        raise BenchmarkError("setup manifest resourceLimits must be an object")
    normalized_limits: list[tuple[str, str]] = []
    for variant in VARIANTS:
        limits = resource_limits.get(variant)
        if not isinstance(limits, dict):
            raise BenchmarkError(
                f"setup manifest resourceLimits.{variant} must be an object"
            )
        normalized_limits.append(
            (
                _required_manifest_string(
                    limits.get("cpu"), f"resourceLimits.{variant}.cpu"
                ),
                _required_manifest_string(
                    limits.get("memory"), f"resourceLimits.{variant}.memory"
                ),
            )
        )
    if len(set(normalized_limits)) != 1:
        raise BenchmarkError(
            "setup manifest must declare identical CPU and memory limits for all targets"
        )

    targets = manifest.get("targets")
    if not isinstance(targets, dict):
        raise BenchmarkError("setup manifest targets must be an object")
    for variant in VARIANTS:
        target = targets.get(variant)
        if not isinstance(target, dict):
            raise BenchmarkError(f"setup manifest targets.{variant} must be an object")
        _required_manifest_string(target.get("runtime"), f"targets.{variant}.runtime")
        _required_manifest_string(
            target.get("configuration"), f"targets.{variant}.configuration"
        )

    return manifest, raw


def parse_paths(path_file: Path) -> list[str]:
    try:
        lines = path_file.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise BenchmarkError(f"failed to read path file {path_file}: {error}") from error

    paths: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        path = line.strip()
        if not path or path.startswith("#"):
            continue
        if not path.startswith("/maps/"):
            raise BenchmarkError(
                f"{path_file}:{line_number}: only /maps/... paths are allowed"
            )
        if "?" in path or "#" in path:
            raise BenchmarkError(
                f"{path_file}:{line_number}: queries and fragments are not allowed"
            )
        if (
            "%" in path
            or "\\" in path
            or not SAFE_PATH_PATTERN.fullmatch(path)
            or any(
                not segment or segment in {".", ".."}
                for segment in path.split("/")[1:]
            )
        ):
            raise BenchmarkError(
                f"{path_file}:{line_number}: path is not canonical URL-safe /maps data"
            )
        if any(ord(character) < 0x21 or ord(character) == 0x7F for character in path):
            raise BenchmarkError(
                f"{path_file}:{line_number}: whitespace/control characters are not allowed"
            )
        if path in seen:
            raise BenchmarkError(f"{path_file}:{line_number}: duplicate path {path!r}")
        seen.add(path)
        paths.append(path)
        if len(path) > MAX_PATH_LENGTH:
            raise BenchmarkError(
                f"{path_file}:{line_number}: path exceeds {MAX_PATH_LENGTH} characters"
            )

    if not paths:
        raise BenchmarkError(f"{path_file} contains no request paths")
    if len(paths) > 4096:
        raise BenchmarkError(f"{path_file} contains more than 4096 paths")
    return paths


def frozen_path_text(paths: Iterable[str]) -> str:
    return "".join(f"{path}\n" for path in paths)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_content_encoding(value: str | None) -> str:
    if value is None or not value.strip():
        return "identity"
    return value.strip().lower()


def preflight(
    targets: Sequence[Target],
    paths: Sequence[str],
    accept_encoding: str,
    required_content_encoding: str,
    timeout_seconds: float,
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    evidence: dict[str, Any] = {
        "formatVersion": FORMAT_VERSION,
        "status": "running",
        "valid": False,
        "paths": {},
    }

    def persist() -> None:
        if evidence_path is not None:
            write_json(evidence_path, evidence)

    def fail(message: str, entry: dict[str, Any]) -> None:
        entry["valid"] = False
        entry["error"] = message
        evidence["status"] = "failed"
        evidence["error"] = message
        persist()
        raise BenchmarkError(message)

    persist()

    for path in paths:
        reference_digest: str | None = None
        reference_size: int | None = None
        path_evidence: dict[str, Any] = {}
        evidence["paths"][path] = path_evidence
        for target in targets:
            entry: dict[str, Any] = {}
            path_evidence[target.name] = entry
            request = urllib.request.Request(
                f"{target.url}{path}",
                headers={
                    "Accept-Encoding": accept_encoding,
                    "User-Agent": "BlueMap-Throughput-Preflight/1",
                },
                method="GET",
            )
            try:
                with opener.open(request, timeout=timeout_seconds) as response:
                    status = response.status
                    body = response.read()
                    headers = response.headers
            except urllib.error.HTTPError as error:
                entry["status"] = error.code
                message = f"preflight {target.name} {path}: HTTP {error.code}"
                try:
                    fail(message, entry)
                except BenchmarkError as benchmark_error:
                    raise benchmark_error from error
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                TimeoutError,
                OSError,
            ) as error:
                message = f"preflight {target.name} {path}: request failed: {error}"
                try:
                    fail(message, entry)
                except BenchmarkError as benchmark_error:
                    raise benchmark_error from error

            encoding = normalize_content_encoding(headers.get("Content-Encoding"))
            digest = sha256_bytes(body)
            entry.update(
                {
                    "status": status,
                    "contentEncoding": encoding,
                    "contentType": headers.get("Content-Type"),
                    "contentLength": len(body),
                    "sha256": digest,
                }
            )
            if status != 200:
                fail(
                    f"preflight {target.name} {path}: expected HTTP 200, got {status}",
                    entry,
                )
            if encoding != required_content_encoding:
                fail(
                    f"preflight {target.name} {path}: expected Content-Encoding "
                    f"{required_content_encoding!r}, got {encoding!r}",
                    entry,
                )
            if reference_digest is None:
                reference_digest = digest
                reference_size = len(body)
            elif digest != reference_digest or len(body) != reference_size:
                fail(
                    f"preflight {target.name} {path}: body differs from {targets[0].name}",
                    entry,
                )

            entry["valid"] = True
            persist()

    evidence["status"] = "completed"
    evidence["valid"] = True
    persist()
    return evidence


def rotated_targets(targets: Sequence[Target], repetition: int) -> list[Target]:
    if not targets:
        return []
    offset = (repetition - 1) % len(targets)
    return list(targets[offset:]) + list(targets[:offset])


def metric_values(summary: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    metrics = summary.get("metrics")
    if not isinstance(metrics, Mapping):
        raise BenchmarkError("k6 summary has no metrics object")
    metric = metrics.get(name)
    if not isinstance(metric, Mapping):
        raise BenchmarkError(f"k6 summary has no {name!r} metric")
    values = metric.get("values")
    if not isinstance(values, Mapping):
        raise BenchmarkError(f"k6 metric {name!r} has no values object")
    return values


def finite_metric(
    values: Mapping[str, Any],
    key: str,
    metric_name: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
    integer: bool = False,
) -> float | int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkError(f"k6 metric {metric_name!r} value {key!r} is not numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise BenchmarkError(f"k6 metric {metric_name!r} value {key!r} is not finite")
    if numeric < minimum or (maximum is not None and numeric > maximum):
        raise BenchmarkError(f"k6 metric {metric_name!r} value {key!r} is out of range")
    if integer:
        if not numeric.is_integer():
            raise BenchmarkError(
                f"k6 metric {metric_name!r} value {key!r} is not an integer"
            )
        return int(numeric)
    return numeric


def extract_metrics(summary: Mapping[str, Any]) -> dict[str, float | int]:
    requests = metric_values(summary, "http_reqs")
    received = metric_values(summary, "data_received")
    duration = metric_values(summary, "http_req_duration")
    failed = metric_values(summary, "http_req_failed")
    benchmark_errors = metric_values(summary, "benchmark_errors")

    request_count = finite_metric(
        requests, "count", "http_reqs", minimum=1.0, integer=True
    )
    request_rate = finite_metric(requests, "rate", "http_reqs", minimum=0.0)
    if request_rate <= 0:
        raise BenchmarkError("k6 metric 'http_reqs' rate must be positive")
    return {
        "requests": request_count,
        "requestsPerSecond": request_rate,
        "mibPerSecond": finite_metric(
            received, "rate", "data_received", minimum=0.0
        )
        / MEBIBYTE,
        "p95Milliseconds": finite_metric(
            duration, "p(95)", "http_req_duration", minimum=0.0
        ),
        "httpFailureRate": finite_metric(
            failed, "rate", "http_req_failed", minimum=0.0, maximum=1.0
        ),
        "errors": finite_metric(
            benchmark_errors,
            "count",
            "benchmark_errors",
            minimum=0.0,
            integer=True,
        ),
    }


def run_k6(
    *,
    k6_binary: str,
    script: Path,
    target: Target,
    path_file: Path,
    vus: int,
    duration: str,
    accept_encoding: str,
    required_content_encoding: str,
    summary_path: Path,
    log_path: Path,
) -> tuple[int, dict[str, float | int]]:
    environment = os.environ.copy()
    environment.update(
        {
            "BASE_URL": target.url,
            "PATH_FILE": str(path_file),
            "VARIANT": target.name,
            "ACCEPT_ENCODING": accept_encoding,
            "REQUIRED_CONTENT_ENCODING": required_content_encoding,
            "VUS": str(vus),
            "DURATION": duration,
            "K6_NO_USAGE_REPORT": "true",
            # The comparison is explicitly direct-origin. Do not inherit a
            # workstation or CI HTTP proxy into the measured path.
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "NO_PROXY": "*",
        }
    )
    command = [
        k6_binary,
        "run",
        "--quiet",
        "--summary-export",
        str(summary_path),
        str(script),
    ]
    timeout = duration_seconds(duration) + 120
    try:
        completed = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as error:
        log_path.write_text(
            f"command: {command!r}\nresult: failed to execute: {error}\n",
            encoding="utf-8",
        )
        raise BenchmarkError(f"failed to execute k6 for {target.name}: {error}") from error
    except subprocess.TimeoutExpired as error:
        log_path.write_text(
            f"command: {command!r}\nresult: timeout after {timeout}s\n"
            f"stdout:\n{error.stdout or ''}\nstderr:\n{error.stderr or ''}\n",
            encoding="utf-8",
        )
        raise BenchmarkError(
            f"k6 timed out for {target.name} after {timeout} seconds"
        ) from error

    log_path.write_text(
        f"command: {command!r}\nexitCode: {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}\n",
        encoding="utf-8",
    )
    if not summary_path.is_file():
        raise BenchmarkError(
            f"k6 produced no summary for {target.name}; see {log_path}"
        )
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkError(
            f"failed to read k6 summary {summary_path}: {error}"
        ) from error
    if not isinstance(summary, Mapping):
        raise BenchmarkError(f"k6 summary {summary_path} root is not an object")
    return completed.returncode, extract_metrics(summary)


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def measurement_is_valid(entry: Mapping[str, Any]) -> bool:
    basic_valid = (
        entry.get("status") == "completed"
        and entry.get("k6ExitCode") == 0
        and entry.get("warmupExitCode") == 0
        and entry.get("errors") == 0
        and entry.get("httpFailureRate") == 0
    )
    if not basic_valid:
        return False
    numeric_fields = (
        "requestsPerSecond",
        "mibPerSecond",
        "p95Milliseconds",
        "httpFailureRate",
    )
    return (
        isinstance(entry.get("requests"), int)
        and not isinstance(entry.get("requests"), bool)
        and entry["requests"] > 0
        and all(
            isinstance(entry.get(field), (int, float))
            and not isinstance(entry.get(field), bool)
            and math.isfinite(float(entry[field]))
            and float(entry[field]) >= 0
            for field in numeric_fields
        )
    )


def summarize_measurements(
    output: Path,
    runs: Sequence[Mapping[str, Any]],
    expected_repetitions: int,
) -> bool:
    if expected_repetitions <= 0:
        raise BenchmarkError("expected repetitions must be positive")
    measurements = [run for run in runs if run["phase"] == "measurement"]
    fields = [
        "status",
        "error",
        "variant",
        "repetition",
        "orderPosition",
        "requests",
        "requestsPerSecond",
        "mibPerSecond",
        "p95Milliseconds",
        "httpFailureRate",
        "errors",
        "warmupExitCode",
        "k6ExitCode",
        "summaryFile",
        "logFile",
    ]
    with (output / "measurements.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for measurement in measurements:
            writer.writerow({field: measurement.get(field) for field in fields})

    variants: dict[str, list[Mapping[str, Any]]] = {}
    for measurement in measurements:
        variants.setdefault(str(measurement["variant"]), []).append(measurement)

    summaries: list[dict[str, Any]] = []
    all_valid = set(variants).issubset(VARIANTS)
    for variant in VARIANTS:
        entries = variants.get(variant, [])
        failed_runs = sum(1 for entry in entries if not measurement_is_valid(entry))
        repetitions = [entry.get("repetition") for entry in entries]
        shape_valid = (
            len(entries) == expected_repetitions
            and all(
                isinstance(repetition, int) and not isinstance(repetition, bool)
                for repetition in repetitions
            )
            and sorted(repetitions) == list(range(1, expected_repetitions + 1))
        )
        variant_valid = shape_valid and failed_runs == 0
        all_valid = all_valid and variant_valid
        summary: dict[str, Any] = {
            "variant": variant,
            "repetitions": len(entries),
            "expectedRepetitions": expected_repetitions,
            "missingRuns": max(0, expected_repetitions - len(entries)),
            "failedRuns": failed_runs,
            "totalErrors": sum(
                int(entry["errors"])
                for entry in entries
                if isinstance(entry.get("errors"), int)
                and not isinstance(entry.get("errors"), bool)
            ),
        }
        summaries.append(summary)

    if all_valid:
        for summary in summaries:
            entries = variants[summary["variant"]]
            summary.update(
                {
                    "medianRequestsPerSecond": statistics.median(
                        float(entry["requestsPerSecond"]) for entry in entries
                    ),
                    "minimumRequestsPerSecond": min(
                        float(entry["requestsPerSecond"]) for entry in entries
                    ),
                    "maximumRequestsPerSecond": max(
                        float(entry["requestsPerSecond"]) for entry in entries
                    ),
                    "medianMibPerSecond": statistics.median(
                        float(entry["mibPerSecond"]) for entry in entries
                    ),
                    "medianP95Milliseconds": statistics.median(
                        float(entry["p95Milliseconds"]) for entry in entries
                    ),
                }
            )

    write_json(
        output / "summary.json",
        {"formatVersion": FORMAT_VERSION, "valid": all_valid, "variants": summaries},
    )
    summary_fields = [
        "variant",
        "repetitions",
        "expectedRepetitions",
        "missingRuns",
        "medianRequestsPerSecond",
        "minimumRequestsPerSecond",
        "maximumRequestsPerSecond",
        "medianMibPerSecond",
        "medianP95Milliseconds",
        "totalErrors",
        "failedRuns",
    ]
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summaries)

    lines = [
        "# Throughput summary",
        "",
        "| Variant | Repetitions | Median req/s | Median MiB/s | Median p95 (ms) | Errors | Failed runs |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for summary in summaries:
        if all_valid:
            lines.append(
                "| {variant} | {repetitions} | {medianRequestsPerSecond:.2f} | "
                "{medianMibPerSecond:.2f} | {medianP95Milliseconds:.2f} | "
                "{totalErrors} | {failedRuns} |".format(**summary)
            )
        else:
            lines.append(
                "| {variant} | {repetitions}/{expectedRepetitions} | — | — | — | "
                "{totalErrors} | {failedRuns} |".format(**summary)
            )
    lines.extend(
        [
            "",
            "Valid: **{}**".format("yes" if all_valid else "no"),
            "",
            (
                "Aggregates use the complete valid measurement matrix."
                if all_valid
                else "No aggregate metrics are published because the matrix is incomplete "
                "or contains a failed run. Partial and failed runs remain in the evidence."
            ),
        ]
    )
    (output / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return all_valid


def default_output(script_directory: Path) -> Path:
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return script_directory / "results" / timestamp


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    script_directory = Path(__file__).resolve().parent
    output = (arguments.output or default_output(script_directory)).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except (FileExistsError, OSError) as error:
        print(
            f"error: failed to create result directory {output}: {error}",
            file=sys.stderr,
        )
        return 2

    started_at = dt.datetime.now(dt.UTC).isoformat()
    metadata: dict[str, Any] = {
        "formatVersion": FORMAT_VERSION,
        "startedAt": started_at,
        "status": "initializing",
        "valid": False,
    }
    runs: list[dict[str, Any]] = []
    expected_repetitions = (
        arguments.repetitions
        if isinstance(arguments.repetitions, int) and arguments.repetitions > 0
        else 0
    )
    try:
        write_json(output / "metadata.json", metadata)
        write_json(output / "runs.json", runs)

        if arguments.vus <= 0:
            raise BenchmarkError("--vus must be positive")
        if arguments.repetitions <= 0:
            raise BenchmarkError("--repetitions must be positive")
        if (
            not math.isfinite(arguments.preflight_timeout_seconds)
            or arguments.preflight_timeout_seconds <= 0
        ):
            raise BenchmarkError("--preflight-timeout-seconds must be positive")

        warmup_duration = validate_duration(
            arguments.warmup_duration, "--warmup-duration"
        )
        measurement_duration = validate_duration(arguments.duration, "--duration")
        accept_encoding = validate_encoding(
            arguments.accept_encoding, "--accept-encoding"
        )
        required_content_encoding = validate_encoding(
            arguments.required_content_encoding, "--required-content-encoding"
        )
        upstream_id = validate_identifier(arguments.upstream_id, "--upstream-id")
        new_java_id = validate_identifier(arguments.new_java_id, "--new-java-id")
        dataset_id = validate_identifier(arguments.dataset_id, "--dataset-id")
        _, setup_manifest_raw = load_setup_manifest(
            arguments.setup_manifest, dataset_id
        )
        frozen_setup_manifest = output / "setup-manifest.json"
        frozen_setup_manifest.write_bytes(setup_manifest_raw)

        targets = [
            Target(
                "upstream",
                validate_url(arguments.upstream_url, "--upstream-url"),
                upstream_id,
            ),
            Target(
                "upstream-php",
                validate_url(arguments.upstream_php_url, "--upstream-php-url"),
                upstream_id,
            ),
            Target(
                "new-java",
                validate_url(arguments.new_java_url, "--new-java-url"),
                new_java_id,
            ),
        ]
        if len({origin_key(target.url) for target in targets}) != len(targets):
            raise BenchmarkError("the three target URLs must be distinct")

        paths = parse_paths(arguments.paths)
        k6_binary = shutil.which(arguments.k6)
        if k6_binary is None:
            raise BenchmarkError(f"k6 executable not found: {arguments.k6}")
        try:
            version_result = subprocess.run(
                [k6_binary, "version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BenchmarkError(f"failed to run {k6_binary} version: {error}") from error
        if version_result.returncode != 0:
            raise BenchmarkError(f"failed to run {k6_binary} version")

        frozen_paths = output / "paths.txt"
        frozen_text = frozen_path_text(paths)
        frozen_paths.write_text(frozen_text, encoding="utf-8")
        script = script_directory / "throughput.js"
        benchmark_files = {
            name: sha256_bytes((script_directory / name).read_bytes())
            for name in ("run.sh", "run_benchmark.py", "throughput.js")
        }

        metadata.update(
            {
                "status": "running",
                "targets": [
                    {
                        "name": target.name,
                        "url": target.url,
                        "artifactId": target.artifact_id,
                    }
                    for target in targets
                ],
                "datasetId": dataset_id,
                "setupManifestSha256": sha256_bytes(setup_manifest_raw),
                "benchmarkFilesSha256": benchmark_files,
                "pathCount": len(paths),
                "pathsSha256": sha256_bytes(frozen_text.encode("utf-8")),
                "vus": arguments.vus,
                "warmupDuration": warmup_duration,
                "measurementDuration": measurement_duration,
                "repetitions": arguments.repetitions,
                "acceptEncoding": accept_encoding,
                "requiredContentEncoding": required_content_encoding,
                "k6Version": (version_result.stdout or version_result.stderr).strip(),
                "pythonVersion": platform.python_version(),
                "platform": platform.platform(),
            }
        )
        write_json(output / "metadata.json", metadata)

        print("Validating status, content encoding, and raw bytes ...", flush=True)
        preflight(
            targets,
            paths,
            accept_encoding,
            required_content_encoding,
            arguments.preflight_timeout_seconds,
            evidence_path=output / "preflight.json",
        )

        def execute_phase(
            *,
            phase: str,
            target: Target,
            repetition: int,
            order_position: int,
            prefix: str,
            duration: str,
            warmup_exit_code: int | None = None,
        ) -> int:
            record: dict[str, Any] = {
                "phase": phase,
                "variant": target.name,
                "repetition": repetition,
                "orderPosition": order_position,
                "status": "running",
                "summaryFile": f"{phase}-{prefix}.json",
                "logFile": f"{phase}-{prefix}.log",
            }
            if warmup_exit_code is not None:
                record["warmupExitCode"] = warmup_exit_code
            runs.append(record)
            write_json(output / "runs.json", runs)
            try:
                exit_code, metrics = run_k6(
                    k6_binary=k6_binary,
                    script=script,
                    target=target,
                    path_file=frozen_paths,
                    vus=arguments.vus,
                    duration=duration,
                    accept_encoding=accept_encoding,
                    required_content_encoding=required_content_encoding,
                    summary_path=output / record["summaryFile"],
                    log_path=output / record["logFile"],
                )
            except BenchmarkError as error:
                record["status"] = "failed"
                record["error"] = str(error)
                write_json(output / "runs.json", runs)
                raise
            record.update(
                {
                    "status": "completed",
                    "k6ExitCode": exit_code,
                    **metrics,
                }
            )
            write_json(output / "runs.json", runs)
            return exit_code

        for repetition in range(1, arguments.repetitions + 1):
            order = rotated_targets(targets, repetition)
            for order_position, target in enumerate(order, start=1):
                prefix = f"r{repetition:02d}-{order_position:02d}-{target.name}"
                print(
                    f"Repetition {repetition}/{arguments.repetitions}, "
                    f"{target.name}: warmup ...",
                    flush=True,
                )
                warmup_exit = execute_phase(
                    phase="warmup",
                    target=target,
                    repetition=repetition,
                    order_position=order_position,
                    prefix=prefix,
                    duration=warmup_duration,
                )

                print(
                    f"Repetition {repetition}/{arguments.repetitions}, "
                    f"{target.name}: measurement ...",
                    flush=True,
                )
                execute_phase(
                    phase="measurement",
                    target=target,
                    repetition=repetition,
                    order_position=order_position,
                    prefix=prefix,
                    duration=measurement_duration,
                    warmup_exit_code=warmup_exit,
                )

        valid = summarize_measurements(output, runs, arguments.repetitions)
        completed_at = dt.datetime.now(dt.UTC).isoformat()
        metadata["completedAt"] = completed_at
        metadata["status"] = "completed"
        metadata["valid"] = valid
        write_json(output / "metadata.json", metadata)
        exit_code = 0 if valid else 1
        write_json(
            output / "terminal.json",
            {
                "formatVersion": FORMAT_VERSION,
                "status": "completed",
                "valid": valid,
                "exitCode": exit_code,
                "completedAt": completed_at,
            },
        )
        print(f"Results: {output}", flush=True)
        return exit_code
    except (BenchmarkError, OSError) as error:
        completed_at = dt.datetime.now(dt.UTC).isoformat()
        metadata["completedAt"] = completed_at
        metadata["status"] = "failed"
        metadata["valid"] = False
        metadata["error"] = str(error)
        terminal: dict[str, Any] = {
            "formatVersion": FORMAT_VERSION,
            "status": "failed",
            "valid": False,
            "exitCode": 2,
            "completedAt": completed_at,
            "error": str(error),
        }
        try:
            write_json(output / "runs.json", runs)
            if expected_repetitions > 0:
                summarize_measurements(output, runs, expected_repetitions)
            write_json(output / "metadata.json", metadata)
        except (OSError, ValueError, KeyError, TypeError) as evidence_error:
            terminal["evidenceWriteError"] = str(evidence_error)
        try:
            write_json(output / "terminal.json", terminal)
        except (OSError, ValueError):
            pass
        print(f"error: {error}", file=sys.stderr)
        print(f"Results: {output}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
