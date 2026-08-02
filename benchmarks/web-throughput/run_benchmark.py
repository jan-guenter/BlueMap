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
import random
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


FORMAT_VERSION = 2
MEBIBYTE = 1024 * 1024
MAX_SETUP_MANIFEST_BYTES = 1024 * 1024
MAX_PATH_LENGTH = 4096
VARIANTS = ("upstream", "upstream-php", "new-java")
APPROVED_VUS = 12
APPROVED_WARMUP_DURATION = "30s"
APPROVED_MEASUREMENT_DURATION = "120s"
APPROVED_REPETITIONS = 5
LINK_CAP_ADMISSION_FRACTION = 0.70
TELEMETRY_MINIMUM_COVERAGE_FRACTION = 0.90
TELEMETRY_MAX_INTERVAL_SECONDS = 2.0
TELEMETRY_MAX_EDGE_LAG_SECONDS = 2.0
DURATION_PATTERN = re.compile(r"^(?:[1-9][0-9]*)(?:ms|s|m|h)$")
ENCODING_PATTERN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
SAFE_PATH_PATTERN = re.compile(r"^/maps/[A-Za-z0-9._~!$&'()*+,;=:@/-]+$")
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
CLOUDFLARE_HEADER_PREFIX = "cf-"
PROXY_RESPONSE_HEADERS = {
    "age",
    "via",
    "x-cache",
    "x-cache-hits",
    "x-proxy-cache",
    "x-served-by",
    "x-varnish",
}


class BenchmarkError(RuntimeError):
    """Raised when benchmark inputs or evidence are invalid."""


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    artifact_id: str
    runtime_identity: str | None = None
    identity_header: str | None = None
    upload_bits_per_second: int | None = None


@dataclass(frozen=True)
class LoadGeneratorAdmission:
    cpu_count: int
    memory_bytes: int
    maximum_cpu_percent: float
    maximum_memory_percent: float
    minimum_samples: int
    minimum_free_disk_bytes: int
    download_bits_per_second: int


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
    parser.add_argument(
        "--profile-id",
        help="Immutable workload profile identity; derived from paths/encodings if omitted",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--vus", type=int, default=APPROVED_VUS)
    parser.add_argument("--warmup-duration", default=APPROVED_WARMUP_DURATION)
    parser.add_argument("--duration", default=APPROVED_MEASUREMENT_DURATION)
    parser.add_argument("--repetitions", type=int, default=APPROVED_REPETITIONS)
    parser.add_argument(
        "--schedule-seed",
        help="Recorded randomization seed; a fresh 128-bit seed is generated if omitted",
    )
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


def _required_manifest_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BenchmarkError(f"setup manifest {name} must be a positive integer")
    return value


def _required_manifest_percent(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkError(f"setup manifest {name} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0 or numeric >= 100:
        raise BenchmarkError(
            f"setup manifest {name} must be finite and greater than 0 and less than 100"
        )
    return numeric


def _required_sha256(value: Any, name: str) -> str:
    normalized = _required_manifest_string(value, name).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise BenchmarkError(f"setup manifest {name} must be a SHA-256 hex digest")
    return normalized


def load_generator_admission(manifest: Mapping[str, Any]) -> LoadGeneratorAdmission:
    load_generator = manifest.get("loadGenerator")
    if not isinstance(load_generator, Mapping):
        raise BenchmarkError("setup manifest loadGenerator must be an object")
    hardware = load_generator.get("hardware")
    if not isinstance(hardware, Mapping):
        raise BenchmarkError("setup manifest loadGenerator.hardware must be an object")
    admission = load_generator.get("admission")
    if not isinstance(admission, Mapping):
        raise BenchmarkError("setup manifest loadGenerator.admission must be an object")
    return LoadGeneratorAdmission(
        cpu_count=_required_manifest_integer(
            hardware.get("logicalCpuCount"), "loadGenerator.hardware.logicalCpuCount"
        ),
        memory_bytes=_required_manifest_integer(
            hardware.get("memoryBytes"), "loadGenerator.hardware.memoryBytes"
        ),
        maximum_cpu_percent=_required_manifest_percent(
            admission.get("maximumCpuUtilizationPercent"),
            "loadGenerator.admission.maximumCpuUtilizationPercent",
        ),
        maximum_memory_percent=_required_manifest_percent(
            admission.get("maximumMemoryUtilizationPercent"),
            "loadGenerator.admission.maximumMemoryUtilizationPercent",
        ),
        minimum_samples=_required_manifest_integer(
            admission.get("minimumSamples"),
            "loadGenerator.admission.minimumSamples",
        ),
        minimum_free_disk_bytes=_required_manifest_integer(
            admission.get("minimumFreeDiskBytes"),
            "loadGenerator.admission.minimumFreeDiskBytes",
        ),
        download_bits_per_second=_required_manifest_integer(
            hardware.get("downloadBitsPerSecond"),
            "loadGenerator.hardware.downloadBitsPerSecond",
        ),
    )


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
    if manifest.get("formatVersion") != FORMAT_VERSION:
        raise BenchmarkError(
            f"setup manifest formatVersion must be {FORMAT_VERSION}"
        )

    _required_manifest_string(manifest.get("environment"), "environment")
    _required_manifest_string(manifest.get("protocol"), "protocol")
    if manifest.get("directOrigin") is not True:
        raise BenchmarkError("setup manifest directOrigin must be true")

    runpod = manifest.get("runpod")
    if not isinstance(runpod, dict):
        raise BenchmarkError("setup manifest runpod must be an object")
    _required_manifest_string(runpod.get("region"), "runpod.region")
    _required_manifest_string(runpod.get("topology"), "runpod.topology")

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
    connection_ceiling = database.get("perCandidateConnectionCeiling")
    if (
        isinstance(connection_ceiling, bool)
        or not isinstance(connection_ceiling, int)
        or connection_ceiling != APPROVED_VUS
    ):
        raise BenchmarkError(
            "setup manifest database.perCandidateConnectionCeiling must be exactly "
            f"{APPROVED_VUS}"
        )

    _required_manifest_string(database.get("engine"), "database.engine")
    _required_manifest_string(database.get("version"), "database.version")
    database_tls = database.get("tls")
    if not isinstance(database_tls, dict):
        raise BenchmarkError("setup manifest database.tls must be an object")
    if database_tls.get("required") is not True or database_tls.get("verified") is not True:
        raise BenchmarkError(
            "setup manifest database.tls.required and verified must both be true"
        )
    _required_manifest_string(database_tls.get("serverName"), "database.tls.serverName")
    _required_sha256(database_tls.get("caSha256"), "database.tls.caSha256")

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
    identity_headers: list[str | None] = []
    for variant in VARIANTS:
        target = targets.get(variant)
        if not isinstance(target, dict):
            raise BenchmarkError(f"setup manifest targets.{variant} must be an object")
        _required_manifest_string(target.get("runtime"), f"targets.{variant}.runtime")
        _required_manifest_string(
            target.get("configuration"), f"targets.{variant}.configuration"
        )

        _required_manifest_string(
            target.get("runtimeIdentity"), f"targets.{variant}.runtimeIdentity"
        )
        _required_manifest_string(target.get("runpodPodId"), f"targets.{variant}.runpodPodId")
        _required_manifest_integer(
            target.get("uploadBitsPerSecond"),
            f"targets.{variant}.uploadBitsPerSecond",
        )
        image_digest = _required_manifest_string(
            target.get("imageDigest"), f"targets.{variant}.imageDigest"
        ).lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
            raise BenchmarkError(
                f"setup manifest targets.{variant}.imageDigest must be an immutable sha256 digest"
            )
        for hash_field in (
            "processIdentitySha256",
            "runtimeProbeSha256",
            "configurationSha256",
        ):
            _required_sha256(
                target.get(hash_field), f"targets.{variant}.{hash_field}"
            )
        identity_header_value = target.get("identityHeader")
        if identity_header_value is None:
            identity_headers.append(None)
        else:
            identity_header = _required_manifest_string(
                identity_header_value, f"targets.{variant}.identityHeader"
            )
            if not HEADER_NAME_PATTERN.fullmatch(identity_header):
                raise BenchmarkError(
                    f"setup manifest targets.{variant}.identityHeader is not a valid HTTP header"
                )
            identity_headers.append(identity_header)

    if any(value is None for value in identity_headers) and any(
        value is not None for value in identity_headers
    ):
        raise BenchmarkError(
            "setup manifest identityHeader must be disabled for all targets or enabled for all"
        )

    load_generator = manifest.get("loadGenerator")
    if not isinstance(load_generator, dict):
        raise BenchmarkError("setup manifest loadGenerator must be an object")
    _required_manifest_string(load_generator.get("runtime"), "loadGenerator.runtime")
    _required_manifest_string(
        load_generator.get("configuration"), "loadGenerator.configuration"
    )
    hardware = load_generator.get("hardware")
    if not isinstance(hardware, dict):
        raise BenchmarkError("setup manifest loadGenerator.hardware must be an object")
    _required_manifest_string(hardware.get("podId"), "loadGenerator.hardware.podId")
    _required_manifest_string(hardware.get("cpuModel"), "loadGenerator.hardware.cpuModel")
    _required_manifest_string(hardware.get("podType"), "loadGenerator.hardware.podType")
    load_generator_admission(manifest)

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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_content_encoding(value: str | None) -> str:
    if value is None or not value.strip():
        return "identity"
    return value.strip().lower()


def normalize_content_type(value: str | None) -> str:
    if value is None or not value.strip():
        raise BenchmarkError("response has no Content-Type")
    return ";".join(part.strip().lower() for part in value.split(";"))


def decode_stored_representation(
    body: bytes,
    content_encoding: str,
    timeout_seconds: float,
    *,
    zstd_executable: str | None = None,
) -> bytes:
    """Decode the stored HTTP representation with an independently pinned tool."""
    if content_encoding == "identity":
        return body
    if content_encoding != "zstd":
        raise BenchmarkError(
            f"preflight cannot independently decode Content-Encoding {content_encoding!r}"
        )
    executable = zstd_executable or shutil.which("zstd")
    if executable is None:
        raise BenchmarkError("preflight requires the zstd executable")
    try:
        result = subprocess.run(
            [executable, "--decompress", "--stdout", "--quiet"],
            input=body,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BenchmarkError(f"preflight zstd decoding failed: {error}") from error
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise BenchmarkError(
            "preflight zstd representation is invalid"
            + (f": {detail}" if detail else "")
        )
    return result.stdout


def zstd_tool_identity() -> dict[str, str]:
    executable = shutil.which("zstd")
    if executable is None:
        raise BenchmarkError("preflight requires the zstd executable")
    resolved = Path(executable).resolve()
    if not resolved.is_file():
        raise BenchmarkError("preflight zstd executable is not a regular file")
    try:
        version = subprocess.run(
            [str(resolved), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise BenchmarkError(f"failed to identify preflight zstd tool: {error}") from error
    if not version or any(character in version for character in "\r\n"):
        raise BenchmarkError("preflight zstd version evidence is malformed")
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "version": version,
    }


def response_header_values(headers: Any) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for name, value in headers.items():
        values.setdefault(name.lower(), []).append(value.strip())
    return values


def rejected_proxy_headers(headers: Any) -> list[str]:
    normalized = response_header_values(headers)
    rejected = sorted(
        name
        for name in normalized
        if name.startswith(CLOUDFLARE_HEADER_PREFIX) or name in PROXY_RESPONSE_HEADERS
    )
    if any("cloudflare" in value.lower() for value in normalized.get("server", [])):
        rejected.append("server: cloudflare")
    return rejected


def _request_without_proxy(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    timeout_seconds: float,
) -> tuple[int, bytes, Any]:
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        if error.code == 304:
            try:
                return error.code, error.read(), error.headers
            finally:
                error.close()
        raise


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
        "proxyDisabled": True,
        "directOriginValidated": False,
        "paths": {},
    }
    decoder = zstd_tool_identity() if required_content_encoding == "zstd" else None
    evidence["storedRepresentationDecoder"] = decoder

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
        reference_decoded_digest: str | None = None
        reference_decoded_size: int | None = None
        reference_content_type: str | None = None
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
                status, body, headers = _request_without_proxy(
                    opener, request, timeout_seconds
                )
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

            rejected_headers = rejected_proxy_headers(headers)
            if rejected_headers:
                fail(
                    f"preflight {target.name} {path}: proxy/CDN response headers "
                    f"are forbidden: {', '.join(rejected_headers)}",
                    entry,
                )

            encoding = normalize_content_encoding(headers.get("Content-Encoding"))
            try:
                content_type = normalize_content_type(headers.get("Content-Type"))
            except BenchmarkError as error:
                fail(f"preflight {target.name} {path}: {error}", entry)
                raise AssertionError("unreachable") from error
            digest = sha256_bytes(body)
            declared_content_length = headers.get("Content-Length")
            parsed_content_length: int | None = None
            if declared_content_length is not None:
                if (
                    re.fullmatch(r"[0-9]+", declared_content_length) is None
                    or int(declared_content_length) != len(body)
                ):
                    fail(
                        f"preflight {target.name} {path}: Content-Length does not "
                        "match the stored representation",
                        entry,
                    )
                parsed_content_length = int(declared_content_length)
            try:
                decoded_body = decode_stored_representation(
                    body,
                    encoding,
                    timeout_seconds,
                    zstd_executable=(decoder["path"] if decoder is not None else None),
                )
            except BenchmarkError as error:
                fail(f"preflight {target.name} {path}: {error}", entry)
                raise AssertionError("unreachable") from error
            decoded_digest = sha256_bytes(decoded_body)
            etag = headers.get("ETag")
            last_modified = headers.get("Last-Modified")
            entry.update(
                {
                    "status": status,
                    "contentEncoding": encoding,
                    "contentType": content_type,
                    "storedRepresentationLength": len(body),
                    "storedRepresentationSha256": digest,
                    "decodedContentLength": len(decoded_body),
                    "decodedSha256": decoded_digest,
                    "declaredContentLength": parsed_content_length,
                    "etag": etag,
                    "lastModified": last_modified,
                    "targetIdentity": None,
                    "runtimeIdentity": target.runtime_identity,
                    "conditional": {},
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
            if target.identity_header is not None:
                observed_identity = headers.get(target.identity_header)
                entry["targetIdentity"] = observed_identity
                if observed_identity != target.runtime_identity:
                    fail(
                        f"preflight {target.name} {path}: expected identity header "
                        f"{target.identity_header}={target.runtime_identity!r}, got "
                        f"{observed_identity!r}",
                        entry,
                    )

            if reference_digest is None:
                reference_digest = digest
                reference_size = len(body)
                reference_decoded_digest = decoded_digest
                reference_decoded_size = len(decoded_body)
                reference_content_type = content_type
            elif (
                digest != reference_digest
                or len(body) != reference_size
                or decoded_digest != reference_decoded_digest
                or len(decoded_body) != reference_decoded_size
                or content_type != reference_content_type
            ):
                fail(
                    f"preflight {target.name} {path}: stored/decoded body size or "
                    f"digest, or Content-Type, differs from {targets[0].name}",
                    entry,
                )

            validators = (
                ("etag", "If-None-Match", etag),
                ("lastModified", "If-Modified-Since", last_modified),
            )
            for validator_name, request_header, validator_value in validators:
                if validator_value is None:
                    entry["conditional"][validator_name] = {
                        "supported": False,
                        "valid": True,
                    }
                    continue
                conditional_request = urllib.request.Request(
                    f"{target.url}{path}",
                    headers={
                        "Accept-Encoding": accept_encoding,
                        "User-Agent": "BlueMap-Throughput-Preflight/1",
                        request_header: validator_value,
                    },
                    method="GET",
                )
                try:
                    conditional_status, conditional_body, conditional_headers = (
                        _request_without_proxy(
                            opener, conditional_request, timeout_seconds
                        )
                    )
                except (
                    urllib.error.HTTPError,
                    urllib.error.URLError,
                    http.client.HTTPException,
                    TimeoutError,
                    OSError,
                ) as error:
                    fail(
                        f"preflight {target.name} {path}: conditional {validator_name} "
                        f"request failed: {error}",
                        entry,
                    )
                    raise AssertionError("unreachable") from error
                conditional_rejected = rejected_proxy_headers(conditional_headers)
                conditional_evidence = {
                    "supported": True,
                    "status": conditional_status,
                    "contentLength": len(conditional_body),
                    "etag": conditional_headers.get("ETag"),
                    "lastModified": conditional_headers.get("Last-Modified"),
                    "valid": False,
                }
                entry["conditional"][validator_name] = conditional_evidence
                if conditional_rejected:
                    fail(
                        f"preflight {target.name} {path}: conditional response has "
                        f"forbidden proxy/CDN headers: {', '.join(conditional_rejected)}",
                        entry,
                    )
                if conditional_status != 304 or conditional_body:
                    fail(
                        f"preflight {target.name} {path}: conditional {validator_name} "
                        f"expected empty HTTP 304, got HTTP {conditional_status} with "
                        f"{len(conditional_body)} bytes",
                        entry,
                    )
                conditional_evidence["valid"] = True

            entry["valid"] = True
            persist()

    evidence["status"] = "completed"
    evidence["valid"] = True
    evidence["directOriginValidated"] = True
    persist()
    return evidence


def rotated_targets(targets: Sequence[Target], repetition: int) -> list[Target]:
    if not targets:
        return []
    offset = (repetition - 1) % len(targets)
    return list(targets[offset:]) + list(targets[:offset])


def create_schedule(
    targets: Sequence[Target], repetitions: int, seed: str
) -> list[dict[str, Any]]:
    if repetitions <= 0:
        raise BenchmarkError("schedule repetitions must be positive")
    if len(targets) != len(VARIANTS) or {target.name for target in targets} != set(
        VARIANTS
    ):
        raise BenchmarkError("schedule requires exactly the three benchmark variants")
    seed_digest = hashlib.sha256(seed.encode("utf-8")).digest()
    initial_order = list(targets)
    random.Random(seed_digest).shuffle(initial_order)
    return [
        {
            "block": repetition,
            "order": [
                target.name
                for target in rotated_targets(initial_order, repetition)
            ],
        }
        for repetition in range(1, repetitions + 1)
    ]


def schedule_order(
    schedule: Sequence[Mapping[str, Any]],
    targets: Sequence[Target],
    repetition: int,
) -> list[Target]:
    if repetition <= 0 or repetition > len(schedule):
        raise BenchmarkError(f"schedule has no block {repetition}")
    entry = schedule[repetition - 1]
    if entry.get("block") != repetition or not isinstance(entry.get("order"), list):
        raise BenchmarkError(f"schedule block {repetition} is malformed")
    by_name = {target.name: target for target in targets}
    order = entry["order"]
    if len(order) != len(VARIANTS) or set(order) != set(VARIANTS):
        raise BenchmarkError(f"schedule block {repetition} is incomplete")
    return [by_name[name] for name in order]


def build_expectations(preflight_evidence: Mapping[str, Any]) -> dict[str, Any]:
    paths = preflight_evidence.get("paths")
    if not isinstance(paths, Mapping) or not preflight_evidence.get("valid"):
        raise BenchmarkError("cannot build expectations from invalid preflight evidence")
    expectations: dict[str, Any] = {"formatVersion": FORMAT_VERSION, "paths": {}}
    for path, target_entries in paths.items():
        if not isinstance(path, str) or not isinstance(target_entries, Mapping):
            raise BenchmarkError("preflight path evidence is malformed")
        reference = target_entries.get(VARIANTS[0])
        if not isinstance(reference, Mapping) or reference.get("valid") is not True:
            raise BenchmarkError(f"preflight path {path!r} has no valid reference")
        stored_length = reference.get("storedRepresentationLength")
        decoded_length = reference.get("decodedContentLength")
        content_type = reference.get("contentType")
        if (
            isinstance(stored_length, bool)
            or not isinstance(stored_length, int)
            or stored_length <= 0
            or isinstance(decoded_length, bool)
            or not isinstance(decoded_length, int)
            or decoded_length <= 0
            or not isinstance(content_type, str)
            or not content_type
        ):
            raise BenchmarkError(f"preflight path {path!r} expectation is malformed")
        expectations["paths"][path] = {
            "storedRepresentationLength": stored_length,
            "decodedContentLength": decoded_length,
            "contentType": content_type,
            "storedRepresentationSha256": reference.get(
                "storedRepresentationSha256"
            ),
            "decodedSha256": reference.get("decodedSha256"),
            "targets": {
                variant: {
                    "etag": target_entries[variant].get("etag"),
                    "lastModified": target_entries[variant].get("lastModified"),
                    "declaredContentLength": target_entries[variant].get(
                        "declaredContentLength"
                    ),
                }
                for variant in VARIANTS
                if isinstance(target_entries.get(variant), Mapping)
                and target_entries[variant].get("valid") is True
            },
        }
        if set(expectations["paths"][path]["targets"]) != set(VARIANTS):
            raise BenchmarkError(
                f"preflight path {path!r} is missing a valid target expectation"
            )
    return expectations


def metric_values(summary: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    metrics = summary.get("metrics")
    if not isinstance(metrics, Mapping):
        raise BenchmarkError("k6 summary has no metrics object")
    metric = metrics.get(name)
    if not isinstance(metric, Mapping):
        raise BenchmarkError(f"k6 summary has no {name!r} metric")
    values = metric.get("values")
    if isinstance(values, Mapping):
        return values
    # k6 v1.3.0's --summary-export format is flat, while handleSummary and
    # older fixtures wrap the values. Accept only these two explicit shapes.
    if any(key in metric for key in ("count", "rate", "med", "p(95)")):
        return metric
    raise BenchmarkError(f"k6 metric {name!r} has no recognized values object")


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


def finite_rate_metric(summary: Mapping[str, Any], name: str) -> float:
    metrics = summary.get("metrics")
    if not isinstance(metrics, Mapping) or not isinstance(metrics.get(name), Mapping):
        raise BenchmarkError(f"k6 summary has no {name!r} rate metric")
    metric = metrics[name]
    assert isinstance(metric, Mapping)
    wrapped = metric.get("values")
    if isinstance(wrapped, Mapping):
        return float(
            finite_metric(wrapped, "rate", name, minimum=0.0, maximum=1.0)
        )
    # Exact k6 v1.3.0 --summary-export renames Rate.rate to Rate.value.
    return float(
        finite_metric(metric, "value", name, minimum=0.0, maximum=1.0)
    )


def extract_metrics(
    summary: Mapping[str, Any],
    *,
    expected_path_count: int | None = None,
    expected_stored_bytes_per_iteration: int | None = None,
) -> dict[str, float | int]:
    requests = metric_values(summary, "http_reqs")
    received = metric_values(summary, "data_received")
    stored_representation = metric_values(
        summary, "benchmark_stored_representation_bytes"
    )
    duration = metric_values(summary, "http_req_duration")
    iterations = metric_values(summary, "iterations")
    error_metrics = {
        "errors": "benchmark_errors",
        "httpErrors": "benchmark_http_errors",
        "transportErrors": "benchmark_transport_errors",
        "proxyHeaderErrors": "benchmark_proxy_header_errors",
        "encodingErrors": "benchmark_encoding_errors",
        "contentTypeErrors": "benchmark_content_type_errors",
        "contentLengthErrors": "benchmark_content_length_errors",
        "bodyLengthErrors": "benchmark_body_length_errors",
        "identityErrors": "benchmark_identity_errors",
        "cacheValidatorErrors": "benchmark_cache_validator_errors",
        "droppedIterations": "benchmark_dropped_iterations",
        "observedResponses": "benchmark_observed_responses",
    }

    request_count = finite_metric(
        requests, "count", "http_reqs", minimum=1.0, integer=True
    )
    request_rate = finite_metric(requests, "rate", "http_reqs", minimum=0.0)
    if request_rate <= 0:
        raise BenchmarkError("k6 metric 'http_reqs' rate must be positive")
    result: dict[str, float | int] = {
        "requests": request_count,
        "requestsPerSecond": request_rate,
        "networkReceivedBytes": finite_metric(
            received, "count", "data_received", minimum=0.0, integer=True
        ),
        "networkMibPerSecond": finite_metric(
            received, "rate", "data_received", minimum=0.0
        )
        / MEBIBYTE,
        "storedRepresentationBytes": finite_metric(
            stored_representation,
            "count",
            "benchmark_stored_representation_bytes",
            minimum=1.0,
            integer=True,
        ),
        "storedMibPerSecond": finite_metric(
            stored_representation,
            "rate",
            "benchmark_stored_representation_bytes",
            minimum=0.0,
        )
        / MEBIBYTE,
        "p50Milliseconds": finite_metric(
            duration, "med", "http_req_duration", minimum=0.0
        ),
        "p95Milliseconds": finite_metric(
            duration, "p(95)", "http_req_duration", minimum=0.0
        ),
        "p99Milliseconds": finite_metric(
            duration, "p(99)", "http_req_duration", minimum=0.0
        ),
        "iterations": finite_metric(
            iterations, "count", "iterations", minimum=1.0, integer=True
        ),
        "httpFailureRate": finite_rate_metric(summary, "http_req_failed"),
        "checkFailureRate": 1.0 - finite_rate_metric(summary, "checks"),
    }
    for result_name, metric_name in error_metrics.items():
        result[result_name] = finite_metric(
            metric_values(summary, metric_name),
            "count",
            metric_name,
            minimum=0.0,
            integer=True,
        )
    if result["observedResponses"] != request_count:
        raise BenchmarkError(
            "k6 observed-response counter does not match http_reqs count"
        )
    if expected_path_count is not None:
        if expected_path_count <= 0:
            raise BenchmarkError("expected path count must be positive")
        expected_requests = int(result["iterations"]) * expected_path_count
        if request_count != expected_requests:
            raise BenchmarkError(
                "k6 http_reqs does not equal completed iterations times path count"
            )
    if expected_stored_bytes_per_iteration is not None:
        if expected_stored_bytes_per_iteration <= 0:
            raise BenchmarkError(
                "expected stored bytes per iteration must be positive"
            )
        expected_stored_bytes = (
            int(result["iterations"]) * expected_stored_bytes_per_iteration
        )
        if result["storedRepresentationBytes"] != expected_stored_bytes:
            raise BenchmarkError(
                "stored-representation counter does not match complete profile iterations"
            )
    result["httpFailures"] = result["httpErrors"]

    metrics = summary.get("metrics")
    assert isinstance(metrics, Mapping)
    built_in_dropped = metrics.get("dropped_iterations")
    if built_in_dropped is None:
        result["k6DroppedIterations"] = 0
    else:
        if not isinstance(built_in_dropped, Mapping):
            raise BenchmarkError("k6 dropped_iterations metric is malformed")
        result["k6DroppedIterations"] = finite_metric(
            metric_values(summary, "dropped_iterations"),
            "count",
            "dropped_iterations",
            minimum=0.0,
            integer=True,
        )
    return result


class ProcessResourceSampler:
    def __init__(
        self,
        pid: int,
        admission: LoadGeneratorAdmission,
        expected_phase_duration_seconds: float,
        target_upload_bits_per_second: int,
        interval_seconds: float = 1.0,
        proc_root: Path = Path("/proc"),
    ) -> None:
        if (
            not math.isfinite(expected_phase_duration_seconds)
            or expected_phase_duration_seconds <= 0
        ):
            raise BenchmarkError("expected telemetry phase duration must be positive")
        if (
            isinstance(target_upload_bits_per_second, bool)
            or not isinstance(target_upload_bits_per_second, int)
            or target_upload_bits_per_second <= 0
        ):
            raise BenchmarkError("target upload link cap must be a positive integer")
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise BenchmarkError("telemetry interval must be positive")
        self.pid = pid
        self.admission = admission
        self.expected_phase_duration_seconds = expected_phase_duration_seconds
        self.target_upload_bits_per_second = target_upload_bits_per_second
        self.interval_seconds = interval_seconds
        self._proc_root = proc_root
        self._stop = threading.Event()
        self._samples: list[dict[str, Any]] = []
        self._capture_errors: list[str] = []
        self._source_discovery_valid = False
        self._cgroup_version: int | None = None
        self._cgroup_cpu_path: Path | None = None
        self._cgroup_memory_path: Path | None = None
        self._network_interface: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._capture_start_monotonic_seconds: float | None = None
        self._capture_stop_monotonic_seconds: float | None = None
        self._phase_start_monotonic_seconds: float | None = None
        self._phase_end_monotonic_seconds: float | None = None
        self._sampler_thread_stopped = False

    def start(self, phase_start_monotonic_seconds: float) -> None:
        self._phase_start_monotonic_seconds = phase_start_monotonic_seconds
        self._capture_start_monotonic_seconds = time.monotonic()
        self._initialize_sources()
        self._capture()
        self._thread.start()

    def stop(self, phase_end_monotonic_seconds: float) -> dict[str, Any]:
        self._phase_end_monotonic_seconds = phase_end_monotonic_seconds
        self._stop.set()
        self._thread.join(timeout=max(2.0, self.interval_seconds * 3))
        self._capture_stop_monotonic_seconds = time.monotonic()
        self._sampler_thread_stopped = not self._thread.is_alive()
        return self._summarize()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._capture()

    @staticmethod
    def _decode_mountinfo_field(value: str) -> str:
        return re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )

    @staticmethod
    def _resolve_cgroup_mount_path(
        mount_root: str, mount_point: str, cgroup_path: str
    ) -> Path | None:
        root = PurePosixPath(mount_root)
        member = PurePosixPath(cgroup_path)
        if not root.is_absolute() or not member.is_absolute():
            return None
        try:
            relative = member.relative_to(root)
        except ValueError:
            return None
        return Path(mount_point).joinpath(*relative.parts)

    def _mountinfo_entries(self) -> list[dict[str, Any]]:
        lines = (self._proc_root / str(self.pid) / "mountinfo").read_text(
            encoding="utf-8"
        ).splitlines()
        entries: list[dict[str, Any]] = []
        for line_number, line in enumerate(lines, start=1):
            fields = line.split()
            try:
                separator = fields.index("-")
            except ValueError as error:
                raise ValueError(
                    f"mountinfo line {line_number} has no separator"
                ) from error
            if separator < 6 or len(fields) <= separator + 3:
                raise ValueError(f"mountinfo line {line_number} is malformed")
            entries.append(
                {
                    "root": self._decode_mountinfo_field(fields[3]),
                    "mountPoint": self._decode_mountinfo_field(fields[4]),
                    "filesystemType": fields[separator + 1],
                    "superOptions": set(fields[separator + 3].split(",")),
                }
            )
        return entries

    def _discover_cgroup_sources(self) -> tuple[int, Path, Path]:
        cgroup_lines = (self._proc_root / str(self.pid) / "cgroup").read_text(
            encoding="utf-8"
        ).splitlines()
        parsed: list[tuple[str, set[str], str]] = []
        for line_number, line in enumerate(cgroup_lines, start=1):
            parts = line.split(":", 2)
            if len(parts) != 3 or not parts[2].startswith("/"):
                raise ValueError(f"cgroup line {line_number} is malformed")
            parsed.append(
                (parts[0], set(filter(None, parts[1].split(","))), parts[2])
            )

        mountinfo = self._mountinfo_entries()
        unified = [
            path
            for hierarchy, controllers, path in parsed
            if hierarchy == "0" and not controllers
        ]
        if unified:
            if len(unified) != 1:
                raise ValueError("cgroup v2 membership is ambiguous")
            candidates: set[tuple[Path, Path]] = set()
            for entry in mountinfo:
                if entry["filesystemType"] != "cgroup2":
                    continue
                base = self._resolve_cgroup_mount_path(
                    entry["root"], entry["mountPoint"], unified[0]
                )
                if base is None:
                    continue
                cpu_path = base / "cpu.stat"
                memory_path = base / "memory.current"
                if cpu_path.is_file() and memory_path.is_file():
                    candidates.add((cpu_path, memory_path))
            if len(candidates) != 1:
                raise ValueError(
                    "expected exactly one readable cgroup v2 CPU/memory source"
                )
            cpu_path, memory_path = next(iter(candidates))
            return 2, cpu_path, memory_path

        cpu_memberships = [
            path for _, controllers, path in parsed if "cpuacct" in controllers
        ]
        memory_memberships = [
            path for _, controllers, path in parsed if "memory" in controllers
        ]
        if len(cpu_memberships) != 1 or len(memory_memberships) != 1:
            raise ValueError(
                "cgroup v1 CPU or memory membership is missing or ambiguous"
            )

        def v1_candidates(
            controller: str, membership: str, filename: str
        ) -> set[Path]:
            candidates: set[Path] = set()
            for entry in mountinfo:
                if entry["filesystemType"] != "cgroup":
                    continue
                mount_controllers = set(entry["superOptions"])
                mount_controllers.update(Path(entry["mountPoint"]).name.split(","))
                if controller not in mount_controllers:
                    continue
                base = self._resolve_cgroup_mount_path(
                    entry["root"], entry["mountPoint"], membership
                )
                if base is not None and (base / filename).is_file():
                    candidates.add(base / filename)
            return candidates

        cpu_candidates = v1_candidates("cpuacct", cpu_memberships[0], "cpuacct.usage")
        memory_candidates = v1_candidates(
            "memory", memory_memberships[0], "memory.usage_in_bytes"
        )
        if len(cpu_candidates) != 1 or len(memory_candidates) != 1:
            raise ValueError(
                "expected exactly one readable cgroup v1 CPU and memory source"
            )
        return 1, next(iter(cpu_candidates)), next(iter(memory_candidates))

    def _network_counters(self) -> dict[str, tuple[int, int]]:
        lines = (self._proc_root / "net" / "dev").read_text(
            encoding="utf-8"
        ).splitlines()
        counters: dict[str, tuple[int, int]] = {}
        for line_number, line in enumerate(lines[2:], start=3):
            if not line.strip():
                continue
            if ":" not in line:
                raise ValueError(f"network counter line {line_number} is malformed")
            interface, data = line.split(":", 1)
            interface = interface.strip()
            if not interface or not re.fullmatch(r"[A-Za-z0-9_.:-]+", interface):
                raise ValueError(f"network interface on line {line_number} is invalid")
            if interface in counters:
                raise ValueError(f"network interface {interface!r} is duplicated")
            fields = data.split()
            if len(fields) != 16:
                raise ValueError(f"network counters for {interface!r} are malformed")
            values = [int(field) for field in fields]
            if any(value < 0 for value in values):
                raise ValueError(f"network counters for {interface!r} are negative")
            counters[interface] = (values[0], values[8])
        return counters

    def _discover_external_network_interface(self) -> str:
        external = sorted(
            interface
            for interface in self._network_counters()
            if interface != "lo"
        )
        if len(external) != 1:
            raise ValueError(
                "expected exactly one non-loopback external network interface"
            )
        return external[0]

    def _initialize_sources(self) -> None:
        try:
            version, cpu_path, memory_path = self._discover_cgroup_sources()
            network_interface = self._discover_external_network_interface()
        except (OSError, ValueError) as error:
            self._capture_errors.append(f"telemetry source discovery failed: {error}")
            return
        self._cgroup_version = version
        self._cgroup_cpu_path = cpu_path
        self._cgroup_memory_path = memory_path
        self._network_interface = network_interface
        self._source_discovery_valid = True

    def _read_cpu_usage_nanoseconds(self) -> int:
        assert self._cgroup_cpu_path is not None
        value = self._cgroup_cpu_path.read_text(encoding="utf-8").strip()
        if self._cgroup_version == 2:
            usage_values = []
            for line in value.splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[0] == "usage_usec":
                    usage_values.append(int(fields[1]))
            if len(usage_values) != 1 or usage_values[0] < 0:
                raise ValueError("cgroup v2 cpu.stat has invalid usage_usec")
            return usage_values[0] * 1000
        if self._cgroup_version != 1 or not re.fullmatch(r"[0-9]+", value):
            raise ValueError("cgroup v1 cpuacct.usage is invalid")
        return int(value)

    def _read_memory_current_bytes(self) -> int:
        assert self._cgroup_memory_path is not None
        value = self._cgroup_memory_path.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError("cgroup memory usage is invalid")
        return int(value)

    def _capture(self) -> None:
        if not self._source_discovery_valid:
            return
        try:
            captured_at = time.monotonic()
            cpu_usage_nanoseconds = self._read_cpu_usage_nanoseconds()
            memory_current_bytes = self._read_memory_current_bytes()
            network_counters = self._network_counters()
            external_interfaces = sorted(
                interface for interface in network_counters if interface != "lo"
            )
            if external_interfaces != [self._network_interface]:
                raise ValueError(
                    "non-loopback network interface set changed after discovery"
                )
            receive_bytes, transmit_bytes = network_counters[self._network_interface]
            self._samples.append(
                {
                    "monotonicSeconds": captured_at,
                    "cpuUsageNanoseconds": cpu_usage_nanoseconds,
                    "memoryCurrentBytes": memory_current_bytes,
                    "networkReceiveBytes": receive_bytes,
                    "networkTransmitBytes": transmit_bytes,
                }
            )
        except (OSError, ValueError, KeyError) as error:
            self._capture_errors.append(f"telemetry capture failed: {error}")

    def _summarize(self) -> dict[str, Any]:
        evidence_errors: list[str] = []
        evidence_errors.extend(self._capture_errors)
        intervals: list[dict[str, float]] = []
        raw_samples = [dict(sample) for sample in self._samples]

        source_evidence_valid = (
            self._source_discovery_valid
            and self._cgroup_version in {1, 2}
            and isinstance(self._cgroup_cpu_path, Path)
            and isinstance(self._cgroup_memory_path, Path)
            and isinstance(self._network_interface, str)
            and bool(self._network_interface)
            and self._network_interface != "lo"
            and not self._capture_errors
        )
        if not source_evidence_valid:
            evidence_errors.append(
                "telemetry source identity or capture evidence is invalid"
            )

        capture_start = self._capture_start_monotonic_seconds
        capture_stop = self._capture_stop_monotonic_seconds
        phase_start = self._phase_start_monotonic_seconds
        phase_end = self._phase_end_monotonic_seconds
        capture_bounds_valid = (
            isinstance(capture_start, (int, float))
            and not isinstance(capture_start, bool)
            and math.isfinite(float(capture_start))
            and isinstance(capture_stop, (int, float))
            and not isinstance(capture_stop, bool)
            and math.isfinite(float(capture_stop))
            and float(capture_start) >= 0
            and float(capture_stop) >= float(capture_start)
        )
        if not capture_bounds_valid:
            evidence_errors.append("capture start/stop monotonic evidence is invalid")
        phase_bounds_valid = (
            isinstance(phase_start, (int, float))
            and not isinstance(phase_start, bool)
            and math.isfinite(float(phase_start))
            and isinstance(phase_end, (int, float))
            and not isinstance(phase_end, bool)
            and math.isfinite(float(phase_end))
            and float(phase_start) >= 0
            and float(phase_end) >= float(phase_start)
        )
        if not phase_bounds_valid:
            evidence_errors.append("phase start/end monotonic evidence is invalid")
        phase_capture_order_valid = (
            capture_bounds_valid
            and phase_bounds_valid
            and float(phase_start) <= float(capture_start)
            and float(phase_end) <= float(capture_stop)
        )
        if not phase_capture_order_valid:
            evidence_errors.append("phase/capture monotonic ordering is invalid")
        if not self._sampler_thread_stopped:
            evidence_errors.append("telemetry sampler thread did not stop")

        required_raw_fields = (
            "monotonicSeconds",
            "cpuUsageNanoseconds",
            "memoryCurrentBytes",
            "networkReceiveBytes",
            "networkTransmitBytes",
        )
        raw_samples_valid = True
        for index, sample in enumerate(raw_samples):
            monotonic_value = sample.get("monotonicSeconds")
            if (
                not isinstance(monotonic_value, (int, float))
                or isinstance(monotonic_value, bool)
                or not math.isfinite(float(monotonic_value))
                or float(monotonic_value) < 0
            ):
                evidence_errors.append(
                    f"raw sample {index} has invalid monotonicSeconds"
                )
                raw_samples_valid = False
            for field in required_raw_fields[1:]:
                value = sample.get(field)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                ):
                    evidence_errors.append(f"raw sample {index} has invalid {field}")
                    raw_samples_valid = False

        if raw_samples_valid and capture_bounds_valid and raw_samples:
            first_monotonic = float(raw_samples[0]["monotonicSeconds"])
            last_monotonic = float(raw_samples[-1]["monotonicSeconds"])
            if first_monotonic < float(capture_start):
                evidence_errors.append("first raw sample predates capture start")
                raw_samples_valid = False
            if last_monotonic > float(capture_stop):
                evidence_errors.append("last raw sample follows capture stop")
                raw_samples_valid = False

        if raw_samples_valid:
            for index, (previous, current) in enumerate(
                zip(raw_samples, raw_samples[1:])
            ):
                start_monotonic = float(previous["monotonicSeconds"])
                end_monotonic = float(current["monotonicSeconds"])
                elapsed = end_monotonic - start_monotonic
                if elapsed <= 0:
                    evidence_errors.append(
                        f"telemetry interval {index} is not strictly chronological"
                    )
                    continue
                if elapsed > TELEMETRY_MAX_INTERVAL_SECONDS:
                    evidence_errors.append(
                        f"telemetry interval {index} exceeds the maximum interval"
                    )
                cpu_usage_delta = (
                    current["cpuUsageNanoseconds"]
                    - previous["cpuUsageNanoseconds"]
                )
                receive_delta = (
                    current["networkReceiveBytes"]
                    - previous["networkReceiveBytes"]
                )
                transmit_delta = (
                    current["networkTransmitBytes"]
                    - previous["networkTransmitBytes"]
                )
                if cpu_usage_delta < 0:
                    evidence_errors.append(
                        f"telemetry interval {index} has regressed cgroup CPU usage"
                    )
                if receive_delta < 0 or transmit_delta < 0:
                    evidence_errors.append(
                        f"telemetry interval {index} has regressed network counters"
                    )
                if cpu_usage_delta < 0 or receive_delta < 0 or transmit_delta < 0:
                    continue
                cpu_utilization = (
                    (cpu_usage_delta / 1_000_000_000.0)
                    / (elapsed * self.admission.cpu_count)
                    * 100.0
                )
                memory_utilization = (
                    current["memoryCurrentBytes"]
                    / self.admission.memory_bytes
                    * 100.0
                )
                receive_rate = receive_delta / elapsed
                transmit_rate = transmit_delta / elapsed
                derived_values = (
                    cpu_utilization,
                    memory_utilization,
                    receive_rate,
                    transmit_rate,
                )
                if not all(math.isfinite(value) and value >= 0 for value in derived_values):
                    evidence_errors.append(
                        f"telemetry interval {index} has invalid derived values"
                    )
                    continue
                intervals.append(
                    {
                        "startMonotonicSeconds": start_monotonic,
                        "endMonotonicSeconds": end_monotonic,
                        "elapsedSeconds": elapsed,
                        "cpuUtilizationPercent": cpu_utilization,
                        "memoryUtilizationPercent": memory_utilization,
                        "networkReceiveBytesPerSecond": receive_rate,
                        "networkTransmitBytesPerSecond": transmit_rate,
                    }
                )

        sample_count = len(intervals)
        minimum_phase_sample_count = math.ceil(
            self.expected_phase_duration_seconds
            * TELEMETRY_MINIMUM_COVERAGE_FRACTION
        )
        required_minimum_sample_count = max(
            self.admission.minimum_samples, minimum_phase_sample_count
        )
        covered_seconds = sum(sample["elapsedSeconds"] for sample in intervals)
        coverage_fraction = covered_seconds / self.expected_phase_duration_seconds
        maximum_interval = max(
            (sample["elapsedSeconds"] for sample in intervals), default=None
        )
        start_edge_lag = (
            float(raw_samples[0]["monotonicSeconds"]) - float(phase_start)
            if raw_samples_valid and raw_samples and phase_bounds_valid
            else None
        )
        end_edge_lag = (
            float(phase_end) - float(raw_samples[-1]["monotonicSeconds"])
            if raw_samples_valid and raw_samples and phase_bounds_valid
            else None
        )
        timing_valid = (
            source_evidence_valid
            and raw_samples_valid
            and capture_bounds_valid
            and phase_bounds_valid
            and phase_capture_order_valid
            and self._sampler_thread_stopped
            and sample_count == max(0, len(raw_samples) - 1)
            and sample_count >= required_minimum_sample_count
            and coverage_fraction >= TELEMETRY_MINIMUM_COVERAGE_FRACTION
            and maximum_interval is not None
            and maximum_interval <= TELEMETRY_MAX_INTERVAL_SECONDS
            and start_edge_lag is not None
            and 0 <= start_edge_lag <= TELEMETRY_MAX_EDGE_LAG_SECONDS
            and end_edge_lag is not None
            and 0 <= end_edge_lag <= TELEMETRY_MAX_EDGE_LAG_SECONDS
        )
        if sample_count < required_minimum_sample_count:
            evidence_errors.append("telemetry interval count is below phase minimum")
        if coverage_fraction < TELEMETRY_MINIMUM_COVERAGE_FRACTION:
            evidence_errors.append("telemetry coverage is below phase minimum")
        if start_edge_lag is None or not (
            0 <= start_edge_lag <= TELEMETRY_MAX_EDGE_LAG_SECONDS
        ):
            evidence_errors.append("telemetry start edge is not fresh")
        if end_edge_lag is None or not (
            0 <= end_edge_lag <= TELEMETRY_MAX_EDGE_LAG_SECONDS
        ):
            evidence_errors.append("telemetry end edge is not fresh")

        complete = timing_valid
        maximum_cpu = max(
            (sample["cpuUtilizationPercent"] for sample in intervals), default=None
        )
        # Memory is an instantaneous gauge, so include the phase-start raw
        # sample as well as every interval endpoint. CPU remains an interval
        # delta. Omitting rawSamples[0] could hide start-of-phase saturation.
        raw_memory_utilization = (
            [
                sample["memoryCurrentBytes"]
                / self.admission.memory_bytes
                * 100.0
                for sample in raw_samples
            ]
            if raw_samples_valid
            else []
        )
        maximum_memory = max(raw_memory_utilization, default=None)
        def nearest_rank(field: str, percentile: float) -> float | None:
            values = sorted(float(sample[field]) for sample in intervals)
            if not values:
                return None
            rank = max(1, math.ceil(percentile * len(values)))
            return values[rank - 1]

        resource_saturated = (
            not complete
            or maximum_cpu is None
            or maximum_memory is None
            or maximum_cpu > self.admission.maximum_cpu_percent
            or maximum_memory > self.admission.maximum_memory_percent
        )
        link_cap_bits_per_second = min(
            self.admission.download_bits_per_second,
            self.target_upload_bits_per_second,
        )
        link_admission_bytes_per_second = (
            link_cap_bits_per_second * LINK_CAP_ADMISSION_FRACTION / 8.0
        )
        p95_receive = nearest_rank("networkReceiveBytesPerSecond", 0.95)
        link_headroom_valid = (
            timing_valid
            and p95_receive is not None
            and p95_receive <= link_admission_bytes_per_second
        )
        if not link_headroom_valid:
            evidence_errors.append("p95 receive rate exceeds or lacks link admission")
        return {
            "formatVersion": FORMAT_VERSION,
            "pid": self.pid,
            "source": "container-cgroup-and-network-interface",
            "resourceScope": "container-cgroup",
            "cgroupVersion": self._cgroup_version,
            "cgroupCpuPath": (
                str(self._cgroup_cpu_path) if self._cgroup_cpu_path is not None else None
            ),
            "cgroupMemoryPath": (
                str(self._cgroup_memory_path)
                if self._cgroup_memory_path is not None
                else None
            ),
            "networkInterface": self._network_interface,
            "sourceEvidenceValid": source_evidence_valid,
            "samplingIntervalSeconds": self.interval_seconds,
            "captureStartMonotonicSeconds": capture_start,
            "captureStopMonotonicSeconds": capture_stop,
            "phaseStartMonotonicSeconds": phase_start,
            "phaseEndMonotonicSeconds": phase_end,
            "expectedPhaseDurationSeconds": self.expected_phase_duration_seconds,
            "coveredSeconds": covered_seconds,
            "coverageFraction": coverage_fraction,
            "minimumCoverageFraction": TELEMETRY_MINIMUM_COVERAGE_FRACTION,
            "maxIntervalSeconds": maximum_interval,
            "maximumAllowedIntervalSeconds": TELEMETRY_MAX_INTERVAL_SECONDS,
            "startEdgeLagSeconds": start_edge_lag,
            "endEdgeLagSeconds": end_edge_lag,
            "maximumAllowedEdgeLagSeconds": TELEMETRY_MAX_EDGE_LAG_SECONDS,
            "timingEvidenceValid": timing_valid,
            "sampleCount": sample_count,
            "minimumSamples": self.admission.minimum_samples,
            "minimumPhaseSampleCount": minimum_phase_sample_count,
            "requiredMinimumSampleCount": required_minimum_sample_count,
            "complete": complete,
            "maximumCpuUtilizationPercent": maximum_cpu,
            "cpuAdmissionPercent": self.admission.maximum_cpu_percent,
            "maximumMemoryUtilizationPercent": maximum_memory,
            "memoryAdmissionPercent": self.admission.maximum_memory_percent,
            "maximumNetworkReceiveBytesPerSecond": max(
                (sample["networkReceiveBytesPerSecond"] for sample in intervals),
                default=None,
            ),
            "p95NetworkReceiveBytesPerSecond": nearest_rank(
                "networkReceiveBytesPerSecond", 0.95
            ),
            "maximumNetworkTransmitBytesPerSecond": max(
                (sample["networkTransmitBytesPerSecond"] for sample in intervals),
                default=None,
            ),
            "p95NetworkTransmitBytesPerSecond": nearest_rank(
                "networkTransmitBytesPerSecond", 0.95
            ),
            "loadGeneratorDownloadBitsPerSecond": self.admission.download_bits_per_second,
            "targetUploadBitsPerSecond": self.target_upload_bits_per_second,
            "networkLinkCapBitsPerSecond": link_cap_bits_per_second,
            "networkLinkAdmissionFraction": LINK_CAP_ADMISSION_FRACTION,
            "networkLinkAdmissionBytesPerSecond": link_admission_bytes_per_second,
            "networkLinkHeadroomValid": link_headroom_valid,
            "saturated": resource_saturated,
            "valid": complete and not resource_saturated and link_headroom_valid,
            "evidenceErrors": evidence_errors,
            "rawSamples": raw_samples,
            "samples": intervals,
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
    expectations_path: Path | None = None,
    telemetry_path: Path | None = None,
    load_generator_admission: LoadGeneratorAdmission | None = None,
    sampler_interval_seconds: float = 1.0,
) -> tuple[int, dict[str, Any]]:
    if vus != APPROVED_VUS:
        raise BenchmarkError(
            f"k6 VUs must be exactly {APPROVED_VUS} for the approved comparison"
        )
    expected_path_count: int | None = None
    expected_stored_bytes_per_iteration: int | None = None
    if expectations_path is not None:
        benchmark_paths = parse_paths(path_file)
        try:
            expectations = json.loads(
                expectations_path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise BenchmarkError(f"invalid k6 expectations evidence: {error}") from error
        expectation_paths = expectations.get("paths") if isinstance(expectations, dict) else None
        if not isinstance(expectation_paths, dict) or set(expectation_paths) != set(
            benchmark_paths
        ):
            raise BenchmarkError("k6 expectations do not exactly match the path profile")
        stored_lengths: list[int] = []
        for path in benchmark_paths:
            path_expectation = expectation_paths.get(path)
            stored_length = (
                path_expectation.get("storedRepresentationLength")
                if isinstance(path_expectation, dict)
                else None
            )
            if (
                isinstance(stored_length, bool)
                or not isinstance(stored_length, int)
                or stored_length <= 0
            ):
                raise BenchmarkError(
                    f"k6 expectation for {path!r} has no stored representation length"
                )
            stored_lengths.append(stored_length)
        expected_path_count = len(benchmark_paths)
        expected_stored_bytes_per_iteration = sum(stored_lengths)
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
            "TARGET_IDENTITY_HEADER": target.identity_header or "",
            "TARGET_RUNTIME_IDENTITY": (
                target.runtime_identity if target.identity_header is not None else ""
            ),
            "K6_NO_USAGE_REPORT": "true",
            # The comparison is explicitly direct-origin. Do not inherit a
            # workstation or CI HTTP proxy into the measured path.
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "NO_PROXY": "*",
        }
    )
    if expectations_path is not None:
        environment["EXPECTATIONS_FILE"] = str(expectations_path)
    command = [
        k6_binary,
        "run",
        "--quiet",
        "--summary-export",
        str(summary_path),
    ]
    command.append(str(script))
    timeout = duration_seconds(duration) + 120
    sampler: ProcessResourceSampler | None = None
    telemetry: dict[str, Any] | None = None
    phase_end_monotonic_seconds: float | None = None
    disk_free_before = shutil.disk_usage(summary_path.parent).free
    if (
        load_generator_admission is not None
        and disk_free_before < load_generator_admission.minimum_free_disk_bytes
    ):
        raise BenchmarkError(
            "load-generator free disk is below the configured admission minimum"
        )
    if load_generator_admission is not None and target.upload_bits_per_second is None:
        raise BenchmarkError("active target upload link cap is missing")
    try:
        phase_start_monotonic_seconds = time.monotonic()
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if load_generator_admission is not None:
            sampler = ProcessResourceSampler(
                process.pid,
                load_generator_admission,
                expected_phase_duration_seconds=duration_seconds(duration),
                target_upload_bits_per_second=target.upload_bits_per_second,
                interval_seconds=sampler_interval_seconds,
            )
            sampler.start(phase_start_monotonic_seconds)
        stdout, stderr = process.communicate(timeout=timeout)
        phase_end_monotonic_seconds = time.monotonic()
        return_code = process.returncode
    except OSError as error:
        log_path.write_text(
            f"command: {command!r}\nresult: failed to execute: {error}\n",
            encoding="utf-8",
        )
        raise BenchmarkError(f"failed to execute k6 for {target.name}: {error}") from error
    except subprocess.TimeoutExpired as error:
        process.kill()
        stdout, stderr = process.communicate()
        phase_end_monotonic_seconds = time.monotonic()
        log_path.write_text(
            f"command: {command!r}\nresult: timeout after {timeout}s\n"
            f"stdout:\n{stdout or error.stdout or ''}\n"
            f"stderr:\n{stderr or error.stderr or ''}\n",
            encoding="utf-8",
        )
        raise BenchmarkError(
            f"k6 timed out for {target.name} after {timeout} seconds"
        ) from error
    finally:
        if sampler is not None:
            if phase_end_monotonic_seconds is None:
                phase_end_monotonic_seconds = time.monotonic()
            telemetry = sampler.stop(phase_end_monotonic_seconds)
            if telemetry_path is not None:
                telemetry["diskFreeBytesBefore"] = disk_free_before
                telemetry["diskFreeBytesAfter"] = shutil.disk_usage(
                    summary_path.parent
                ).free
                telemetry["minimumFreeDiskBytes"] = (
                    load_generator_admission.minimum_free_disk_bytes
                    if load_generator_admission is not None
                    else None
                )
                write_json(telemetry_path, telemetry)

    log_path.write_text(
        f"command: {command!r}\nexitCode: {return_code}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}\n",
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
    metrics = extract_metrics(
        summary,
        expected_path_count=expected_path_count,
        expected_stored_bytes_per_iteration=expected_stored_bytes_per_iteration,
    )
    metrics.update(
        {
            "summaryEvidenceValid": True,
            "summaryEvidenceBytes": summary_path.stat().st_size,
            "summaryEvidenceSha256": sha256_file(summary_path),
            "logSha256": sha256_file(log_path),
        }
    )
    if load_generator_admission is not None:
        if telemetry is None:
            raise BenchmarkError("load-generator telemetry was not captured")
        metrics.update(
            {
                "loadGeneratorEvidenceValid": telemetry.get("valid") is True,
                "loadGeneratorSaturated": telemetry.get("saturated") is True,
                "loadGeneratorSampleCount": telemetry.get("sampleCount"),
                "loadGeneratorMaximumCpuPercent": telemetry.get(
                    "maximumCpuUtilizationPercent"
                ),
                "loadGeneratorMaximumMemoryPercent": telemetry.get(
                    "maximumMemoryUtilizationPercent"
                ),
                "loadGeneratorP95NetworkReceiveBytesPerSecond": telemetry.get(
                    "p95NetworkReceiveBytesPerSecond"
                ),
                "loadGeneratorP95NetworkTransmitBytesPerSecond": telemetry.get(
                    "p95NetworkTransmitBytesPerSecond"
                ),
                "loadGeneratorTimingEvidenceValid": telemetry.get(
                    "timingEvidenceValid"
                )
                is True,
                "loadGeneratorExpectedPhaseDurationSeconds": telemetry.get(
                    "expectedPhaseDurationSeconds"
                ),
                "loadGeneratorCoveredSeconds": telemetry.get("coveredSeconds"),
                "loadGeneratorCoverageFraction": telemetry.get(
                    "coverageFraction"
                ),
                "loadGeneratorMaxIntervalSeconds": telemetry.get(
                    "maxIntervalSeconds"
                ),
                "loadGeneratorStartEdgeLagSeconds": telemetry.get(
                    "startEdgeLagSeconds"
                ),
                "loadGeneratorEndEdgeLagSeconds": telemetry.get(
                    "endEdgeLagSeconds"
                ),
                "loadGeneratorDownloadBitsPerSecond": telemetry.get(
                    "loadGeneratorDownloadBitsPerSecond"
                ),
                "targetUploadBitsPerSecond": telemetry.get(
                    "targetUploadBitsPerSecond"
                ),
                "networkLinkCapBitsPerSecond": telemetry.get(
                    "networkLinkCapBitsPerSecond"
                ),
                "networkLinkAdmissionFraction": telemetry.get(
                    "networkLinkAdmissionFraction"
                ),
                "networkLinkAdmissionBytesPerSecond": telemetry.get(
                    "networkLinkAdmissionBytesPerSecond"
                ),
                "networkLinkHeadroomValid": telemetry.get(
                    "networkLinkHeadroomValid"
                )
                is True,
                "loadGeneratorDiskFreeBytesBefore": telemetry.get(
                    "diskFreeBytesBefore"
                ),
                "loadGeneratorDiskFreeBytesAfter": telemetry.get(
                    "diskFreeBytesAfter"
                ),
                "loadGeneratorDiskEvidenceValid": (
                    isinstance(telemetry.get("diskFreeBytesBefore"), int)
                    and isinstance(telemetry.get("diskFreeBytesAfter"), int)
                    and telemetry["diskFreeBytesBefore"]
                    >= load_generator_admission.minimum_free_disk_bytes
                    and telemetry["diskFreeBytesAfter"]
                    >= load_generator_admission.minimum_free_disk_bytes
                ),
                "telemetrySha256": (
                    sha256_file(telemetry_path)
                    if telemetry_path is not None and telemetry_path.is_file()
                    else None
                ),
            }
        )
    return return_code, metrics


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run_evidence_is_valid(entry: Mapping[str, Any]) -> bool:
    zero_fields = (
        "errors",
        "httpErrors",
        "transportErrors",
        "proxyHeaderErrors",
        "encodingErrors",
        "contentTypeErrors",
        "contentLengthErrors",
        "bodyLengthErrors",
        "identityErrors",
        "cacheValidatorErrors",
        "droppedIterations",
        "k6DroppedIterations",
        "httpFailures",
    )
    basic_valid = (
        entry.get("k6ExitCode") == 0
        and all(
            isinstance(entry.get(field), int)
            and not isinstance(entry.get(field), bool)
            and entry[field] == 0
            for field in zero_fields
        )
        and entry.get("httpFailureRate") == 0
        and entry.get("checkFailureRate") == 0
        and entry.get("summaryEvidenceValid") is True
        and entry.get("loadGeneratorEvidenceValid") is True
        and entry.get("loadGeneratorTimingEvidenceValid") is True
        and entry.get("networkLinkHeadroomValid") is True
        and entry.get("loadGeneratorDiskEvidenceValid") is True
        and entry.get("loadGeneratorSaturated") is False
    )
    if not basic_valid:
        return False
    numeric_fields = (
        "requestsPerSecond",
        "storedMibPerSecond",
        "networkMibPerSecond",
        "p50Milliseconds",
        "p95Milliseconds",
        "p99Milliseconds",
        "httpFailureRate",
        "checkFailureRate",
        "loadGeneratorMaximumCpuPercent",
        "loadGeneratorMaximumMemoryPercent",
        "loadGeneratorP95NetworkReceiveBytesPerSecond",
        "loadGeneratorP95NetworkTransmitBytesPerSecond",
        "loadGeneratorExpectedPhaseDurationSeconds",
        "loadGeneratorCoveredSeconds",
        "loadGeneratorCoverageFraction",
        "loadGeneratorMaxIntervalSeconds",
        "loadGeneratorStartEdgeLagSeconds",
        "loadGeneratorEndEdgeLagSeconds",
        "networkLinkAdmissionFraction",
        "networkLinkAdmissionBytesPerSecond",
    )
    expected_phase_duration = {
        "warmup": duration_seconds(APPROVED_WARMUP_DURATION),
        "measurement": duration_seconds(APPROVED_MEASUREMENT_DURATION),
    }.get(entry.get("phase"))
    minimum_phase_sample_count = (
        math.ceil(expected_phase_duration * TELEMETRY_MINIMUM_COVERAGE_FRACTION)
        if expected_phase_duration is not None
        else None
    )
    numeric_evidence_valid = all(
        isinstance(entry.get(field), (int, float))
        and not isinstance(entry.get(field), bool)
        and math.isfinite(float(entry[field]))
        and float(entry[field]) >= 0
        for field in numeric_fields
    )
    if not numeric_evidence_valid:
        return False
    return basic_valid and (
        expected_phase_duration is not None
        and entry.get("loadGeneratorExpectedPhaseDurationSeconds")
        == expected_phase_duration
        and isinstance(entry.get("requests"), int)
        and not isinstance(entry.get("requests"), bool)
        and entry["requests"] > 0
        and isinstance(entry.get("networkReceivedBytes"), int)
        and not isinstance(entry.get("networkReceivedBytes"), bool)
        and entry["networkReceivedBytes"] >= 0
        and isinstance(entry.get("storedRepresentationBytes"), int)
        and not isinstance(entry.get("storedRepresentationBytes"), bool)
        and entry["storedRepresentationBytes"] > 0
        and isinstance(entry.get("iterations"), int)
        and not isinstance(entry.get("iterations"), bool)
        and entry["iterations"] > 0
        and entry.get("observedResponses") == entry["requests"]
        and isinstance(entry.get("loadGeneratorSampleCount"), int)
        and not isinstance(entry.get("loadGeneratorSampleCount"), bool)
        and entry["loadGeneratorSampleCount"] >= minimum_phase_sample_count
        and float(entry.get("loadGeneratorCoverageFraction", -1))
        >= TELEMETRY_MINIMUM_COVERAGE_FRACTION
        and float(entry.get("loadGeneratorMaxIntervalSeconds", math.inf))
        <= TELEMETRY_MAX_INTERVAL_SECONDS
        and 0
        <= float(entry.get("loadGeneratorStartEdgeLagSeconds", -1))
        <= TELEMETRY_MAX_EDGE_LAG_SECONDS
        and 0
        <= float(entry.get("loadGeneratorEndEdgeLagSeconds", -1))
        <= TELEMETRY_MAX_EDGE_LAG_SECONDS
        and isinstance(entry.get("loadGeneratorDownloadBitsPerSecond"), int)
        and not isinstance(entry.get("loadGeneratorDownloadBitsPerSecond"), bool)
        and entry["loadGeneratorDownloadBitsPerSecond"] > 0
        and isinstance(entry.get("targetUploadBitsPerSecond"), int)
        and not isinstance(entry.get("targetUploadBitsPerSecond"), bool)
        and entry["targetUploadBitsPerSecond"] > 0
        and entry.get("networkLinkCapBitsPerSecond")
        == min(
            entry["loadGeneratorDownloadBitsPerSecond"],
            entry["targetUploadBitsPerSecond"],
        )
        and entry.get("networkLinkAdmissionFraction")
        == LINK_CAP_ADMISSION_FRACTION
        and entry.get("networkLinkAdmissionBytesPerSecond")
        == entry["networkLinkCapBitsPerSecond"]
        * LINK_CAP_ADMISSION_FRACTION
        / 8.0
        and entry["loadGeneratorP95NetworkReceiveBytesPerSecond"]
        <= entry["networkLinkAdmissionBytesPerSecond"]
    )


def measurement_is_valid(entry: Mapping[str, Any]) -> bool:
    return (
        entry.get("status") == "completed"
        and entry.get("warmupExitCode") == 0
        and run_evidence_is_valid(entry)
    )


def summarize_measurements(
    output: Path,
    runs: Sequence[Mapping[str, Any]],
    expected_repetitions: int,
    expected_schedule: Sequence[Mapping[str, Any]] | None = None,
    profile_id: str | None = None,
) -> bool:
    if expected_repetitions <= 0:
        raise BenchmarkError("expected repetitions must be positive")
    measurements = [run for run in runs if run.get("phase") == "measurement"]
    fields = [
        "status",
        "error",
        "variant",
        "profileId",
        "repetition",
        "orderPosition",
        "requests",
        "requestsPerSecond",
        "storedMibPerSecond",
        "networkMibPerSecond",
        "p50Milliseconds",
        "p95Milliseconds",
        "p99Milliseconds",
        "httpFailureRate",
        "httpFailures",
        "transportErrors",
        "droppedIterations",
        "k6DroppedIterations",
        "errors",
        "summaryEvidenceValid",
        "summaryEvidenceSha256",
        "summaryEvidenceBytes",
        "loadGeneratorEvidenceValid",
        "loadGeneratorSaturated",
        "loadGeneratorMaximumCpuPercent",
        "loadGeneratorMaximumMemoryPercent",
        "loadGeneratorP95NetworkReceiveBytesPerSecond",
        "loadGeneratorP95NetworkTransmitBytesPerSecond",
        "loadGeneratorTimingEvidenceValid",
        "loadGeneratorExpectedPhaseDurationSeconds",
        "loadGeneratorCoveredSeconds",
        "loadGeneratorCoverageFraction",
        "loadGeneratorMaxIntervalSeconds",
        "loadGeneratorStartEdgeLagSeconds",
        "loadGeneratorEndEdgeLagSeconds",
        "loadGeneratorDownloadBitsPerSecond",
        "targetUploadBitsPerSecond",
        "networkLinkCapBitsPerSecond",
        "networkLinkAdmissionFraction",
        "networkLinkAdmissionBytesPerSecond",
        "networkLinkHeadroomValid",
        "loadGeneratorDiskEvidenceValid",
        "loadGeneratorDiskFreeBytesBefore",
        "loadGeneratorDiskFreeBytesAfter",
        "warmupExitCode",
        "k6ExitCode",
        "summaryFile",
        "telemetryFile",
        "logFile",
    ]
    with (output / "measurements.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for measurement in measurements:
            writer.writerow({field: measurement.get(field) for field in fields})

    variants: dict[str, list[Mapping[str, Any]]] = {}
    for measurement in measurements:
        variant = measurement.get("variant")
        variants.setdefault(variant if isinstance(variant, str) else "<missing>", []).append(
            measurement
        )

    block_summaries: list[dict[str, Any]] = []
    blocks_valid = expected_schedule is None or len(expected_schedule) == expected_repetitions
    for repetition in range(1, expected_repetitions + 1):
        entries = [
            entry for entry in measurements if entry.get("repetition") == repetition
        ]
        by_position = sorted(
            entries,
            key=lambda entry: (
                entry.get("orderPosition")
                if isinstance(entry.get("orderPosition"), int)
                and not isinstance(entry.get("orderPosition"), bool)
                else -1
            ),
        )
        observed_order = [entry.get("variant") for entry in by_position]
        expected_order = None
        if expected_schedule is not None:
            if repetition > len(expected_schedule):
                expected_order = []
            else:
                candidate = expected_schedule[repetition - 1].get("order")
                expected_order = candidate if isinstance(candidate, list) else []
        shape_valid = (
            len(entries) == len(VARIANTS)
            and [entry.get("orderPosition") for entry in by_position]
            == list(range(1, len(VARIANTS) + 1))
            and set(observed_order) == set(VARIANTS)
            and (expected_order is None or observed_order == expected_order)
            and (
                profile_id is None
                or all(entry.get("profileId") == profile_id for entry in entries)
            )
        )
        valid_runs = sum(1 for entry in entries if measurement_is_valid(entry))
        block_valid = shape_valid and valid_runs == len(VARIANTS)
        blocks_valid = blocks_valid and block_valid
        block_summaries.append(
            {
                "block": repetition,
                "expectedOrder": expected_order,
                "observedOrder": observed_order,
                "runCount": len(entries),
                "validRuns": valid_runs,
                "valid": block_valid,
            }
        )

    summaries: list[dict[str, Any]] = []
    all_valid = (
        set(variants) == set(VARIANTS)
        and len(measurements) == expected_repetitions * len(VARIANTS)
        and blocks_valid
    )
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
            "totalHttpErrors": sum(
                int(entry["httpErrors"])
                for entry in entries
                if isinstance(entry.get("httpErrors"), int)
                and not isinstance(entry.get("httpErrors"), bool)
            ),
            "totalTransportErrors": sum(
                int(entry["transportErrors"])
                for entry in entries
                if isinstance(entry.get("transportErrors"), int)
                and not isinstance(entry.get("transportErrors"), bool)
            ),
            "totalDroppedIterations": sum(
                int(entry["droppedIterations"])
                + int(entry["k6DroppedIterations"])
                for entry in entries
                if isinstance(entry.get("droppedIterations"), int)
                and not isinstance(entry.get("droppedIterations"), bool)
                and isinstance(entry.get("k6DroppedIterations"), int)
                and not isinstance(entry.get("k6DroppedIterations"), bool)
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
                    "medianStoredMibPerSecond": statistics.median(
                        float(entry["storedMibPerSecond"]) for entry in entries
                    ),
                    "medianNetworkMibPerSecond": statistics.median(
                        float(entry["networkMibPerSecond"]) for entry in entries
                    ),
                    "medianP50Milliseconds": statistics.median(
                        float(entry["p50Milliseconds"]) for entry in entries
                    ),
                    "medianP95Milliseconds": statistics.median(
                        float(entry["p95Milliseconds"]) for entry in entries
                    ),
                    "medianP99Milliseconds": statistics.median(
                        float(entry["p99Milliseconds"]) for entry in entries
                    ),
                }
            )

    write_json(
        output / "summary.json",
        {
            "formatVersion": FORMAT_VERSION,
            "valid": all_valid,
            "profileId": profile_id,
            "failedBlocks": sum(1 for block in block_summaries if not block["valid"]),
            "blocks": block_summaries,
            "variants": summaries,
        },
    )
    summary_fields = [
        "variant",
        "repetitions",
        "expectedRepetitions",
        "missingRuns",
        "medianRequestsPerSecond",
        "minimumRequestsPerSecond",
        "maximumRequestsPerSecond",
        "medianStoredMibPerSecond",
        "medianNetworkMibPerSecond",
        "medianP50Milliseconds",
        "medianP95Milliseconds",
        "medianP99Milliseconds",
        "totalErrors",
        "totalHttpErrors",
        "totalTransportErrors",
        "totalDroppedIterations",
        "failedRuns",
    ]
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summaries)

    lines = [
        "# Throughput summary",
        "",
        f"Profile: `{profile_id or 'unspecified'}`",
        "",
        "| Variant | Blocks | Median req/s | Stored MiB/s | Network MiB/s | Median p50/p95/p99 (ms) | Errors | Failed runs |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for summary in summaries:
        if all_valid:
            lines.append(
                "| {variant} | {repetitions} | {medianRequestsPerSecond:.2f} | "
                "{medianStoredMibPerSecond:.2f} | {medianNetworkMibPerSecond:.2f} | "
                "{medianP50Milliseconds:.2f} / "
                "{medianP95Milliseconds:.2f} / {medianP99Milliseconds:.2f} | "
                "{totalErrors} | {failedRuns} |".format(**summary)
            )
        else:
            lines.append(
                "| {variant} | {repetitions}/{expectedRepetitions} | — | — | — | — | "
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

        if arguments.vus != APPROVED_VUS:
            raise BenchmarkError(
                f"--vus must be exactly {APPROVED_VUS} for the approved comparison"
            )
        if arguments.repetitions != APPROVED_REPETITIONS:
            raise BenchmarkError(
                f"--repetitions must be exactly {APPROVED_REPETITIONS} for the "
                "approved complete block matrix"
            )
        if (
            not math.isfinite(arguments.preflight_timeout_seconds)
            or arguments.preflight_timeout_seconds <= 0
        ):
            raise BenchmarkError("--preflight-timeout-seconds must be positive")

        warmup_duration = validate_duration(
            arguments.warmup_duration, "--warmup-duration"
        )
        measurement_duration = validate_duration(arguments.duration, "--duration")
        if warmup_duration != APPROVED_WARMUP_DURATION:
            raise BenchmarkError(
                f"--warmup-duration must be exactly {APPROVED_WARMUP_DURATION}"
            )
        if measurement_duration != APPROVED_MEASUREMENT_DURATION:
            raise BenchmarkError(
                f"--duration must be exactly {APPROVED_MEASUREMENT_DURATION}"
            )
        accept_encoding = validate_encoding(
            arguments.accept_encoding, "--accept-encoding"
        )
        required_content_encoding = validate_encoding(
            arguments.required_content_encoding, "--required-content-encoding"
        )
        upstream_id = validate_identifier(arguments.upstream_id, "--upstream-id")
        new_java_id = validate_identifier(arguments.new_java_id, "--new-java-id")
        dataset_id = validate_identifier(arguments.dataset_id, "--dataset-id")
        setup_manifest, setup_manifest_raw = load_setup_manifest(
            arguments.setup_manifest, dataset_id
        )
        admission = load_generator_admission(setup_manifest)
        frozen_setup_manifest = output / "setup-manifest.json"
        frozen_setup_manifest.write_bytes(setup_manifest_raw)

        targets = [
            Target(
                "upstream",
                validate_url(arguments.upstream_url, "--upstream-url"),
                upstream_id,
                setup_manifest["targets"]["upstream"]["runtimeIdentity"],
                setup_manifest["targets"]["upstream"].get("identityHeader"),
                setup_manifest["targets"]["upstream"]["uploadBitsPerSecond"],
            ),
            Target(
                "upstream-php",
                validate_url(arguments.upstream_php_url, "--upstream-php-url"),
                upstream_id,
                setup_manifest["targets"]["upstream-php"]["runtimeIdentity"],
                setup_manifest["targets"]["upstream-php"].get("identityHeader"),
                setup_manifest["targets"]["upstream-php"]["uploadBitsPerSecond"],
            ),
            Target(
                "new-java",
                validate_url(arguments.new_java_url, "--new-java-url"),
                new_java_id,
                setup_manifest["targets"]["new-java"]["runtimeIdentity"],
                setup_manifest["targets"]["new-java"].get("identityHeader"),
                setup_manifest["targets"]["new-java"]["uploadBitsPerSecond"],
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
        derived_profile_material = json.dumps(
            {
                "paths": paths,
                "datasetId": dataset_id,
                "acceptEncoding": accept_encoding,
                "requiredContentEncoding": required_content_encoding,
                "realTimeMetricStream": (
                    "disabled to avoid benchmark-client CPU, I/O, and disk bias"
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        profile_id = (
            validate_identifier(arguments.profile_id, "--profile-id")
            if arguments.profile_id is not None
            else f"sha256:{sha256_bytes(derived_profile_material)}"
        )
        schedule_seed = validate_identifier(
            arguments.schedule_seed or secrets.token_hex(16), "--schedule-seed"
        )
        schedule = create_schedule(targets, arguments.repetitions, schedule_seed)
        write_json(
            output / "schedule.json",
            {
                "formatVersion": FORMAT_VERSION,
                "seed": schedule_seed,
                "blocks": schedule,
            },
        )
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
                        "runtimeIdentity": target.runtime_identity,
                        "identityHeader": target.identity_header,
                        "uploadBitsPerSecond": target.upload_bits_per_second,
                    }
                    for target in targets
                ],
                "outOfBandTargetIdentities": {
                    variant: {
                        field: setup_manifest["targets"][variant][field]
                        for field in (
                            "runpodPodId",
                            "imageDigest",
                            "processIdentitySha256",
                            "runtimeProbeSha256",
                            "configurationSha256",
                            "uploadBitsPerSecond",
                        )
                    }
                    for variant in VARIANTS
                },
                "datasetId": dataset_id,
                "setupManifestSha256": sha256_bytes(setup_manifest_raw),
                "benchmarkFilesSha256": benchmark_files,
                "pathCount": len(paths),
                "profileId": profile_id,
                "pathsSha256": sha256_bytes(frozen_text.encode("utf-8")),
                "vus": arguments.vus,
                "warmupDuration": warmup_duration,
                "measurementDuration": measurement_duration,
                "repetitions": arguments.repetitions,
                "scheduleSeed": schedule_seed,
                "scheduleFile": "schedule.json",
                "acceptEncoding": accept_encoding,
                "requiredContentEncoding": required_content_encoding,
                "loadGeneratorDownloadBitsPerSecond": (
                    admission.download_bits_per_second
                ),
                "networkLinkAdmissionFraction": LINK_CAP_ADMISSION_FRACTION,
                "k6Version": (version_result.stdout or version_result.stderr).strip(),
                "pythonVersion": platform.python_version(),
                "platform": platform.platform(),
            }
        )
        write_json(output / "metadata.json", metadata)

        print(
            "Validating status, encoding, and stored/decoded representations ...",
            flush=True,
        )
        preflight_evidence = preflight(
            targets,
            paths,
            accept_encoding,
            required_content_encoding,
            arguments.preflight_timeout_seconds,
            evidence_path=output / "preflight.json",
        )
        expectations_path = output / "expectations.json"
        write_json(expectations_path, build_expectations(preflight_evidence))
        metadata["preflightSha256"] = sha256_file(output / "preflight.json")
        metadata["expectationsSha256"] = sha256_file(expectations_path)
        write_json(output / "metadata.json", metadata)

        def execute_phase(
            *,
            phase: str,
            target: Target,
            repetition: int,
            order_position: int,
            prefix: str,
            duration: str,
            warmup_exit_code: int | None = None,
        ) -> int | None:
            record: dict[str, Any] = {
                "phase": phase,
                "variant": target.name,
                "profileId": profile_id,
                "artifactId": target.artifact_id,
                "runtimeIdentity": target.runtime_identity,
                "repetition": repetition,
                "orderPosition": order_position,
                "status": "running",
                "summaryFile": f"{phase}-{prefix}.json",
                "telemetryFile": f"{phase}-{prefix}-loadgen.json",
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
                    expectations_path=expectations_path,
                    telemetry_path=output / record["telemetryFile"],
                    load_generator_admission=admission,
                )
            except BenchmarkError as error:
                record["status"] = "failed"
                record["error"] = str(error)
                write_json(output / "runs.json", runs)
                return None
            record.update(
                {
                    "status": "completed",
                    "k6ExitCode": exit_code,
                    **metrics,
                }
            )
            if not run_evidence_is_valid(record):
                record["status"] = "failed"
                record["error"] = (
                    "k6, correctness, summary, telemetry-coverage, resource, disk, "
                    "or network-link admission failed"
                )
                write_json(output / "runs.json", runs)
                return None
            write_json(output / "runs.json", runs)
            return exit_code

        for repetition in range(1, arguments.repetitions + 1):
            order = schedule_order(schedule, targets, repetition)
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
                if warmup_exit is None or warmup_exit != 0:
                    runs.append(
                        {
                            "phase": "measurement",
                            "variant": target.name,
                            "profileId": profile_id,
                            "artifactId": target.artifact_id,
                            "runtimeIdentity": target.runtime_identity,
                            "repetition": repetition,
                            "orderPosition": order_position,
                            "status": "failed",
                            "warmupExitCode": warmup_exit,
                            "error": "measurement denied because warmup evidence was invalid",
                        }
                    )
                    write_json(output / "runs.json", runs)
                else:
                    execute_phase(
                        phase="measurement",
                        target=target,
                        repetition=repetition,
                        order_position=order_position,
                        prefix=prefix,
                        duration=measurement_duration,
                        warmup_exit_code=warmup_exit,
                    )

        valid = summarize_measurements(
            output,
            runs,
            arguments.repetitions,
            expected_schedule=schedule,
            profile_id=profile_id,
        )
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
