#!/usr/bin/env python3
"""Safely orchestrate the frozen 80-entry BlueMap benchmark schedule.

The durable Kubernetes controller runs this tracked helper. Its mutation
surface is limited to scaling six exact disposable Deployments, while every
HTTP load phase is delegated to a frozen external RunPod CPU generator.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BENCHMARK_ROOT = SCRIPT_DIR.parents[1]
REPOSITORY_ROOT = BENCHMARK_ROOT.parents[1]
TOOLS_DIR = BENCHMARK_ROOT / "tools"
FROZEN_DIR = SCRIPT_DIR.parent / "frozen"
ANALYZER = SCRIPT_DIR / "analyze.py"
FREEZER = SCRIPT_DIR / "freeze.py"
CONTROL_LOCK = FROZEN_DIR / "controller-lock.json"

DEFAULT_FORMAL_INPUTS = FROZEN_DIR / "formal-inputs"
DEFAULT_MATRIX = DEFAULT_FORMAL_INPUTS / "matrix.json"
DEFAULT_SCHEDULE = DEFAULT_FORMAL_INPUTS / "schedule.json"
DEFAULT_ADMISSION_IDENTITIES = (
    DEFAULT_FORMAL_INPUTS / "runtime-admission-identities.json"
)
DEFAULT_BUNDLE_MANIFEST = DEFAULT_FORMAL_INPUTS / "bundle-manifest.json"
DEFAULT_MANIFEST = FROZEN_DIR / "manifest.json"
DEFAULT_RUNNER = TOOLS_DIR / "run_origin_case.sh"
DEFAULT_GENERATOR = TOOLS_DIR / "generate_schedule.py"
DEFAULT_KUBECONFIG = Path("/root/.kube/guenter-cloud")
DEFAULT_PROMETHEUS_URL = (
    "http://rancher-monitoring-prometheus.cattle-monitoring-system.svc:9090"
)
DEFAULT_TRAFFIC_BASE_URL = "https://bluemap-test.guenter.cloud"

NAMESPACE = "minecraft"
DATABASE_POD = "bluemap-perf-postgres-0"
TRAFFIC_SERVICE = "bluemap-perf-public"
TRAFFIC_SERVICE_PORT = 8100
CONFIRMATION = "RUN-FROZEN-80-ENTRY-MATRIX"

PROTECTED_RESOURCES = frozenset(
    {
        "deployment/minecraft",
        "persistentvolumeclaim/minecraft-data",
        "pvc/minecraft-data",
        "pod/minecraft-maintenance-holder",
    }
)


class SafetyError(RuntimeError):
    """Raised when an invariant prevents safe formal execution."""


@dataclass(frozen=True)
class VariantTarget:
    variant_id: str
    release: str
    service: str
    port: int
    deployment: str
    configmaps: tuple[str, ...]
    contract_mode: str
    implementation: str
    replica_count: int
    experiment_id: str


TARGETS: dict[str, VariantTarget] = {
    "php-postgresql": VariantTarget(
        variant_id="php-postgresql",
        release="bluemap-perf-java",
        service="bluemap-perf-java-php",
        port=8080,
        deployment="bluemap-perf-java-php",
        configmaps=(
            "bluemap-perf-java-php-fpm",
            "bluemap-perf-java-php-nginx",
        ),
        contract_mode="legacy",
        implementation="php",
        replica_count=1,
        experiment_id="php-postgresql-baseline",
    ),
    "java-old-postgresql": VariantTarget(
        variant_id="java-old-postgresql",
        release="bluemap-perf-java",
        service="bluemap-perf-java",
        port=8100,
        deployment="bluemap-perf-java",
        configmaps=(
            "bluemap-perf-java-config",
            "bluemap-perf-java-storage",
        ),
        contract_mode="legacy",
        implementation="java",
        replica_count=1,
        experiment_id="java-postgresql",
    ),
    "java-new-postgresql": VariantTarget(
        variant_id="java-new-postgresql",
        release="bluemap-perf-java-new-postgresql",
        service="bluemap-perf-java-new-postgresql",
        port=8100,
        deployment="bluemap-perf-java-new-postgresql",
        configmaps=(
            "bluemap-perf-java-new-postgresql-config",
            "bluemap-perf-java-new-postgresql-storage",
        ),
        contract_mode="enhanced",
        implementation="java",
        replica_count=1,
        experiment_id="java-new-postgresql",
    ),
    "rust-postgresql": VariantTarget(
        variant_id="rust-postgresql",
        release="bluemap-perf-rust-postgresql",
        service="bluemap-perf-rust-postgresql",
        port=8100,
        deployment="bluemap-perf-rust-postgresql",
        configmaps=("bluemap-perf-rust-postgresql-rust",),
        contract_mode="enhanced",
        implementation="rust",
        replica_count=1,
        experiment_id="rust-postgresql",
    ),
    "java-new-postgresql-r3": VariantTarget(
        variant_id="java-new-postgresql-r3",
        release="bluemap-perf-java-new-postgresql-r3",
        service="bluemap-perf-java-new-postgresql-r3",
        port=8100,
        deployment="bluemap-perf-java-new-postgresql-r3",
        configmaps=(
            "bluemap-perf-java-new-postgresql-r3-config",
            "bluemap-perf-java-new-postgresql-r3-storage",
        ),
        contract_mode="enhanced",
        implementation="java",
        replica_count=3,
        experiment_id="java-new-postgresql-r3",
    ),
    "rust-postgresql-r3": VariantTarget(
        variant_id="rust-postgresql-r3",
        release="bluemap-perf-rust-postgresql-r3",
        service="bluemap-perf-rust-postgresql-r3",
        port=8100,
        deployment="bluemap-perf-rust-postgresql-r3",
        configmaps=("bluemap-perf-rust-postgresql-r3-rust",),
        contract_mode="enhanced",
        implementation="rust",
        replica_count=3,
        experiment_id="rust-postgresql-r3",
    ),
}

FORMAL_DEPLOYMENTS = tuple(sorted({target.deployment for target in TARGETS.values()}))
EXPECTED_VARIANTS = frozenset(TARGETS)
RESOURCE_NAME = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*$"
)
SERVICE_ACCOUNT_VOLUME_NAME = re.compile(r"^kube-api-access-[a-z0-9]{5}$")
ADMISSION_POD_SPEC_IDENTITY_VERSION = 1


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def is_service_account_projection(volume: dict[str, Any]) -> bool:
    name = volume.get("name")
    projected = volume.get("projected")
    if (
        not isinstance(name, str)
        or SERVICE_ACCOUNT_VOLUME_NAME.fullmatch(name) is None
        or not isinstance(projected, dict)
    ):
        return False
    sources = projected.get("sources")
    return isinstance(sources, list) and any(
        isinstance(source, dict) and isinstance(source.get("serviceAccountToken"), dict)
        for source in sources
    )


def normalize_admitted_pod_spec(pod: dict[str, Any]) -> dict[str, Any]:
    if pod.get("kind") != "Pod":
        raise SafetyError("Admission identity input must be a Pod")
    spec = pod.get("spec")
    if not isinstance(spec, dict):
        raise SafetyError("Admission identity Pod has no spec")
    normalized = json.loads(json.dumps(spec))
    normalized.pop("nodeName", None)

    generated_volume_names: dict[str, str] = {}
    volumes = normalized.get("volumes", [])
    if not isinstance(volumes, list):
        raise SafetyError("Admission identity Pod spec.volumes is not an array")
    for volume in volumes:
        if not isinstance(volume, dict):
            raise SafetyError("Admission identity Pod has a non-object volume")
        if is_service_account_projection(volume):
            original = volume["name"]
            replacement = "<kubernetes-service-account-projection>"
            if replacement in generated_volume_names.values():
                raise SafetyError(
                    "Admission identity Pod has multiple generated "
                    "service-account projections"
                )
            generated_volume_names[original] = replacement
            volume["name"] = replacement

    for field in ("containers", "initContainers", "ephemeralContainers"):
        containers = normalized.get(field, [])
        if not isinstance(containers, list):
            raise SafetyError(f"Admission identity Pod spec.{field} is not an array")
        for container in containers:
            if not isinstance(container, dict):
                raise SafetyError(
                    f"Admission identity Pod spec.{field} contains a non-object"
                )
            for mount_field in ("volumeMounts", "volumeDevices"):
                mounts = container.get(mount_field, [])
                if not isinstance(mounts, list):
                    raise SafetyError(
                        f"Admission identity Pod {field}.{mount_field} is not an array"
                    )
                for mount in mounts:
                    if not isinstance(mount, dict):
                        raise SafetyError(
                            f"Admission identity Pod {field}.{mount_field} "
                            "contains a non-object"
                        )
                    name = mount.get("name")
                    if isinstance(name, str) and name in generated_volume_names:
                        mount["name"] = generated_volume_names[name]
    return normalized


def admitted_pod_spec_sha256(pod: dict[str, Any]) -> str:
    identity = {
        "formatVersion": ADMISSION_POD_SPEC_IDENTITY_VERSION,
        "normalizedPodSpec": normalize_admitted_pod_spec(pod),
    }
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SafetyError(f"Could not load JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise SafetyError(f"{path} must contain a JSON object")
    return value


def load_runpod_identity(path: Path, formal_run_id: str) -> dict[str, Any]:
    """Load and strictly validate the non-secret frozen RunPod identity."""
    if not path.is_file() or path.is_symlink():
        raise SafetyError(
            "RunPod load-generator identity must be a regular, non-symlink file"
        )
    identity = load_json(path)
    runpod = identity.get("runpod")
    ssh = identity.get("ssh")
    if (
        identity.get("formatVersion") != 1
        or identity.get("backend") != "runpod-ssh"
        or identity.get("runId") != formal_run_id
        or not isinstance(runpod, dict)
        or not isinstance(ssh, dict)
        or ssh.get("user") != "loadgen"
        or identity.get("remoteRoot") != "/artifacts"
    ):
        raise SafetyError(
            "RunPod identity format, backend, run ID, SSH user, or remote root differs"
        )

    def nonempty_string(value: Any) -> bool:
        return isinstance(value, str) and bool(value)

    for field in ("podId", "machineId", "dataCenterId"):
        if not nonempty_string(runpod.get(field)):
            raise SafetyError(f"RunPod identity runpod.{field} is invalid")
    if (
        runpod.get("cpuFlavorId") != "cpu5c"
        or runpod.get("vcpuCount") != 8
        or runpod.get("minDownloadMbps") != 500
        or runpod.get("minUploadMbps") != 100
        or runpod.get("secureCloud") is not True
    ):
        raise SafetyError(
            "RunPod identity must use exact cpu5c/8-vCPU/500/100 Secure Cloud controls"
        )
    for field, minimum in (
        ("maxDownloadMbps", 500),
        ("maxUploadMbps", 100),
    ):
        value = runpod.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < minimum
        ):
            raise SafetyError(f"RunPod identity runpod.{field} is invalid")
    image_digest = runpod.get("imageDigest")
    if (
        not isinstance(image_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None
    ):
        raise SafetyError("RunPod identity image digest is invalid")
    host = ssh.get("host")
    if (
        not isinstance(host, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host) is None
    ):
        raise SafetyError("RunPod identity SSH host is invalid")
    port = ssh.get("port")
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        raise SafetyError("RunPod identity SSH port is invalid")
    host_key = ssh.get("hostKey")
    if (
        not isinstance(host_key, str)
        or re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+", host_key) is None
    ):
        raise SafetyError("RunPod identity SSH host key is invalid")
    return identity


def validate_runpod_controls(args: argparse.Namespace) -> dict[str, Any]:
    if args.load_generator_backend != "runpod-ssh":
        raise SafetyError("Formal load generation must use runpod-ssh")
    if (
        not isinstance(args.formal_run_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.formal_run_id) is None
    ):
        raise SafetyError("Formal run ID is invalid")
    if args.traffic_base_url != DEFAULT_TRAFFIC_BASE_URL:
        raise SafetyError(
            f"Traffic base URL must exactly equal {DEFAULT_TRAFFIC_BASE_URL}"
        )
    if (
        args.traffic_service != TRAFFIC_SERVICE
        or args.traffic_service_port != TRAFFIC_SERVICE_PORT
    ):
        raise SafetyError("Traffic Service identity differs from the public router")
    if not args.require_edge_bypass:
        raise SafetyError("Formal RunPod traffic requires the edge-bypass proof")
    identity_key = args.load_generator_identity_key
    if not identity_key.is_file() or identity_key.is_symlink():
        raise SafetyError(
            "RunPod SSH key must be a regular, non-symlink file"
        )
    mode = identity_key.stat().st_mode & 0o777
    if mode & 0o077:
        raise SafetyError("RunPod SSH key must not be accessible by group or others")
    if identity_key.stat().st_uid != os.getuid():
        raise SafetyError("RunPod SSH key must be owned by the controller user")
    return load_runpod_identity(args.load_generator_identity, args.formal_run_id)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def run_checked(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SafetyError(
            f"Command failed ({result.returncode}): {' '.join(command)}"
            + (f"\n{detail}" if detail else "")
        )
    return result


def validate_target_constants() -> None:
    if set(TARGETS) != EXPECTED_VARIANTS or len(TARGETS) != 6:
        raise SafetyError("The formal target map must contain exactly six variants")
    if len(FORMAL_DEPLOYMENTS) != 6:
        raise SafetyError("Every formal variant must have one unique Deployment")
    for target in TARGETS.values():
        for name in (
            target.release,
            target.service,
            target.deployment,
            *target.configmaps,
        ):
            if (
                not name.startswith("bluemap-perf-")
                or RESOURCE_NAME.fullmatch(name) is None
            ):
                raise SafetyError(f"Unsafe target resource name: {name!r}")
        if f"deployment/{target.deployment}" in PROTECTED_RESOURCES:
            raise SafetyError("A protected Deployment entered the target allowlist")
        if target.contract_mode not in {"legacy", "enhanced"}:
            raise SafetyError(f"Invalid contract mode for {target.variant_id}")
        if target.replica_count not in {1, 3}:
            raise SafetyError(f"Invalid replica count for {target.variant_id}")


def validate_formal_documents(
    matrix_path: Path,
    schedule_path: Path,
    manifest_path: Path,
    generator_path: Path,
    *,
    invoke_generator: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_target_constants()
    for path in (matrix_path, schedule_path, manifest_path, generator_path):
        if not path.is_file():
            raise SafetyError(f"Required formal input is missing: {path}")
    if invoke_generator:
        run_checked(
            [
                sys.executable,
                str(generator_path),
                "validate",
                str(matrix_path),
                str(schedule_path),
            ],
            cwd=REPOSITORY_ROOT,
        )

    matrix = load_json(matrix_path)
    schedule = load_json(schedule_path)
    manifest = load_json(manifest_path)
    if matrix.get("formatVersion") != 3 or schedule.get("formatVersion") != 3:
        raise SafetyError("Formal matrix and schedule must both use formatVersion 3")
    if matrix.get("repetitions") != 5 or schedule.get("repetitions") != 5:
        raise SafetyError("The formal matrix must contain exactly five blocks")
    variants = matrix.get("variants")
    entries = schedule.get("entries")
    if not isinstance(variants, list) or not isinstance(entries, list):
        raise SafetyError("Matrix variants and schedule entries must be arrays")
    variant_by_id = {
        item.get("id"): item for item in variants if isinstance(item, dict)
    }
    if (
        len(variants) != 6
        or set(variant_by_id) != EXPECTED_VARIANTS
        or len(variant_by_id) != 6
    ):
        raise SafetyError("Matrix variants do not exactly match the six-target map")
    if len(entries) != 80:
        raise SafetyError(
            f"Formal schedule must contain 80 entries, found {len(entries)}"
        )
    if [entry.get("sequence") for entry in entries] != list(range(1, 81)):
        raise SafetyError("Schedule entries are not in exact sequence 1..80")
    if len({entry.get("entryId") for entry in entries}) != 80:
        raise SafetyError("Schedule entry IDs are not unique")
    if len({entry.get("runnerCaseId") for entry in entries}) != 80:
        raise SafetyError("Schedule runner case IDs are not unique")
    block_counts = {
        block: sum(entry.get("block") == block for entry in entries)
        for block in range(1, 6)
    }
    if block_counts != {1: 16, 2: 16, 3: 16, 4: 16, 5: 16}:
        raise SafetyError(f"Schedule block sizes are invalid: {block_counts}")

    benchmark_revision = matrix.get("benchmarkGitRevision")
    if schedule.get("benchmarkGitRevision") != benchmark_revision:
        raise SafetyError("Schedule and matrix benchmark revisions differ")
    if schedule.get("matrixSha256") != file_sha256(matrix_path):
        raise SafetyError("Schedule matrixSha256 does not match matrix bytes")
    if matrix.get("manifestSha256") != file_sha256(manifest_path):
        raise SafetyError("Matrix manifestSha256 does not match manifest bytes")
    if manifest.get("mapIds") != matrix.get("mapIds"):
        raise SafetyError("Manifest and matrix map IDs differ")

    for variant_id, target in TARGETS.items():
        variant = variant_by_id[variant_id]
        expected = {
            "implementation": target.implementation,
            "storageType": "sql",
            "databaseBackend": "postgresql",
            "replicaCount": target.replica_count,
            "contractMode": target.contract_mode,
        }
        for field, value in expected.items():
            if variant.get(field) != value:
                raise SafetyError(
                    f"Matrix variant {variant_id} has unexpected {field}: "
                    f"{variant.get(field)!r}"
                )

    for entry in entries:
        variant_id = entry.get("variantId")
        if variant_id not in TARGETS:
            raise SafetyError(f"Unknown scheduled variant: {variant_id!r}")
        target = TARGETS[variant_id]
        expected = {
            "implementation": target.implementation,
            "storageType": "sql",
            "databaseBackend": "postgresql",
            "replicaCount": target.replica_count,
            "contractMode": target.contract_mode,
            "benchmarkGitRevision": benchmark_revision,
            "mapIds": matrix.get("mapIds"),
        }
        for field, value in expected.items():
            if entry.get(field) != value:
                raise SafetyError(
                    f"Schedule entry {entry.get('entryId')} has unexpected "
                    f"{field}: {entry.get(field)!r}"
                )
    return matrix, schedule


def validate_formal_bundle(
    matrix_path: Path,
    schedule_path: Path,
    admission_identities_path: Path,
    bundle_manifest_path: Path,
    benchmark_revision: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    expected_names = {
        matrix_path: "matrix.json",
        schedule_path: "schedule.json",
        admission_identities_path: "runtime-admission-identities.json",
        bundle_manifest_path: "bundle-manifest.json",
    }
    bundle_directories = {path.resolve().parent for path in expected_names}
    if len(bundle_directories) != 1:
        raise SafetyError(
            "Matrix, schedule, admission identities, and bundle manifest "
            "must come from one exact frozen directory"
        )
    for path, expected_name in expected_names.items():
        if path.name != expected_name or not path.is_file():
            raise SafetyError(
                f"Frozen bundle input must be an existing {expected_name}: {path}"
            )

    admission = load_json(admission_identities_path)
    bundle = load_json(bundle_manifest_path)
    if (
        admission.get("formatVersion") != 1
        or admission.get("benchmarkGitRevision") != benchmark_revision
        or admission.get("podSpecIdentityVersion")
        != ADMISSION_POD_SPEC_IDENTITY_VERSION
    ):
        raise SafetyError(
            "Frozen admission identities use the wrong format or revision"
        )
    variants = admission.get("variants")
    if not isinstance(variants, list) or len(variants) != 6:
        raise SafetyError(
            "Frozen admission identities must contain exactly six variants"
        )
    expected_admission: dict[str, str] = {}
    for item in variants:
        if not isinstance(item, dict):
            raise SafetyError("Frozen admission identity contains a non-object")
        variant_id = item.get("variantId")
        if (
            variant_id not in TARGETS
            or variant_id in expected_admission
            or item.get("replicaCount") != TARGETS[variant_id].replica_count
        ):
            raise SafetyError(
                f"Invalid frozen admission identity variant: {variant_id!r}"
            )
        digest = item.get("expectedAdmissionPodSpecSha256")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or set(digest) == {"0"}
        ):
            raise SafetyError(
                f"Frozen admission identity {variant_id} has an invalid digest"
            )
        expected_admission[variant_id] = digest
    if list(expected_admission) != list(TARGETS):
        raise SafetyError(
            "Frozen admission identity variant ordering differs from the target map"
        )

    controller_lock_sha256 = validate_controller_lock(benchmark_revision)
    expected_bundle_values = {
        "formatVersion": 1,
        "benchmarkGitRevision": benchmark_revision,
        "matrixSha256": file_sha256(matrix_path),
        "scheduleSha256": file_sha256(schedule_path),
        "runtimeAdmissionIdentitiesSha256": file_sha256(admission_identities_path),
        "controllerLockSha256": controller_lock_sha256,
        "freezerSha256": file_sha256(FREEZER),
        "orchestratorSha256": file_sha256(Path(__file__)),
        "analyzerSha256": file_sha256(ANALYZER),
    }
    for field, value in expected_bundle_values.items():
        if bundle.get(field) != value:
            raise SafetyError(
                f"Frozen bundle {field} does not match the exact formal inputs"
            )
    return expected_admission, bundle


def validate_controller_lock(benchmark_revision: str) -> str:
    """Require the reviewed lock to close over every tracked controller."""
    lock = load_json(CONTROL_LOCK)
    if (
        set(lock) != {"formatVersion", "requiredRevision", "controllers"}
        or lock.get("formatVersion") != 1
        or lock.get("requiredRevision") != benchmark_revision
    ):
        raise SafetyError("Reviewed controller lock has the wrong shape or revision")
    expected = [
        ("freeze.py", FREEZER),
        ("orchestrate.py", Path(__file__).resolve()),
        ("analyze.py", ANALYZER),
    ]
    controllers = lock.get("controllers")
    if not isinstance(controllers, list) or len(controllers) != len(expected):
        raise SafetyError("Reviewed controller lock must bind every controller")
    for item, (expected_name, path) in zip(controllers, expected, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or item.get("path") != expected_name
        ):
            raise SafetyError("Reviewed controller lock has invalid controller order")
        digest = item.get("sha256")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or set(digest) == {"0"}
            or digest != file_sha256(path)
        ):
            raise SafetyError(f"Reviewed controller {expected_name} hash differs")
    return file_sha256(CONTROL_LOCK)


def validate_repository_freeze(revision: str, runner: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        raise SafetyError("Matrix benchmarkGitRevision is not an exact commit")
    if not runner.is_file():
        raise SafetyError(f"Runner is missing: {runner}")
    head = run_checked(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=REPOSITORY_ROOT,
    ).stdout.strip()
    if head != revision:
        raise SafetyError(
            f"Repository HEAD {head} does not match frozen revision {revision}"
        )
    tracked_status = run_checked(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=REPOSITORY_ROOT,
    ).stdout
    if tracked_status:
        raise SafetyError("Formal execution requires a clean tracked worktree")
    relative_runner = runner.resolve().relative_to(REPOSITORY_ROOT.resolve())
    run_checked(
        ["git", "cat-file", "-e", f"{revision}:{relative_runner.as_posix()}"],
        cwd=REPOSITORY_ROOT,
    )


def scale_command(
    deployment: str,
    replicas: int,
    *,
    kubeconfig: Path,
) -> list[str]:
    if deployment not in FORMAL_DEPLOYMENTS:
        raise SafetyError(
            f"Refusing to scale non-allowlisted Deployment {deployment!r}"
        )
    if replicas not in {0, 1, 3}:
        raise SafetyError(f"Refusing unsupported replica count {replicas}")
    resource = f"deployment/{deployment}"
    if resource in PROTECTED_RESOURCES:
        raise SafetyError(f"Refusing protected resource {resource}")
    return [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "--namespace",
        NAMESPACE,
        "scale",
        resource,
        f"--replicas={replicas}",
    ]


class Kubectl:
    def __init__(self, kubeconfig: Path) -> None:
        if not kubeconfig.is_file():
            raise SafetyError(f"Kubeconfig is missing: {kubeconfig}")
        self.kubeconfig = kubeconfig
        self.base = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            NAMESPACE,
        ]

    def json(self, arguments: Sequence[str]) -> dict[str, Any]:
        result = run_checked([*self.base, *arguments])
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SafetyError(f"kubectl returned invalid JSON: {error}") from error
        if not isinstance(value, dict):
            raise SafetyError("kubectl JSON response is not an object")
        return value

    def scale(self, deployment: str, replicas: int) -> None:
        run_checked(
            scale_command(
                deployment,
                replicas,
                kubeconfig=self.kubeconfig,
            )
        )

    def deployment(self, name: str) -> dict[str, Any]:
        if name not in FORMAL_DEPLOYMENTS:
            raise SafetyError(f"Unknown formal Deployment {name!r}")
        return self.json(["get", f"deployment/{name}", "-o", "json"])

    def pod(self, name: str) -> dict[str, Any]:
        require_benchmark_name(name, "Pod")
        return self.json(["get", f"pod/{name}", "-o", "json"])

    def replicaset(self, name: str) -> dict[str, Any]:
        require_benchmark_name(name, "ReplicaSet")
        return self.json(["get", f"replicaset/{name}", "-o", "json"])

    def service(self, name: str) -> dict[str, Any]:
        require_benchmark_name(name, "Service")
        return self.json(["get", f"service/{name}", "-o", "json"])

    def configmap(self, name: str) -> dict[str, Any]:
        require_benchmark_name(name, "ConfigMap")
        return self.json(["get", f"configmap/{name}", "-o", "json"])

    def pods(self, selector: str | None = None) -> dict[str, Any]:
        arguments = ["get", "pods"]
        if selector:
            arguments.extend(["--selector", selector])
        arguments.extend(["-o", "json"])
        return self.json(arguments)

    def endpoint_slices(self, service: str) -> dict[str, Any]:
        require_benchmark_name(service, "Service")
        return self.json(
            [
                "get",
                "endpointslice",
                "--selector",
                f"kubernetes.io/service-name={service}",
                "-o",
                "json",
            ]
        )

    def metrics(self, pod: str) -> dict[str, Any]:
        require_benchmark_name(pod, "metrics Pod")
        path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{NAMESPACE}/pods/{pod}"
        return self.json(["get", "--raw", path])


def require_benchmark_name(name: str, label: str) -> None:
    if (
        not isinstance(name, str)
        or not name.startswith("bluemap-perf-")
        or RESOURCE_NAME.fullmatch(name) is None
    ):
        raise SafetyError(f"Unsafe {label} name: {name!r}")


def metadata(resource: dict[str, Any], kind: str, name: str) -> dict[str, Any]:
    if resource.get("kind") != kind:
        raise SafetyError(f"Expected {kind}/{name}, got {resource.get('kind')!r}")
    value = resource.get("metadata")
    if not isinstance(value, dict):
        raise SafetyError(f"{kind}/{name} has no metadata")
    if value.get("name") != name or value.get("namespace") != NAMESPACE:
        raise SafetyError(f"{kind}/{name} identity or namespace mismatch")
    return value


def ready(resource: dict[str, Any]) -> bool:
    status = resource.get("status")
    if not isinstance(status, dict) or status.get("phase") != "Running":
        return False
    conditions = status.get("conditions")
    return isinstance(conditions, list) and any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    )


def validate_candidate_deployment(
    resource: dict[str, Any],
    target: VariantTarget,
) -> None:
    value = metadata(resource, "Deployment", target.deployment)
    labels = value.get("labels")
    if not isinstance(labels, dict):
        raise SafetyError(f"Deployment/{target.deployment} has no labels")
    if labels.get("app.kubernetes.io/part-of") != "bluemap-web-performance":
        raise SafetyError(f"Deployment/{target.deployment} is not a benchmark resource")
    if labels.get("app.kubernetes.io/instance") != target.release:
        raise SafetyError(
            f"Deployment/{target.deployment} does not belong to the expected release"
        )
    if labels.get("bluemap.guenter.cloud/experiment-id") != target.experiment_id:
        raise SafetyError(
            f"Deployment/{target.deployment} experiment label does not match"
        )


def deployment_converged(resource: dict[str, Any], replicas: int) -> bool:
    spec = resource.get("spec")
    status = resource.get("status")
    metadata_value = resource.get("metadata")
    if not all(isinstance(value, dict) for value in (spec, status, metadata_value)):
        return False
    generation = metadata_value.get("generation")
    if not isinstance(generation, int):
        return False
    return (
        spec.get("replicas", 0) == replicas
        and status.get("observedGeneration", 0) == generation
        and status.get("replicas", 0) == replicas
        and status.get("updatedReplicas", 0) == replicas
        and status.get("readyReplicas", 0) == replicas
        and status.get("availableReplicas", 0) == replicas
        and status.get("unavailableReplicas", 0) == 0
    )


def deployment_selector(resource: dict[str, Any]) -> str:
    selector = resource.get("spec", {}).get("selector")
    if not isinstance(selector, dict):
        raise SafetyError("Deployment has no selector")
    if selector.get("matchExpressions"):
        raise SafetyError("Formal Deployments must use matchLabels-only selectors")
    labels = selector.get("matchLabels")
    if not isinstance(labels, dict) or not labels:
        raise SafetyError("Deployment selector has no matchLabels")
    parts = []
    for key, value in sorted(labels.items()):
        if not isinstance(key, str) or not isinstance(value, str):
            raise SafetyError("Deployment selector contains non-string labels")
        parts.append(f"{key}={value}")
    return ",".join(parts)


def active_pods(kube: Kubectl, deployment: dict[str, Any]) -> list[dict[str, Any]]:
    payload = kube.pods(deployment_selector(deployment))
    items = payload.get("items")
    if not isinstance(items, list):
        raise SafetyError("Pod List has no items array")
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("metadata", {}).get("deletionTimestamp") is None
    ]


def controller_reference(
    resource_metadata: dict[str, Any],
    *,
    kind: str,
    label: str,
) -> dict[str, Any]:
    owner_references = resource_metadata.get("ownerReferences")
    if not isinstance(owner_references, list):
        raise SafetyError(f"{label} has no ownerReferences")
    controllers = [
        item
        for item in owner_references
        if isinstance(item, dict) and item.get("controller") is True
    ]
    if len(controllers) != 1:
        raise SafetyError(f"{label} must have exactly one controller")
    controller = controllers[0]
    if (
        controller.get("apiVersion") != "apps/v1"
        or controller.get("kind") != kind
        or not isinstance(controller.get("name"), str)
        or not isinstance(controller.get("uid"), str)
    ):
        raise SafetyError(f"{label} has the wrong controller kind or identity")
    return controller


def validate_current_pod_ownership(
    kube: Kubectl,
    pod: dict[str, Any],
    deployment: dict[str, Any],
    target: VariantTarget,
) -> None:
    pod_metadata = metadata(
        pod,
        "Pod",
        str(pod.get("metadata", {}).get("name", "")),
    )
    pod_labels = pod_metadata.get("labels")
    if not isinstance(pod_labels, dict):
        raise SafetyError("Selected Pod has no labels")
    if pod_labels.get("bluemap.guenter.cloud/experiment-id") != target.experiment_id:
        raise SafetyError(f"Pod/{pod_metadata['name']} experiment label differs")
    pod_controller = controller_reference(
        pod_metadata,
        kind="ReplicaSet",
        label=f"Pod/{pod_metadata['name']}",
    )
    replicaset = kube.replicaset(pod_controller["name"])
    rs_metadata = metadata(replicaset, "ReplicaSet", pod_controller["name"])
    if rs_metadata.get("uid") != pod_controller["uid"]:
        raise SafetyError("Pod controller UID does not match ReplicaSet UID")
    rs_controller = controller_reference(
        rs_metadata,
        kind="Deployment",
        label=f"ReplicaSet/{pod_controller['name']}",
    )
    deployment_metadata = metadata(deployment, "Deployment", target.deployment)
    if rs_controller.get("name") != target.deployment or rs_controller.get(
        "uid"
    ) != deployment_metadata.get("uid"):
        raise SafetyError("ReplicaSet is not owned by the exact Deployment UID")
    deployment_revision = deployment_metadata.get("annotations", {}).get(
        "deployment.kubernetes.io/revision"
    )
    replicaset_revision = rs_metadata.get("annotations", {}).get(
        "deployment.kubernetes.io/revision"
    )
    if (
        not isinstance(deployment_revision, str)
        or not re.fullmatch(r"[1-9][0-9]*", deployment_revision)
        or replicaset_revision != deployment_revision
    ):
        raise SafetyError("Pod is not owned by the current Deployment revision")


def ready_endpoint_pods(payload: dict[str, Any]) -> list[str]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise SafetyError("EndpointSlice List has no items array")
    names: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for endpoint in item.get("endpoints", []):
            if not isinstance(endpoint, dict):
                continue
            conditions = endpoint.get("conditions", {})
            if (
                conditions.get("ready") is True
                and conditions.get("serving", True) is True
                and conditions.get("terminating", False) is False
            ):
                target_ref = endpoint.get("targetRef", {})
                if target_ref.get("kind") != "Pod" or not isinstance(
                    target_ref.get("name"), str
                ):
                    raise SafetyError("Ready endpoint does not reference a Pod")
                names.add(target_ref["name"])
    return sorted(names)


def wait_until(
    description: str,
    timeout_seconds: int,
    interval_seconds: float,
    predicate: Any,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (SafetyError, subprocess.SubprocessError) as error:
            last_error = error
        time.sleep(interval_seconds)
    detail = f": {last_error}" if last_error else ""
    raise SafetyError(f"Timed out waiting for {description}{detail}")


def validate_static_target_resources(kube: Kubectl, target: VariantTarget) -> None:
    service = kube.service(target.service)
    metadata(service, "Service", target.service)
    traffic_service = kube.service(TRAFFIC_SERVICE)
    metadata(traffic_service, "Service", TRAFFIC_SERVICE)
    for configmap in target.configmaps:
        resource = kube.configmap(configmap)
        metadata(resource, "ConfigMap", configmap)


def validate_infrastructure_pod(kube: Kubectl, name: str) -> None:
    resource = kube.pod(name)
    metadata(resource, "Pod", name)
    if not ready(resource):
        raise SafetyError(f"Infrastructure Pod/{name} is not Running and Ready")


def wait_all_zero(
    kube: Kubectl,
    deployments: dict[str, dict[str, Any]],
    *,
    timeout_seconds: int,
    interval_seconds: float,
) -> None:
    for deployment_name, initial in deployments.items():
        selector = deployment_selector(initial)

        def zero(
            deployment_name: str = deployment_name,
            selector: str = selector,
        ) -> bool:
            current = kube.deployment(deployment_name)
            if not deployment_converged(current, 0):
                return False
            pods = kube.pods(selector).get("items", [])
            return isinstance(pods, list) and len(pods) == 0

        wait_until(
            f"Deployment/{deployment_name} to reach zero with no Pods",
            timeout_seconds,
            interval_seconds,
            zero,
        )


def audit_no_other_web_candidates(
    kube: Kubectl,
    expected_pods: set[str],
) -> None:
    payload = kube.pods()
    items = payload.get("items")
    if not isinstance(items, list):
        raise SafetyError("Namespace Pod List has no items")
    active: set[str] = set()
    for pod in items:
        if not isinstance(pod, dict):
            continue
        metadata_value = pod.get("metadata", {})
        name = metadata_value.get("name")
        labels = metadata_value.get("labels", {})
        phase = pod.get("status", {}).get("phase")
        if (
            isinstance(name, str)
            and name.startswith("bluemap-perf-")
            and labels.get("app.kubernetes.io/component") in {"web", "sql-data"}
            and phase not in {"Succeeded", "Failed"}
        ):
            active.add(name)
    if active != expected_pods:
        raise SafetyError(
            "Active benchmark web-tier Pods differ from the selected candidate: "
            f"expected={sorted(expected_pods)}, actual={sorted(active)}"
        )


def wait_metrics(
    kube: Kubectl,
    pods: Sequence[str],
    *,
    timeout_seconds: int,
    interval_seconds: float,
) -> None:
    for pod in pods:

        def available(pod: str = pod) -> bool:
            value = kube.metrics(pod)
            containers = value.get("containers")
            return (
                value.get("metadata", {}).get("name") == pod
                and isinstance(value.get("timestamp"), str)
                and isinstance(containers, list)
                and len(containers) > 0
            )

        wait_until(
            f"a fresh metrics.k8s.io sample for Pod/{pod}",
            timeout_seconds,
            interval_seconds,
            available,
        )


def activate_target(
    kube: Kubectl,
    target: VariantTarget,
    replicas: int,
    *,
    transition_timeout_seconds: int,
    metrics_timeout_seconds: int,
    poll_interval_seconds: float,
) -> list[str]:
    if replicas != target.replica_count:
        raise SafetyError("Schedule replica count differs from the target allowlist")

    deployments: dict[str, dict[str, Any]] = {}
    for variant in TARGETS.values():
        resource = kube.deployment(variant.deployment)
        validate_candidate_deployment(resource, variant)
        deployments[variant.deployment] = resource
    validate_static_target_resources(kube, target)
    validate_infrastructure_pod(kube, DATABASE_POD)

    for deployment in FORMAL_DEPLOYMENTS:
        kube.scale(deployment, 0)
    wait_all_zero(
        kube,
        deployments,
        timeout_seconds=transition_timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )

    kube.scale(target.deployment, replicas)

    def converged() -> dict[str, Any] | None:
        resource = kube.deployment(target.deployment)
        validate_candidate_deployment(resource, target)
        return resource if deployment_converged(resource, replicas) else None

    deployment = wait_until(
        f"Deployment/{target.deployment} to converge at {replicas}",
        transition_timeout_seconds,
        poll_interval_seconds,
        converged,
    )
    pods = active_pods(kube, deployment)
    ready_pods = sorted(
        (pod for pod in pods if ready(pod)),
        key=lambda item: item.get("metadata", {}).get("name", ""),
    )
    if len(ready_pods) != replicas or len(pods) != replicas:
        raise SafetyError(
            f"Deployment/{target.deployment} has {len(pods)} current Pods and "
            f"{len(ready_pods)} Ready Pods; expected {replicas}"
        )
    names: list[str] = []
    for pod in ready_pods:
        name = pod.get("metadata", {}).get("name")
        require_benchmark_name(name, "Pod")
        validate_current_pod_ownership(kube, pod, deployment, target)
        names.append(name)

    expected_names = sorted(names)

    def endpoints_match() -> bool:
        origin = ready_endpoint_pods(kube.endpoint_slices(target.service))
        traffic = ready_endpoint_pods(kube.endpoint_slices(TRAFFIC_SERVICE))
        return origin == expected_names and traffic == expected_names

    wait_until(
        (
            f"Services/{target.service},{TRAFFIC_SERVICE} EndpointSlices "
            "to contain exact target Pods"
        ),
        transition_timeout_seconds,
        poll_interval_seconds,
        endpoints_match,
    )
    audit_no_other_web_candidates(kube, set(expected_names))
    wait_metrics(
        kube,
        [DATABASE_POD, *expected_names],
        timeout_seconds=metrics_timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    return expected_names


def verify_admission_pod_spec_identity(
    kube: Kubectl,
    target: VariantTarget,
    web_pods: Sequence[str],
    expected_sha256: str,
) -> dict[str, str]:
    if len(web_pods) != target.replica_count:
        raise SafetyError(
            f"Admission identity Pod count differs for {target.variant_id}"
        )
    deployment = kube.deployment(target.deployment)
    validate_candidate_deployment(deployment, target)
    if not deployment_converged(deployment, target.replica_count):
        raise SafetyError(
            f"Deployment/{target.deployment} changed before admission check"
        )
    actual: dict[str, str] = {}
    for pod_name in sorted(web_pods):
        pod = kube.pod(pod_name)
        metadata(pod, "Pod", pod_name)
        if not ready(pod):
            raise SafetyError(
                f"Pod/{pod_name} is not Ready during admission identity check"
            )
        validate_current_pod_ownership(kube, pod, deployment, target)
        actual[pod_name] = admitted_pod_spec_sha256(pod)
    if set(actual.values()) != {expected_sha256}:
        raise SafetyError(
            f"Normalized admission-mutated Pod execution spec for "
            f"{target.variant_id} differs from the frozen identity: "
            f"expected={expected_sha256}, actual={actual}"
        )
    if ready_endpoint_pods(kube.endpoint_slices(target.service)) != sorted(actual):
        raise SafetyError(
            f"Service/{target.service} endpoints changed during admission check"
        )
    if ready_endpoint_pods(kube.endpoint_slices(TRAFFIC_SERVICE)) != sorted(actual):
        raise SafetyError(
            f"Service/{TRAFFIC_SERVICE} endpoints changed during admission check"
        )
    audit_no_other_web_candidates(kube, set(actual))
    return actual


@dataclass(frozen=True)
class RunnerOptions:
    runner: Path
    matrix: Path
    schedule: Path
    manifest: Path
    artifact_root: Path
    benchmark_python: Path
    kubeconfig: Path
    prometheus_url: str | None
    load_generator_identity: Path
    load_generator_identity_key: Path
    traffic_base_url: str
    traffic_service: str
    traffic_service_port: int
    formal_run_id: str
    require_edge_bypass: bool


def build_runner_command(
    entry: dict[str, Any],
    target: VariantTarget,
    web_pods: Sequence[str],
    options: RunnerOptions,
) -> list[str]:
    if entry.get("variantId") != target.variant_id:
        raise SafetyError("Entry variant and target differ")
    if len(web_pods) != entry.get("replicaCount"):
        raise SafetyError("Runner Pod count does not match the schedule")
    command = [
        "bash",
        str(options.runner),
        "--case-id",
        str(entry["runnerCaseId"]),
        "--matrix",
        str(options.matrix),
        "--schedule",
        str(options.schedule),
        "--schedule-entry",
        str(entry["entryId"]),
        "--variant-id",
        target.variant_id,
        "--implementation",
        str(entry["implementation"]),
        "--storage-type",
        str(entry["storageType"]),
        "--database-backend",
        str(entry["databaseBackend"]),
        "--service",
        target.service,
        "--service-port",
        str(target.port),
        "--origin-base-url",
        f"http://{target.service}.{NAMESPACE}.svc.cluster.local:{target.port}",
        "--manifest",
        str(options.manifest),
        "--web-deployment",
        target.deployment,
        "--database-pod",
        DATABASE_POD,
        "--profile",
        str(entry["profile"]),
        "--rate",
        str(entry["rate"]),
        "--viewers",
        str(entry["viewers"]),
        "--marker-interval-seconds",
        str(entry["markerIntervalSeconds"]),
        "--min-achieved-rate-ratio",
        str(entry["minimumAchievedRateRatio"]),
        "--trace-seed",
        str(entry["traceSeed"]),
        "--latency-p95-ms",
        str(entry["latencyP95Milliseconds"]),
        "--latency-p99-ms",
        str(entry["latencyP99Milliseconds"]),
        "--pre-allocated-vus",
        str(entry["preAllocatedVUs"]),
        "--max-vus",
        str(entry["maxVUs"]),
        "--accept-encoding",
        str(entry["acceptEncoding"]),
        "--stored-encoding",
        str(entry["storedEncoding"]),
        "--contract-mode",
        str(entry["contractMode"]),
        "--warmup",
        str(entry["warmupDuration"]),
        "--measurement",
        str(entry["measurementDuration"]),
        "--cooldown-seconds",
        str(entry["cooldownSeconds"]),
        "--repetitions",
        "1",
        "--metrics-interval-seconds",
        "5",
        "--prometheus-step-seconds",
        "15",
        "--max-non-target-node-cpu-range-cores",
        "0.5",
        "--max-non-target-node-cpu-mean-cores",
        "3.0",
        "--max-non-target-node-cpu-maximum-cores",
        "4.0",
        "--artifact-root",
        str(options.artifact_root),
        "--python",
        str(options.benchmark_python),
        "--kubeconfig",
        str(options.kubeconfig),
        "--namespace",
        NAMESPACE,
        "--load-generator-backend",
        "runpod-ssh",
        "--load-generator-identity",
        str(options.load_generator_identity),
        "--load-generator-identity-key",
        str(options.load_generator_identity_key),
        "--traffic-base-url",
        options.traffic_base_url,
        "--traffic-service",
        options.traffic_service,
        "--traffic-service-port",
        str(options.traffic_service_port),
        "--formal-run-id",
        options.formal_run_id,
    ]
    if options.require_edge_bypass:
        command.append("--require-edge-bypass")
    if options.prometheus_url:
        command.extend(["--prometheus-url", options.prometheus_url])
    for map_id in entry["mapIds"]:
        command.extend(["--map-id", str(map_id)])
    for configmap in target.configmaps:
        command.extend(["--configmap", configmap])
    for pod in web_pods:
        require_benchmark_name(pod, "Pod")
        command.extend(["--web-pod", pod])
    if any(protected in command for protected in PROTECTED_RESOURCES):
        raise SafetyError("Runner command unexpectedly references a protected resource")
    return command


def plan_entries(
    schedule: dict[str, Any],
    options: RunnerOptions,
) -> list[dict[str, Any]]:
    plans = []
    for entry in schedule["entries"]:
        target = TARGETS[entry["variantId"]]
        placeholder_pods = [
            f"{target.deployment}-resolved-pod-{index}"
            for index in range(1, target.replica_count + 1)
        ]
        command = build_runner_command(entry, target, placeholder_pods, options)
        plans.append(
            {
                "sequence": entry["sequence"],
                "block": entry["block"],
                "entryId": entry["entryId"],
                "runnerCaseId": entry["runnerCaseId"],
                "variantId": entry["variantId"],
                "release": target.release,
                "scaleDownExactly": list(FORMAL_DEPLOYMENTS),
                "scaleUpExactly": {
                    "deployment": target.deployment,
                    "replicas": target.replica_count,
                },
                "service": {"name": target.service, "port": target.port},
                "configMaps": list(target.configmaps),
                "webPods": "resolved-and-validated-at-runtime",
                "runnerCommandTemplate": command,
            }
        )
    return plans


def initial_state(
    matrix_path: Path,
    schedule_path: Path,
    manifest_path: Path,
    admission_identities_path: Path,
    bundle_manifest_path: Path,
    revision: str,
    execution_identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "formatVersion": 1,
        "createdAt": timestamp(),
        "updatedAt": timestamp(),
        "matrixSha256": file_sha256(matrix_path),
        "scheduleSha256": file_sha256(schedule_path),
        "manifestSha256": file_sha256(manifest_path),
        "runtimeAdmissionIdentitiesSha256": file_sha256(admission_identities_path),
        "bundleManifestSha256": file_sha256(bundle_manifest_path),
        "orchestratorSha256": file_sha256(Path(__file__)),
        "analyzerSha256": file_sha256(ANALYZER),
        "benchmarkGitRevision": revision,
        "executionIdentity": execution_identity,
        "nextSequence": 1,
        "status": "running",
        "entries": {},
    }


def validate_resume_state(
    state: dict[str, Any],
    *,
    matrix_path: Path,
    schedule_path: Path,
    manifest_path: Path,
    admission_identities_path: Path,
    bundle_manifest_path: Path,
    revision: str,
    execution_identity: dict[str, Any],
) -> None:
    expected = {
        "matrixSha256": file_sha256(matrix_path),
        "scheduleSha256": file_sha256(schedule_path),
        "manifestSha256": file_sha256(manifest_path),
        "runtimeAdmissionIdentitiesSha256": file_sha256(admission_identities_path),
        "bundleManifestSha256": file_sha256(bundle_manifest_path),
        "orchestratorSha256": file_sha256(Path(__file__)),
        "analyzerSha256": file_sha256(ANALYZER),
        "benchmarkGitRevision": revision,
        "executionIdentity": execution_identity,
    }
    for field, value in expected.items():
        if state.get(field) != value:
            raise SafetyError(f"Run state {field} no longer matches frozen inputs")
    next_sequence = state.get("nextSequence")
    if not isinstance(next_sequence, int) or not 1 <= next_sequence <= 81:
        raise SafetyError("Run state has an invalid nextSequence")
    entries = state.get("entries")
    if not isinstance(entries, dict):
        raise SafetyError("Run state entries are invalid")
    for sequence in range(1, next_sequence):
        item = entries.get(str(sequence))
        if not isinstance(item, dict) or item.get("status") != "completed":
            raise SafetyError("Run state has a gap before nextSequence")
    current = entries.get(str(next_sequence))
    if isinstance(current, dict) and current.get("status") in {
        "runner-started",
        "fatal",
    }:
        raise SafetyError(
            "The current entry may have started measurement and cannot be retried"
        )


def execution_identity(args: argparse.Namespace) -> dict[str, Any]:
    """Bind resume-sensitive local controls without copying credential contents."""
    load_generator_identity = validate_runpod_controls(args)
    return {
        "formatVersion": 1,
        "namespace": NAMESPACE,
        "databasePod": DATABASE_POD,
        "loadGeneratorBackend": "runpod-ssh",
        "loadGeneratorIdentity": load_generator_identity,
        "loadGeneratorIdentitySha256": hashlib.sha256(
            canonical_json(load_generator_identity)
        ).hexdigest(),
        "formalRunId": args.formal_run_id,
        "traffic": {
            "baseUrl": args.traffic_base_url,
            "service": args.traffic_service,
            "port": args.traffic_service_port,
            "requiresEdgeBypass": args.require_edge_bypass,
        },
        "runner": str(args.runner.resolve()),
        "runnerSha256": file_sha256(args.runner.resolve()),
        "benchmarkPython": str(args.benchmark_python.resolve()),
        "benchmarkPythonSha256": file_sha256(args.benchmark_python.resolve()),
        "kubeconfig": str(args.kubeconfig.resolve()),
        "kubeconfigSha256": file_sha256(args.kubeconfig.resolve()),
        "prometheus": {
            "enabled": not args.no_prometheus,
            "url": None if args.no_prometheus else args.prometheus_url,
        },
        "transitionTimeoutSeconds": args.transition_timeout_seconds,
        "metricsTimeoutSeconds": args.metrics_timeout_seconds,
        "pollIntervalSeconds": args.poll_interval_seconds,
    }


def append_event(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, sort_keys=True) + "\n")


def run_streamed(command: Sequence[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("x", encoding="utf-8") as log:
        process = subprocess.Popen(
            list(command),
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        assert process.stdout is not None
        try:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
                log.flush()
        except KeyboardInterrupt:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            raise
        return process.wait()


def runner_completed_cooldown(case_dir: Path, required_seconds: int) -> bool:
    path = case_dir / "phases.ndjson"
    if not path.is_file():
        return False
    start: datetime | None = None
    end: datetime | None = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if (
                event.get("repetition") != 1
                or event.get("phase") != "cooldown"
                or event.get("event") not in {"start", "end"}
            ):
                continue
            parsed = datetime.fromisoformat(
                str(event.get("timestamp", "")).replace("Z", "+00:00")
            )
            if event["event"] == "start":
                if start is not None or end is not None:
                    return False
                start = parsed
            else:
                if start is None or end is not None:
                    return False
                end = parsed
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return (
        start is not None
        and end is not None
        and end >= start
        and (end - start).total_seconds() >= required_seconds
    )


def ensure_inter_entry_cooldown(
    case_dir: Path,
    required_seconds: int,
) -> dict[str, Any]:
    runner_satisfied = runner_completed_cooldown(case_dir, required_seconds)
    wait_started_at = timestamp()
    wait_started = time.monotonic()
    if not runner_satisfied:
        time.sleep(required_seconds)
    waited_seconds = time.monotonic() - wait_started
    return {
        "requiredSeconds": required_seconds,
        "runnerSatisfied": runner_satisfied,
        "orchestratorWaitedSeconds": waited_seconds,
        "waitStartedAt": wait_started_at,
        "completedAt": timestamp(),
    }


def validate_benchmark_python(path: Path) -> None:
    if not path.is_file():
        raise SafetyError(f"Benchmark Python is missing: {path}")
    run_checked([str(path), "-c", "import zstandard"])


def quiesce_all(
    kube: Kubectl,
    *,
    timeout_seconds: int,
    interval_seconds: float,
) -> None:
    failures: list[str] = []
    scaled: dict[str, dict[str, Any]] = {}
    for target in TARGETS.values():
        try:
            resource = kube.deployment(target.deployment)
            validate_candidate_deployment(resource, target)
            kube.scale(target.deployment, 0)
            scaled[target.deployment] = resource
        except (SafetyError, OSError) as error:
            failures.append(f"Deployment/{target.deployment}: {error}")
    for deployment, resource in scaled.items():
        try:
            wait_all_zero(
                kube,
                {deployment: resource},
                timeout_seconds=timeout_seconds,
                interval_seconds=interval_seconds,
            )
        except (SafetyError, OSError) as error:
            failures.append(f"Deployment/{deployment} convergence: {error}")
    if failures:
        raise SafetyError(
            "One or more exact candidate quiesce operations failed; "
            "all other candidates were still attempted:\n" + "\n".join(failures)
        )


def acquire_global_lock(run_root: Path) -> Any:
    lock_path = run_root.parent / ".active-formal-run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise SafetyError(f"Another formal orchestrator holds {lock_path}") from error
    lock.seek(0)
    lock.truncate()
    lock.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "runRoot": str(run_root),
                "acquiredAt": timestamp(),
            },
            sort_keys=True,
        )
        + "\n"
    )
    lock.flush()
    return lock


def validate_run_root(path: Path) -> Path:
    run_root = path.resolve()
    ignored_artifact_root = (BENCHMARK_ROOT / "artifacts").resolve()
    try:
        run_root.relative_to(ignored_artifact_root)
    except ValueError as error:
        raise SafetyError(
            "Run root must be below the ignored artifact directory "
            f"{ignored_artifact_root}"
        ) from error
    if run_root == ignored_artifact_root:
        raise SafetyError("Run root must be a dedicated child artifact directory")
    return run_root


def execute_schedule(
    matrix: dict[str, Any],
    schedule: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    if args.confirm != CONFIRMATION:
        raise SafetyError(
            f"--confirm must exactly equal {CONFIRMATION!r} for cluster mutation"
        )
    validate_repository_freeze(matrix["benchmarkGitRevision"], args.runner)
    validate_benchmark_python(args.benchmark_python)
    run_execution_identity = execution_identity(args)
    kube = Kubectl(args.kubeconfig)
    run_root = validate_run_root(args.run_root)
    run_lock = acquire_global_lock(run_root)
    state_path = run_root / "state.json"
    events_path = run_root / "events.ndjson"
    result_root = run_root / "results"
    logs_root = run_root / "logs"

    try:
        if state_path.exists():
            if not args.resume:
                raise SafetyError(
                    "Run state already exists; use --resume only after "
                    f"inspection: {state_path}"
                )
            state = load_json(state_path)
            validate_resume_state(
                state,
                matrix_path=args.matrix,
                schedule_path=args.schedule,
                manifest_path=args.manifest,
                admission_identities_path=args.runtime_admission_identities,
                bundle_manifest_path=args.bundle_manifest,
                revision=matrix["benchmarkGitRevision"],
                execution_identity=run_execution_identity,
            )
        else:
            if args.resume:
                raise SafetyError("--resume was requested but no run state exists")
            if run_root.exists() and any(run_root.iterdir()):
                raise SafetyError(f"New run root is not empty: {run_root}")
            run_root.mkdir(parents=True, exist_ok=True)
            result_root.mkdir()
            logs_root.mkdir()
            state = initial_state(
                args.matrix,
                args.schedule,
                args.manifest,
                args.runtime_admission_identities,
                args.bundle_manifest,
                matrix["benchmarkGitRevision"],
                run_execution_identity,
            )
            atomic_write_json(state_path, state)

        options = RunnerOptions(
            runner=args.runner,
            matrix=args.matrix,
            schedule=args.schedule,
            manifest=args.manifest,
            artifact_root=result_root,
            benchmark_python=args.benchmark_python,
            kubeconfig=args.kubeconfig,
            prometheus_url=None if args.no_prometheus else args.prometheus_url,
            load_generator_identity=args.load_generator_identity,
            load_generator_identity_key=args.load_generator_identity_key,
            traffic_base_url=args.traffic_base_url,
            traffic_service=args.traffic_service,
            traffic_service_port=args.traffic_service_port,
            formal_run_id=args.formal_run_id,
            require_edge_bypass=args.require_edge_bypass,
        )
        mutated = False
        try:
            for entry in schedule["entries"]:
                sequence = entry["sequence"]
                if sequence < state["nextSequence"]:
                    continue
                if sequence != state["nextSequence"]:
                    raise SafetyError(
                        "State cursor and strict schedule sequence differ"
                    )
                target = TARGETS[entry["variantId"]]
                case_dir = result_root / entry["runnerCaseId"]
                if case_dir.exists():
                    raise SafetyError(f"Case artifact already exists: {case_dir}")

                state["entries"][str(sequence)] = {
                    "status": "activating",
                    "entryId": entry["entryId"],
                    "runnerCaseId": entry["runnerCaseId"],
                    "variantId": entry["variantId"],
                    "startedAt": timestamp(),
                }
                state["updatedAt"] = timestamp()
                atomic_write_json(state_path, state)
                append_event(
                    events_path,
                    {
                        "timestamp": timestamp(),
                        "sequence": sequence,
                        "event": "activation-start",
                        "entryId": entry["entryId"],
                    },
                )

                mutated = True
                pods = activate_target(
                    kube,
                    target,
                    entry["replicaCount"],
                    transition_timeout_seconds=args.transition_timeout_seconds,
                    metrics_timeout_seconds=args.metrics_timeout_seconds,
                    poll_interval_seconds=args.poll_interval_seconds,
                )
                admission_pod_specs = verify_admission_pod_spec_identity(
                    kube,
                    target,
                    pods,
                    args.expected_admission_identities[target.variant_id],
                )
                command = build_runner_command(entry, target, pods, options)
                state_entry = state["entries"][str(sequence)]
                state_entry.update(
                    {
                        "status": "runner-started",
                        "runnerStartedAt": timestamp(),
                        "webPods": pods,
                        "admissionPodSpecIdentity": {
                            "expected": args.expected_admission_identities[
                                target.variant_id
                            ],
                            "actual": admission_pod_specs,
                        },
                        "runnerCommand": command,
                    }
                )
                state["updatedAt"] = timestamp()
                atomic_write_json(state_path, state)
                append_event(
                    events_path,
                    {
                        "timestamp": timestamp(),
                        "sequence": sequence,
                        "event": "runner-started",
                        "entryId": entry["entryId"],
                        "webPods": pods,
                    },
                )

                log_path = logs_root / f"{sequence:03d}-{entry['runnerCaseId']}.log"
                returncode = run_streamed(command, log_path)
                result_path = case_dir / "result.json"
                if not result_path.is_file():
                    state_entry.update(
                        {
                            "status": "fatal",
                            "completedAt": timestamp(),
                            "runnerExitStatus": returncode,
                            "reason": "runner produced no result.json",
                        }
                    )
                    state["status"] = "fatal"
                    state["updatedAt"] = timestamp()
                    atomic_write_json(state_path, state)
                    raise SafetyError(
                        "Runner stopped before producing result.json; this "
                        "entry cannot be selectively retried"
                    )
                result = load_json(result_path)
                result_status = result.get("result")
                if (returncode == 0) != (result_status == "passed"):
                    state_entry.update(
                        {
                            "status": "fatal",
                            "completedAt": timestamp(),
                            "runnerExitStatus": returncode,
                            "result": result_status,
                            "reason": ("runner exit status and result.json disagree"),
                        }
                    )
                    state["status"] = "fatal"
                    state["updatedAt"] = timestamp()
                    atomic_write_json(state_path, state)
                    raise SafetyError("Runner exit status and result artifact disagree")

                cooldown_evidence = ensure_inter_entry_cooldown(
                    case_dir,
                    entry["cooldownSeconds"],
                )
                append_event(
                    events_path,
                    {
                        "timestamp": timestamp(),
                        "sequence": sequence,
                        "event": "inter-entry-cooldown-completed",
                        "entryId": entry["entryId"],
                        **cooldown_evidence,
                    },
                )
                state_entry.update(
                    {
                        "status": "completed",
                        "completedAt": timestamp(),
                        "runnerExitStatus": returncode,
                        "result": result_status,
                        "interEntryCooldown": cooldown_evidence,
                    }
                )
                state["nextSequence"] = sequence + 1
                state["updatedAt"] = timestamp()
                append_event(
                    events_path,
                    {
                        "timestamp": timestamp(),
                        "sequence": sequence,
                        "event": "runner-completed",
                        "entryId": entry["entryId"],
                        "result": result_status,
                        "exitStatus": returncode,
                    },
                )
                # Publish the resumable cursor only after every required event
                # for this sequence has been written.
                atomic_write_json(state_path, state)

            state["status"] = "completed"
            state["completedAt"] = timestamp()
            state["updatedAt"] = timestamp()
            atomic_write_json(state_path, state)
        finally:
            if mutated:
                active_exception = sys.exc_info()[0] is not None
                try:
                    quiesce_all(
                        kube,
                        timeout_seconds=args.transition_timeout_seconds,
                        interval_seconds=args.poll_interval_seconds,
                    )
                except Exception as error:
                    append_event(
                        events_path,
                        {
                            "timestamp": timestamp(),
                            "event": "cleanup-failed",
                            "error": str(error),
                        },
                    )
                    state["cleanupError"] = str(error)
                    state["updatedAt"] = timestamp()
                    atomic_write_json(state_path, state)
                    if not active_exception:
                        raise SafetyError(
                            f"Exact candidate quiesce failed: {error}"
                        ) from error
                    print(
                        f"WARNING: exact candidate quiesce failed: {error}",
                        file=sys.stderr,
                    )
    finally:
        fcntl.flock(run_lock.fileno(), fcntl.LOCK_UN)
        run_lock.close()


def add_document_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE)
    parser.add_argument(
        "--runtime-admission-identities",
        type=Path,
        default=DEFAULT_ADMISSION_IDENTITIES,
    )
    parser.add_argument(
        "--bundle-manifest",
        type=Path,
        default=DEFAULT_BUNDLE_MANIFEST,
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--generator", type=Path, default=DEFAULT_GENERATOR)
    parser.add_argument("--runner", type=Path, default=DEFAULT_RUNNER)


def add_runpod_arguments(
    parser: argparse.ArgumentParser,
    *,
    required: bool,
) -> None:
    parser.add_argument(
        "--load-generator-backend",
        choices=("runpod-ssh",),
        required=required,
        default=None if required else "runpod-ssh",
    )
    parser.add_argument(
        "--load-generator-identity",
        type=Path,
        required=required,
        default=(
            None
            if required
            else Path("/runpod-identity/identity.json")
        ),
    )
    parser.add_argument(
        "--load-generator-identity-key",
        type=Path,
        required=required,
        default=(
            None
            if required
            else Path("/opt/bluemap-runtime/credentials/id_ed25519")
        ),
    )
    parser.add_argument(
        "--traffic-base-url",
        required=required,
        default=None if required else DEFAULT_TRAFFIC_BASE_URL,
    )
    parser.add_argument(
        "--traffic-service",
        required=required,
        default=None if required else TRAFFIC_SERVICE,
    )
    parser.add_argument(
        "--traffic-service-port",
        type=int,
        required=required,
        default=None if required else TRAFFIC_SERVICE_PORT,
    )
    parser.add_argument(
        "--formal-run-id",
        required=required,
        default=None if required else "formal-dry-run",
    )
    parser.add_argument("--require-edge-bypass", action="store_true")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate the frozen inputs without contacting Kubernetes",
    )
    add_document_arguments(validate_parser)
    validate_parser.add_argument(
        "--documents-only",
        action="store_true",
        help="skip the clean-HEAD/revision check",
    )

    dry_parser = subparsers.add_parser(
        "dry-run",
        help="render the exact 80-entry action plan without contacting Kubernetes",
    )
    add_document_arguments(dry_parser)
    dry_parser.add_argument(
        "--documents-only",
        action="store_true",
        help="skip the clean-HEAD/revision check",
    )
    dry_parser.add_argument(
        "--output",
        type=Path,
        default=SCRIPT_DIR / "dry-run-plan.json",
    )
    dry_parser.add_argument(
        "--benchmark-python",
        type=Path,
        default=Path(os.environ.get("BENCHMARK_PYTHON", sys.executable)),
    )
    dry_parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    dry_parser.add_argument(
        "--prometheus-url",
        default=DEFAULT_PROMETHEUS_URL,
    )
    dry_parser.add_argument("--no-prometheus", action="store_true")
    add_runpod_arguments(dry_parser, required=False)

    run_parser = subparsers.add_parser(
        "run",
        help="execute the frozen schedule against the allowlisted Deployments",
    )
    add_document_arguments(run_parser)
    run_parser.add_argument("--run-root", type=Path, required=True)
    run_parser.add_argument("--confirm", required=True)
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument(
        "--benchmark-python",
        type=Path,
        default=Path(os.environ.get("BENCHMARK_PYTHON", sys.executable)),
    )
    run_parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    run_parser.add_argument(
        "--prometheus-url",
        default=DEFAULT_PROMETHEUS_URL,
    )
    run_parser.add_argument("--no-prometheus", action="store_true")
    run_parser.add_argument(
        "--transition-timeout-seconds",
        type=int,
        default=300,
    )
    run_parser.add_argument(
        "--metrics-timeout-seconds",
        type=int,
        default=180,
    )
    run_parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=2.0,
    )
    add_runpod_arguments(run_parser, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        matrix, schedule = validate_formal_documents(
            args.matrix.resolve(),
            args.schedule.resolve(),
            args.manifest.resolve(),
            args.generator.resolve(),
        )
        args.matrix = args.matrix.resolve()
        args.schedule = args.schedule.resolve()
        args.runtime_admission_identities = args.runtime_admission_identities.resolve()
        args.bundle_manifest = args.bundle_manifest.resolve()
        args.manifest = args.manifest.resolve()
        args.generator = args.generator.resolve()
        args.runner = args.runner.resolve()
        if hasattr(args, "benchmark_python"):
            args.benchmark_python = args.benchmark_python.resolve()
        if hasattr(args, "kubeconfig"):
            args.kubeconfig = args.kubeconfig.resolve()
        if hasattr(args, "load_generator_identity"):
            args.load_generator_identity = args.load_generator_identity.resolve()
        if hasattr(args, "load_generator_identity_key"):
            args.load_generator_identity_key = (
                args.load_generator_identity_key.resolve()
            )
        (
            args.expected_admission_identities,
            args.frozen_bundle,
        ) = validate_formal_bundle(
            args.matrix,
            args.schedule,
            args.runtime_admission_identities,
            args.bundle_manifest,
            matrix["benchmarkGitRevision"],
        )
        if args.command in {"validate", "dry-run"}:
            if not args.documents_only:
                validate_repository_freeze(
                    matrix["benchmarkGitRevision"],
                    args.runner,
                )
            if args.command == "validate":
                print(
                    json.dumps(
                        {
                            "valid": True,
                            "formatVersion": 3,
                            "entries": len(schedule["entries"]),
                            "blocks": 5,
                            "benchmarkGitRevision": matrix["benchmarkGitRevision"],
                            "matrixSha256": file_sha256(args.matrix),
                            "scheduleSha256": file_sha256(args.schedule),
                            "runtimeAdmissionIdentitiesSha256": file_sha256(
                                args.runtime_admission_identities
                            ),
                            "bundleManifestSha256": file_sha256(args.bundle_manifest),
                            "orchestratorSha256": file_sha256(Path(__file__)),
                            "clusterContacted": False,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            options = RunnerOptions(
                runner=args.runner,
                matrix=args.matrix,
                schedule=args.schedule,
                manifest=args.manifest,
                artifact_root=Path("<run-root>/results"),
                benchmark_python=args.benchmark_python,
                kubeconfig=args.kubeconfig,
                prometheus_url=(None if args.no_prometheus else args.prometheus_url),
                load_generator_identity=args.load_generator_identity,
                load_generator_identity_key=args.load_generator_identity_key,
                traffic_base_url=args.traffic_base_url,
                traffic_service=args.traffic_service,
                traffic_service_port=args.traffic_service_port,
                formal_run_id=args.formal_run_id,
                require_edge_bypass=True,
            )
            plan = {
                "formatVersion": 1,
                "generatedAt": timestamp(),
                "clusterContacted": False,
                "scaleDownExactly": [
                    f"deployment/{name}" for name in FORMAL_DEPLOYMENTS
                ],
                "activateExactly": [
                    {
                        "deployment": f"deployment/{target.deployment}",
                        "replicas": target.replica_count,
                    }
                    for target in TARGETS.values()
                ],
                "frozenBundle": {
                    "directory": str(args.bundle_manifest.parent),
                    "bundleManifestSha256": file_sha256(args.bundle_manifest),
                    "runtimeAdmissionIdentitiesSha256": file_sha256(
                        args.runtime_admission_identities
                    ),
                    "orchestratorSha256": file_sha256(Path(__file__)),
                },
                "protectedResourcesNeverPassedToKubernetes": sorted(
                    PROTECTED_RESOURCES
                ),
                "entries": plan_entries(schedule, options),
            }
            atomic_write_json(args.output.resolve(), plan)
            print(args.output.resolve())
            return 0

        if (
            args.transition_timeout_seconds < 30
            or args.metrics_timeout_seconds < 30
            or not 0.1 <= args.poll_interval_seconds <= 30
        ):
            raise SafetyError("Run timeouts or polling interval are unsafe")
        execute_schedule(matrix, schedule, args)
        return 0
    except (SafetyError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"FORMAL ORCHESTRATION REFUSED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
