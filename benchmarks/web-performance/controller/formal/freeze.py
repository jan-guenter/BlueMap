#!/usr/bin/env python3
"""Freeze the six formal BlueMap candidates into a v3 matrix and schedule.

The tracked helper is revision-agnostic: a controller lock initialized for
the current clean commit binds the reviewed controller sources, then this
helper captures the disposable candidates for that same commit.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orchestrate

SCRIPT_DIR = Path(__file__).resolve().parent
BENCHMARK_ROOT = orchestrate.BENCHMARK_ROOT
REPOSITORY_ROOT = orchestrate.REPOSITORY_ROOT
TOOLS_DIR = BENCHMARK_ROOT / "tools"

REQUIRED_REVISION = subprocess.run(
    ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "--verify", "HEAD^{commit}"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
if (
    len(REQUIRED_REVISION) != 40
    or any(character not in "0123456789abcdef" for character in REQUIRED_REVISION)
):
    raise RuntimeError("Could not resolve a full lowercase benchmark Git revision")

MATRIX_EXAMPLE = BENCHMARK_ROOT / "matrix.example.json"
RUNTIME_IDENTITY = TOOLS_DIR / "runtime_identity.py"
SANITIZE_RESOURCE = TOOLS_DIR / "sanitize_kubernetes_resource.py"
SANITIZE_CONFIGMAP = TOOLS_DIR / "sanitize_configmap.py"
SCHEDULE_GENERATOR = TOOLS_DIR / "generate_schedule.py"
DEFAULT_OUTPUT_DIR = BENCHMARK_ROOT / "artifacts" / "snapshot"
DEFAULT_MANIFEST = DEFAULT_OUTPUT_DIR / "manifest.json"
ARCHIVE_NAME = f"runtime-identity-freeze-{REQUIRED_REVISION}"
BUNDLE_NAME = "formal-inputs"
STAGING_BUNDLE_NAME = f".{BUNDLE_NAME}-{REQUIRED_REVISION}.staging"
CONTROL_LOCK = orchestrate.CONTROL_LOCK
PLACEHOLDER = "REPLACE_WITH_"
ANALYZER = SCRIPT_DIR / "analyze.py"

REVIEWED_CONTROLLERS = {
    "freeze.py": Path(__file__).resolve(),
    "orchestrate.py": SCRIPT_DIR / "orchestrate.py",
    "analyze.py": ANALYZER,
}

COMMITTED_INPUTS = (
    MATRIX_EXAMPLE,
    RUNTIME_IDENTITY,
    SANITIZE_RESOURCE,
    SANITIZE_CONFIGMAP,
    SCHEDULE_GENERATOR,
)


@dataclass(frozen=True)
class Toolchain:
    runtime_identity: Path
    sanitize_resource: Path
    sanitize_configmap: Path
    schedule_generator: Path


LIVE_TOOLCHAIN = Toolchain(
    runtime_identity=RUNTIME_IDENTITY,
    sanitize_resource=SANITIZE_RESOURCE,
    sanitize_configmap=SANITIZE_CONFIGMAP,
    schedule_generator=SCHEDULE_GENERATOR,
)


def validate_control_lock() -> tuple[dict[str, str], str]:
    if not CONTROL_LOCK.is_file():
        raise orchestrate.SafetyError(
            f"Reviewed controller lock is missing: {CONTROL_LOCK}"
        )
    value = orchestrate.load_json(CONTROL_LOCK)
    if (
        set(value) != {"formatVersion", "requiredRevision", "controllers"}
        or value.get("formatVersion") != 1
        or value.get("requiredRevision") != REQUIRED_REVISION
    ):
        raise orchestrate.SafetyError(
            "Reviewed controller lock has the wrong format or revision"
        )
    controllers = value.get("controllers")
    if not isinstance(controllers, list) or len(controllers) != len(
        REVIEWED_CONTROLLERS
    ):
        raise orchestrate.SafetyError(
            "Reviewed controller lock must bind every formal controller"
        )
    expected_paths = list(REVIEWED_CONTROLLERS)
    if [
        item.get("path") for item in controllers if isinstance(item, dict)
    ] != expected_paths:
        raise orchestrate.SafetyError(
            "Reviewed controller lock helper paths or ordering differ"
        )
    hashes: dict[str, str] = {}
    for item in controllers:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise orchestrate.SafetyError(
                "Reviewed controller lock contains an invalid helper entry"
            )
        path = REVIEWED_CONTROLLERS[item["path"]]
        expected_digest = validate_identity_digest(
            item["sha256"],
            f"reviewed {item['path']} controller",
        )
        actual_digest = orchestrate.file_sha256(path)
        if actual_digest != expected_digest:
            raise orchestrate.SafetyError(
                f"Reviewed controller {item['path']} changed: "
                f"expected={expected_digest}, actual={actual_digest}"
            )
        hashes[item["path"]] = actual_digest
    return hashes, orchestrate.file_sha256(CONTROL_LOCK)


def required_confirmation() -> str:
    _, lock_digest = validate_control_lock()
    return f"FREEZE-FORMAL-MATRIX-{REQUIRED_REVISION}-CONTROL-{lock_digest}"


def invoke_json_tool(
    command: Sequence[str],
    *,
    input_value: dict[str, Any] | None = None,
) -> Any:
    result = subprocess.run(
        list(command),
        cwd=REPOSITORY_ROOT,
        input=(
            json.dumps(input_value, separators=(",", ":"))
            if input_value is not None
            else None
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise orchestrate.SafetyError(
            f"Identity tool failed ({result.returncode}): {' '.join(command)}"
            + (f"\n{detail}" if detail else "")
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise orchestrate.SafetyError(
            f"Identity tool returned invalid JSON: {' '.join(command)}"
        ) from error


def placeholders(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, str) and PLACEHOLDER in value:
        found.append(path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(placeholders(item, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(placeholders(item, f"{path}.{key}"))
    return found


def validate_template(
    template: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    orchestrate.validate_target_constants()
    if template.get("formatVersion") != 3 or template.get("repetitions") != 5:
        raise orchestrate.SafetyError(
            "matrix.example.json must be the five-block v3 template"
        )
    variants = template.get("variants")
    if not isinstance(variants, list) or len(variants) != 6:
        raise orchestrate.SafetyError("Matrix template must contain six variants")
    variant_ids = [
        variant.get("id") for variant in variants if isinstance(variant, dict)
    ]
    if variant_ids != list(orchestrate.TARGETS):
        raise orchestrate.SafetyError(
            "Matrix template variants or ordering differ from the target map"
        )
    for variant in variants:
        target = orchestrate.TARGETS[variant["id"]]
        expected = {
            "contractMode": target.contract_mode,
            "implementation": target.implementation,
            "storageType": "sql",
            "databaseBackend": "postgresql",
            "replicaCount": target.replica_count,
        }
        for field, expected_value in expected.items():
            if variant.get(field) != expected_value:
                raise orchestrate.SafetyError(
                    f"Template variant {target.variant_id} has unexpected {field}"
                )
        images = variant.get("expectedImages")
        if not isinstance(images, list) or not images:
            raise orchestrate.SafetyError(
                f"Template variant {target.variant_id} has no expectedImages"
            )
        for image in images:
            if (
                not isinstance(image, dict)
                or set(image) != {"kind", "name", "digest"}
                or PLACEHOLDER not in str(image.get("digest"))
            ):
                raise orchestrate.SafetyError(
                    f"Template variant {target.variant_id} image is not a placeholder"
                )
        for field in (
            "expectedSanitizedConfigSha256",
            "expectedSanitizedRuntimeSpecSha256",
        ):
            if PLACEHOLDER not in str(variant.get(field)):
                raise orchestrate.SafetyError(
                    f"Template variant {target.variant_id} {field} is not a placeholder"
                )

    if PLACEHOLDER not in str(template.get("benchmarkGitRevision")):
        raise orchestrate.SafetyError(
            "Template benchmarkGitRevision is not a placeholder"
        )
    if PLACEHOLDER not in str(template.get("manifestSha256")):
        raise orchestrate.SafetyError("Template manifestSha256 is not a placeholder")
    map_ids = manifest.get("mapIds")
    if (
        not isinstance(map_ids, list)
        or not map_ids
        or map_ids != sorted(set(map_ids))
        or any(not isinstance(map_id, str) or not map_id for map_id in map_ids)
    ):
        raise orchestrate.SafetyError(
            "Snapshot manifest mapIds must be a sorted unique nonempty string array"
        )


def validate_local_inputs(
    manifest_path: Path,
    *,
    documents_only: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    for path in (
        *COMMITTED_INPUTS,
        manifest_path,
        Path(__file__),
        SCRIPT_DIR / "orchestrate.py",
        ANALYZER,
        CONTROL_LOCK,
    ):
        if not path.is_file():
            raise orchestrate.SafetyError(f"Required freeze input is missing: {path}")
    validate_control_lock()
    if not documents_only:
        orchestrate.validate_repository_freeze(REQUIRED_REVISION, RUNTIME_IDENTITY)
        for path in COMMITTED_INPUTS:
            relative = path.resolve().relative_to(REPOSITORY_ROOT.resolve())
            orchestrate.run_checked(
                [
                    "git",
                    "cat-file",
                    "-e",
                    f"{REQUIRED_REVISION}:{relative.as_posix()}",
                ],
                cwd=REPOSITORY_ROOT,
            )
    template = orchestrate.load_json(MATRIX_EXAMPLE)
    manifest = orchestrate.load_json(manifest_path)
    validate_template(template, manifest)
    return template, manifest


def validate_identity_digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        or set(value) == {"0"}
    ):
        raise orchestrate.SafetyError(f"{label} is not a resolved SHA-256 digest")
    return value


def build_frozen_matrix(
    template: dict[str, Any],
    manifest: dict[str, Any],
    identities: dict[str, dict[str, Any]],
    manifest_sha256: str,
) -> dict[str, Any]:
    validate_template(template, manifest)
    if set(identities) != set(orchestrate.TARGETS):
        raise orchestrate.SafetyError(
            "Captured identities do not exactly cover the six formal variants"
        )
    matrix = json.loads(json.dumps(template))
    matrix["benchmarkGitRevision"] = REQUIRED_REVISION
    matrix["manifestSha256"] = validate_identity_digest(
        manifest_sha256,
        "manifestSha256",
    )
    matrix["mapIds"] = manifest["mapIds"]

    for variant in matrix["variants"]:
        variant_id = variant["id"]
        identity = identities[variant_id]
        images = identity.get("expectedImages")
        if not isinstance(images, list) or not images:
            raise orchestrate.SafetyError(
                f"Captured identity for {variant_id} has no images"
            )
        expected_keys = [
            (image.get("kind"), image.get("name"))
            for image in variant["expectedImages"]
        ]
        actual_keys = [
            (image.get("kind"), image.get("name"))
            for image in images
            if isinstance(image, dict)
        ]
        if actual_keys != expected_keys or len(actual_keys) != len(images):
            raise orchestrate.SafetyError(
                f"Captured container set for {variant_id} differs from the template"
            )
        normalized_images = []
        for index, image in enumerate(images):
            digest = image.get("digest")
            if (
                not isinstance(digest, str)
                or not digest.startswith("sha256:")
                or len(digest) != 71
            ):
                raise orchestrate.SafetyError(
                    f"Captured image {variant_id}[{index}] has no OCI digest"
                )
            validate_identity_digest(
                digest.removeprefix("sha256:"),
                f"{variant_id} image digest",
            )
            normalized_images.append(
                {
                    "kind": image["kind"],
                    "name": image["name"],
                    "digest": digest,
                }
            )
        variant["expectedImages"] = normalized_images
        variant["expectedSanitizedConfigSha256"] = validate_identity_digest(
            identity.get("expectedSanitizedConfigSha256"),
            f"{variant_id} configuration identity",
        )
        variant["expectedSanitizedRuntimeSpecSha256"] = validate_identity_digest(
            identity.get("expectedSanitizedRuntimeSpecSha256"),
            f"{variant_id} runtime-spec identity",
        )

    unresolved = placeholders(matrix)
    if unresolved:
        raise orchestrate.SafetyError(
            "Frozen matrix still contains placeholders: " + ", ".join(unresolved)
        )
    return matrix


def resource_version(
    resource: dict[str, Any],
    kind: str,
    name: str,
) -> tuple[str, str]:
    metadata = orchestrate.metadata(resource, kind, name)
    uid = metadata.get("uid")
    version = metadata.get("resourceVersion")
    if not isinstance(uid, str) or not uid:
        raise orchestrate.SafetyError(f"{kind}/{name} has no UID")
    if not isinstance(version, str) or not version:
        raise orchestrate.SafetyError(f"{kind}/{name} has no resourceVersion")
    return uid, version


def assert_unchanged(
    before: dict[str, Any],
    after: dict[str, Any],
    kind: str,
    name: str,
) -> None:
    if resource_version(before, kind, name) != resource_version(
        after,
        kind,
        name,
    ):
        raise orchestrate.SafetyError(
            f"{kind}/{name} changed while its identity was captured"
        )


def sanitize_snapshot(
    script: Path,
    resource: dict[str, Any],
    captured_at: str,
) -> dict[str, Any]:
    value = invoke_json_tool(
        [sys.executable, str(script), "--captured-at", captured_at],
        input_value=resource,
    )
    if not isinstance(value, dict):
        raise orchestrate.SafetyError(f"{script.name} returned a non-object")
    return value


def ownership_chain_evidence(
    pod: dict[str, Any],
    replicaset: dict[str, Any],
    deployment: dict[str, Any],
) -> dict[str, Any]:
    pod_name = str(pod.get("metadata", {}).get("name", ""))
    pod_metadata = orchestrate.metadata(pod, "Pod", pod_name)
    pod_controller = orchestrate.controller_reference(
        pod_metadata,
        kind="ReplicaSet",
        label=f"Pod/{pod_name}",
    )
    replicaset_name = pod_controller["name"]
    replicaset_metadata = orchestrate.metadata(
        replicaset,
        "ReplicaSet",
        replicaset_name,
    )
    deployment_name = str(deployment.get("metadata", {}).get("name", ""))
    deployment_metadata = orchestrate.metadata(
        deployment,
        "Deployment",
        deployment_name,
    )
    replicaset_controller = orchestrate.controller_reference(
        replicaset_metadata,
        kind="Deployment",
        label=f"ReplicaSet/{replicaset_name}",
    )
    return {
        "pod": {
            "name": pod_name,
            "uid": pod_metadata.get("uid"),
            "resourceVersion": pod_metadata.get("resourceVersion"),
        },
        "replicaSet": {
            "name": replicaset_name,
            "uid": replicaset_metadata.get("uid"),
            "resourceVersion": replicaset_metadata.get("resourceVersion"),
            "revision": replicaset_metadata.get("annotations", {}).get(
                "deployment.kubernetes.io/revision"
            ),
            "controllerDeploymentName": replicaset_controller.get("name"),
            "controllerDeploymentUid": replicaset_controller.get("uid"),
        },
        "deployment": {
            "name": deployment_name,
            "uid": deployment_metadata.get("uid"),
            "resourceVersion": deployment_metadata.get("resourceVersion"),
            "revision": deployment_metadata.get("annotations", {}).get(
                "deployment.kubernetes.io/revision"
            ),
        },
    }


def capture_variant_identity(
    kube: orchestrate.Kubectl,
    target: orchestrate.VariantTarget,
    web_pods: Sequence[str],
    evidence_dir: Path,
    toolchain: Toolchain = LIVE_TOOLCHAIN,
) -> dict[str, Any]:
    if len(web_pods) != target.replica_count or len(set(web_pods)) != len(web_pods):
        raise orchestrate.SafetyError(
            f"Resolved Pod set for {target.variant_id} has the wrong cardinality"
        )
    if evidence_dir.exists():
        raise orchestrate.SafetyError(
            f"Evidence directory already exists: {evidence_dir}"
        )
    source_dir = evidence_dir / "source-snapshots"
    identity_dir = evidence_dir / "derived-identities"
    source_dir.mkdir(parents=True)
    identity_dir.mkdir()
    captured_at = orchestrate.timestamp()

    deployment = kube.deployment(target.deployment)
    orchestrate.validate_candidate_deployment(deployment, target)
    if not orchestrate.deployment_converged(deployment, target.replica_count):
        raise orchestrate.SafetyError(
            f"Deployment/{target.deployment} is no longer converged"
        )
    service = kube.service(target.service)
    orchestrate.metadata(service, "Service", target.service)
    configmaps = {name: kube.configmap(name) for name in target.configmaps}
    for name, resource in configmaps.items():
        orchestrate.metadata(resource, "ConfigMap", name)

    pods: dict[str, dict[str, Any]] = {}
    replicasets: dict[str, dict[str, Any]] = {}
    for pod_name in sorted(web_pods):
        orchestrate.require_benchmark_name(pod_name, "Pod")
        pod = kube.pod(pod_name)
        orchestrate.metadata(pod, "Pod", pod_name)
        if not orchestrate.ready(pod):
            raise orchestrate.SafetyError(f"Pod/{pod_name} is no longer Ready")
        orchestrate.validate_current_pod_ownership(
            kube,
            pod,
            deployment,
            target,
        )
        pod_metadata = orchestrate.metadata(pod, "Pod", pod_name)
        controller = orchestrate.controller_reference(
            pod_metadata,
            kind="ReplicaSet",
            label=f"Pod/{pod_name}",
        )
        replicaset = kube.replicaset(controller["name"])
        replicaset_metadata = orchestrate.metadata(
            replicaset,
            "ReplicaSet",
            controller["name"],
        )
        if replicaset_metadata.get("uid") != controller["uid"]:
            raise orchestrate.SafetyError(
                f"Pod/{pod_name} ReplicaSet UID changed before capture"
            )
        replicasets[controller["name"]] = replicaset
        pods[pod_name] = pod

    expected_pods = sorted(pods)
    admission_spec_hashes_before = {
        pod_name: orchestrate.admitted_pod_spec_sha256(pod)
        for pod_name, pod in pods.items()
    }
    if len(set(admission_spec_hashes_before.values())) != 1:
        raise orchestrate.SafetyError(
            f"Replicas for {target.variant_id} have different normalized "
            "admission-mutated Pod execution specs"
        )
    expected_admission_spec_sha256 = next(iter(admission_spec_hashes_before.values()))
    ownership_before = [
        ownership_chain_evidence(
            pods[pod_name],
            replicasets[
                orchestrate.controller_reference(
                    orchestrate.metadata(pods[pod_name], "Pod", pod_name),
                    kind="ReplicaSet",
                    label=f"Pod/{pod_name}",
                )["name"]
            ],
            deployment,
        )
        for pod_name in expected_pods
    ]
    endpoint_pods_before = orchestrate.ready_endpoint_pods(
        kube.endpoint_slices(target.service)
    )
    if endpoint_pods_before != expected_pods:
        raise orchestrate.SafetyError(
            f"Service/{target.service} endpoints changed before capture"
        )
    orchestrate.audit_no_other_web_candidates(kube, set(expected_pods))

    service_path = source_dir / f"service-{target.service}.json"
    deployment_path = source_dir / f"deployment-{target.deployment}.json"
    orchestrate.atomic_write_json(
        service_path,
        sanitize_snapshot(toolchain.sanitize_resource, service, captured_at),
    )
    orchestrate.atomic_write_json(
        deployment_path,
        sanitize_snapshot(toolchain.sanitize_resource, deployment, captured_at),
    )
    for name, replicaset in sorted(replicasets.items()):
        orchestrate.atomic_write_json(
            source_dir / f"replicaset-{name}.json",
            sanitize_snapshot(
                toolchain.sanitize_resource,
                replicaset,
                captured_at,
            ),
        )

    config_paths: list[Path] = []
    for name in target.configmaps:
        path = source_dir / f"configmap-{name}.json"
        orchestrate.atomic_write_json(
            path,
            sanitize_snapshot(
                toolchain.sanitize_configmap,
                configmaps[name],
                captured_at,
            ),
        )
        config_paths.append(path)

    image_identities: dict[str, list[dict[str, str]]] = {}
    for pod_name, pod in pods.items():
        pod_path = source_dir / f"pod-{pod_name}.json"
        pod_snapshot = sanitize_snapshot(
            toolchain.sanitize_resource,
            pod,
            captured_at,
        )
        orchestrate.atomic_write_json(pod_path, pod_snapshot)
        images = invoke_json_tool(
            [sys.executable, str(toolchain.runtime_identity), "pod-images"],
            input_value=pod_snapshot,
        )
        if not isinstance(images, list):
            raise orchestrate.SafetyError(
                f"Pod/{pod_name} image identity is not an array"
            )
        image_identities[pod_name] = images
        orchestrate.atomic_write_json(
            identity_dir / f"pod-images-{pod_name}.json",
            images,
        )

    canonical_images = {
        orchestrate.canonical_json(images) for images in image_identities.values()
    }
    if len(canonical_images) != 1:
        raise orchestrate.SafetyError(
            f"Replicas for {target.variant_id} resolved different image identities"
        )
    expected_images = next(iter(image_identities.values()))

    config_identity = invoke_json_tool(
        [
            sys.executable,
            str(toolchain.runtime_identity),
            "config-snapshots",
            *(str(path) for path in config_paths),
        ]
    )
    if not isinstance(config_identity, dict):
        raise orchestrate.SafetyError("Configuration identity is not an object")
    config_identity_path = identity_dir / "configuration.json"
    orchestrate.atomic_write_json(config_identity_path, config_identity)

    runtime_identity = invoke_json_tool(
        [
            sys.executable,
            str(toolchain.runtime_identity),
            "runtime-spec-snapshots",
            "--service",
            str(service_path),
            "--deployment",
            str(deployment_path),
        ]
    )
    if not isinstance(runtime_identity, dict):
        raise orchestrate.SafetyError("Runtime-spec identity is not an object")
    runtime_identity_path = identity_dir / "runtime-spec.json"
    orchestrate.atomic_write_json(runtime_identity_path, runtime_identity)

    current_deployment = kube.deployment(target.deployment)
    orchestrate.validate_candidate_deployment(current_deployment, target)
    if not orchestrate.deployment_converged(
        current_deployment,
        target.replica_count,
    ):
        raise orchestrate.SafetyError(
            f"Deployment/{target.deployment} changed during capture"
        )
    assert_unchanged(
        deployment,
        current_deployment,
        "Deployment",
        target.deployment,
    )
    assert_unchanged(
        service,
        kube.service(target.service),
        "Service",
        target.service,
    )
    for name, before in configmaps.items():
        assert_unchanged(
            before,
            kube.configmap(name),
            "ConfigMap",
            name,
        )
    current_pods: dict[str, dict[str, Any]] = {}
    for pod_name, before in pods.items():
        current_pod = kube.pod(pod_name)
        assert_unchanged(before, current_pod, "Pod", pod_name)
        if not orchestrate.ready(current_pod):
            raise orchestrate.SafetyError(
                f"Pod/{pod_name} stopped being Ready during capture"
            )
        orchestrate.validate_current_pod_ownership(
            kube,
            current_pod,
            current_deployment,
            target,
        )
        current_pods[pod_name] = current_pod
    current_replicasets: dict[str, dict[str, Any]] = {}
    for name, before in replicasets.items():
        current_replicaset = kube.replicaset(name)
        assert_unchanged(
            before,
            current_replicaset,
            "ReplicaSet",
            name,
        )
        current_replicasets[name] = current_replicaset
    endpoint_pods_after = orchestrate.ready_endpoint_pods(
        kube.endpoint_slices(target.service)
    )
    if endpoint_pods_after != expected_pods:
        raise orchestrate.SafetyError(
            f"Service/{target.service} endpoints changed during capture"
        )
    orchestrate.audit_no_other_web_candidates(kube, set(expected_pods))
    orchestrate.atomic_write_json(
        identity_dir / "endpoint-membership.json",
        {
            "capturedAt": captured_at,
            "service": target.service,
            "expectedReadyPods": expected_pods,
            "before": endpoint_pods_before,
            "after": endpoint_pods_after,
        },
    )
    ownership_after = [
        ownership_chain_evidence(
            current_pods[pod_name],
            current_replicasets[
                orchestrate.controller_reference(
                    orchestrate.metadata(
                        current_pods[pod_name],
                        "Pod",
                        pod_name,
                    ),
                    kind="ReplicaSet",
                    label=f"Pod/{pod_name}",
                )["name"]
            ],
            current_deployment,
        )
        for pod_name in expected_pods
    ]
    orchestrate.atomic_write_json(
        identity_dir / "ownership-chains.json",
        {
            "capturedAt": captured_at,
            "before": ownership_before,
            "after": ownership_after,
            "stable": ownership_before == ownership_after,
        },
    )
    if ownership_before != ownership_after:
        raise orchestrate.SafetyError(
            f"Ownership evidence changed during capture for {target.variant_id}"
        )
    admission_spec_hashes_after = {
        pod_name: orchestrate.admitted_pod_spec_sha256(pod)
        for pod_name, pod in current_pods.items()
    }
    if admission_spec_hashes_after != admission_spec_hashes_before:
        raise orchestrate.SafetyError(
            f"Normalized admission-mutated Pod execution spec changed during "
            f"capture for {target.variant_id}"
        )
    orchestrate.atomic_write_json(
        identity_dir / "admission-pod-spec-identity.json",
        {
            "formatVersion": (orchestrate.ADMISSION_POD_SPEC_IDENTITY_VERSION),
            "capturedAt": captured_at,
            "normalization": {
                "excludedTopLevelPodFields": [
                    "metadata",
                    "status",
                ],
                "excludedPodSpecFields": ["nodeName"],
                "normalizedControllerUniqueFields": [
                    "generated service-account projected volume name",
                    "matching volumeMount and volumeDevice names",
                ],
            },
            "before": admission_spec_hashes_before,
            "after": admission_spec_hashes_after,
            "expectedAdmissionPodSpecSha256": (expected_admission_spec_sha256),
        },
    )

    identity = {
        "variantId": target.variant_id,
        "capturedAt": captured_at,
        "release": target.release,
        "service": target.service,
        "deployment": target.deployment,
        "replicaSets": sorted(replicasets),
        "configMaps": list(target.configmaps),
        "webPods": expected_pods,
        "expectedImages": expected_images,
        "expectedSanitizedConfigSha256": validate_identity_digest(
            config_identity.get("sanitizedConfigSha256"),
            f"{target.variant_id} configuration identity",
        ),
        "expectedSanitizedRuntimeSpecSha256": validate_identity_digest(
            runtime_identity.get("sanitizedRuntimeSpecSha256"),
            f"{target.variant_id} runtime-spec identity",
        ),
        "expectedAdmissionPodSpecSha256": validate_identity_digest(
            expected_admission_spec_sha256,
            f"{target.variant_id} admission Pod-spec identity",
        ),
    }
    orchestrate.atomic_write_json(evidence_dir / "identity.json", identity)
    return identity


def copy_input(path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise orchestrate.SafetyError(f"Archive input already exists: {destination}")
    shutil.copyfile(path, destination)


def archive_file_inventory(
    archive_root: Path,
    identities: dict[str, dict[str, Any]],
) -> None:
    controller_hashes, control_lock_sha256 = validate_control_lock()
    excluded = {
        "SHA256SUMS",
        "archive-manifest.json",
        "state.json",
    }
    files = []
    for path in sorted(item for item in archive_root.rglob("*") if item.is_file()):
        relative = path.relative_to(archive_root).as_posix()
        if relative in excluded:
            continue
        files.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": orchestrate.file_sha256(path),
            }
        )
    manifest = {
        "formatVersion": 1,
        "createdAt": orchestrate.timestamp(),
        "benchmarkGitRevision": REQUIRED_REVISION,
        "controllerLockSha256": control_lock_sha256,
        "controllerSha256": controller_hashes,
        "variants": [
            {
                "variantId": variant_id,
                "webPods": identity["webPods"],
                "expectedImages": identity["expectedImages"],
                "expectedSanitizedConfigSha256": identity[
                    "expectedSanitizedConfigSha256"
                ],
                "expectedSanitizedRuntimeSpecSha256": identity[
                    "expectedSanitizedRuntimeSpecSha256"
                ],
                "expectedAdmissionPodSpecSha256": identity[
                    "expectedAdmissionPodSpecSha256"
                ],
            }
            for variant_id, identity in identities.items()
        ],
        "files": files,
    }
    archive_manifest_path = archive_root / "archive-manifest.json"
    orchestrate.atomic_write_json(archive_manifest_path, manifest)
    checksum_paths = [
        path
        for path in sorted(item for item in archive_root.rglob("*") if item.is_file())
        if path.name not in {"SHA256SUMS", "state.json"}
    ]
    checksum_lines = [
        f"{orchestrate.file_sha256(path)}  {path.relative_to(archive_root).as_posix()}"
        for path in checksum_paths
    ]
    (archive_root / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="utf-8",
    )


def publish_atomic_bundle(
    matrix: Path,
    schedule: Path,
    admission_identities: Path,
    bundle_manifest: Path,
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    bundle = output_dir / BUNDLE_NAME
    staging = output_dir / STAGING_BUNDLE_NAME
    for path in (bundle, staging):
        if path.exists():
            raise orchestrate.SafetyError(
                f"Refusing to overwrite formal input bundle: {path}"
            )
    try:
        staging.mkdir()
        staged_matrix = staging / "matrix.json"
        staged_schedule = staging / "schedule.json"
        staged_admission_identities = staging / "runtime-admission-identities.json"
        shutil.copyfile(matrix, staged_matrix)
        shutil.copyfile(schedule, staged_schedule)
        shutil.copyfile(admission_identities, staged_admission_identities)
        shutil.copyfile(bundle_manifest, staging / "bundle-manifest.json")
        if bundle.exists():
            raise orchestrate.SafetyError(
                f"Refusing to overwrite formal input bundle: {bundle}"
            )
        os.rename(staging, bundle)
    except orchestrate.SafetyError:
        raise
    except OSError as error:
        raise orchestrate.SafetyError(
            f"Could not atomically publish formal input bundle: {error}"
        ) from error
    return (
        bundle / "matrix.json",
        bundle / "schedule.json",
        bundle / "runtime-admission-identities.json",
    )


def create_frozen_documents(
    template: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    manifest_sha256: str,
    identities: dict[str, dict[str, Any]],
    archive_root: Path,
    toolchain: Toolchain = LIVE_TOOLCHAIN,
) -> tuple[Path, Path, Path, Path]:
    final_dir = archive_root / "final"
    final_dir.mkdir()
    matrix_path = final_dir / "matrix.json"
    schedule_path = final_dir / "schedule.json"
    admission_identities_path = final_dir / "runtime-admission-identities.json"
    bundle_manifest_path = final_dir / "bundle-manifest.json"
    frozen_matrix = build_frozen_matrix(
        template,
        manifest,
        identities,
        manifest_sha256,
    )
    orchestrate.atomic_write_json(matrix_path, frozen_matrix)
    orchestrate.run_checked(
        [
            sys.executable,
            str(toolchain.schedule_generator),
            "generate",
            str(matrix_path),
            str(schedule_path),
        ],
        cwd=REPOSITORY_ROOT,
    )
    validation = orchestrate.run_checked(
        [
            sys.executable,
            str(toolchain.schedule_generator),
            "validate",
            str(matrix_path),
            str(schedule_path),
        ],
        cwd=REPOSITORY_ROOT,
    )
    try:
        validation_value = json.loads(validation.stdout)
    except json.JSONDecodeError as error:
        raise orchestrate.SafetyError(
            "Schedule validator returned invalid JSON"
        ) from error
    orchestrate.atomic_write_json(
        final_dir / "schedule-validation.json",
        validation_value,
    )
    orchestrate.validate_formal_documents(
        matrix_path,
        schedule_path,
        manifest_path,
        toolchain.schedule_generator,
    )
    orchestrate.atomic_write_json(
        admission_identities_path,
        {
            "formatVersion": 1,
            "benchmarkGitRevision": REQUIRED_REVISION,
            "podSpecIdentityVersion": (orchestrate.ADMISSION_POD_SPEC_IDENTITY_VERSION),
            "variants": [
                {
                    "variantId": variant_id,
                    "replicaCount": orchestrate.TARGETS[variant_id].replica_count,
                    "expectedAdmissionPodSpecSha256": identity[
                        "expectedAdmissionPodSpecSha256"
                    ],
                }
                for variant_id, identity in identities.items()
            ],
        },
    )
    controller_hashes, control_lock_sha256 = validate_control_lock()
    orchestrate.atomic_write_json(
        bundle_manifest_path,
        {
            "formatVersion": 1,
            "createdAt": orchestrate.timestamp(),
            "benchmarkGitRevision": REQUIRED_REVISION,
            "matrixSha256": orchestrate.file_sha256(matrix_path),
            "scheduleSha256": orchestrate.file_sha256(schedule_path),
            "runtimeAdmissionIdentitiesSha256": (
                orchestrate.file_sha256(admission_identities_path)
            ),
            "controllerLockSha256": control_lock_sha256,
            "orchestratorSha256": controller_hashes["orchestrate.py"],
            "freezerSha256": controller_hashes["freeze.py"],
            "analyzerSha256": controller_hashes["analyze.py"],
        },
    )
    return (
        matrix_path,
        schedule_path,
        admission_identities_path,
        bundle_manifest_path,
    )


def execute_freeze(
    template: dict[str, Any],
    manifest: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    controller_hashes, control_lock_sha256 = validate_control_lock()
    confirmation = required_confirmation()
    if args.confirm != confirmation:
        raise orchestrate.SafetyError(f"--confirm must exactly equal {confirmation!r}")
    orchestrate.validate_repository_freeze(REQUIRED_REVISION, RUNTIME_IDENTITY)
    manifest_sha256 = orchestrate.file_sha256(args.manifest)
    output_dir = orchestrate.validate_run_root(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_root = output_dir / ARCHIVE_NAME
    bundle = output_dir / BUNDLE_NAME
    staging_bundle = output_dir / STAGING_BUNDLE_NAME
    for path in (archive_root, bundle, staging_bundle):
        if path.exists():
            raise orchestrate.SafetyError(
                f"Freeze output already exists; refusing overwrite: {path}"
            )

    kube = orchestrate.Kubectl(args.kubeconfig)
    run_lock = orchestrate.acquire_global_lock(archive_root)
    try:
        archive_root.mkdir()
        evidence_root = archive_root / "evidence"
        inputs_root = archive_root / "inputs"
        evidence_root.mkdir()
        inputs_root.mkdir()
        state_path = archive_root / "state.json"
        state: dict[str, Any] = {
            "formatVersion": 1,
            "status": "capturing",
            "createdAt": orchestrate.timestamp(),
            "updatedAt": orchestrate.timestamp(),
            "benchmarkGitRevision": REQUIRED_REVISION,
            "controllerLockSha256": control_lock_sha256,
            "controllerSha256": controller_hashes,
            "nextVariant": next(iter(orchestrate.TARGETS)),
            "variants": {},
        }
        orchestrate.atomic_write_json(state_path, state)
        for path in COMMITTED_INPUTS:
            copy_input(path, inputs_root / path.name)
            if orchestrate.file_sha256(path) != orchestrate.file_sha256(
                inputs_root / path.name
            ):
                raise orchestrate.SafetyError(
                    f"Committed input changed while it was archived: {path}"
                )
        copy_input(args.manifest, inputs_root / "manifest.json")
        if orchestrate.file_sha256(inputs_root / "manifest.json") != manifest_sha256:
            raise orchestrate.SafetyError("Manifest changed while it was archived")
        for name, path in REVIEWED_CONTROLLERS.items():
            copy_input(path, inputs_root / name)
        copy_input(CONTROL_LOCK, inputs_root / CONTROL_LOCK.name)
        for name, expected_digest in controller_hashes.items():
            if orchestrate.file_sha256(inputs_root / name) != expected_digest:
                raise orchestrate.SafetyError(
                    f"Reviewed controller changed while it was archived: {name}"
                )
        if (
            orchestrate.file_sha256(inputs_root / CONTROL_LOCK.name)
            != control_lock_sha256
        ):
            raise orchestrate.SafetyError(
                "Reviewed controller lock changed while it was archived"
            )
        orchestrate.validate_repository_freeze(
            REQUIRED_REVISION,
            RUNTIME_IDENTITY,
        )
        archived_toolchain = Toolchain(
            runtime_identity=inputs_root / RUNTIME_IDENTITY.name,
            sanitize_resource=inputs_root / SANITIZE_RESOURCE.name,
            sanitize_configmap=inputs_root / SANITIZE_CONFIGMAP.name,
            schedule_generator=inputs_root / SCHEDULE_GENERATOR.name,
        )
        archived_input_hashes = {
            path.name: orchestrate.file_sha256(inputs_root / path.name)
            for path in COMMITTED_INPUTS
        }
    except BaseException:
        fcntl.flock(run_lock.fileno(), fcntl.LOCK_UN)
        run_lock.close()
        raise

    identities: dict[str, dict[str, Any]] = {}
    mutated = False
    try:
        try:
            targets = list(orchestrate.TARGETS.values())
            for index, target in enumerate(targets):
                state["nextVariant"] = target.variant_id
                state["updatedAt"] = orchestrate.timestamp()
                state["variants"][target.variant_id] = {
                    "status": "activating",
                    "startedAt": orchestrate.timestamp(),
                }
                orchestrate.atomic_write_json(state_path, state)
                mutated = True
                web_pods = orchestrate.activate_target(
                    kube,
                    target,
                    target.replica_count,
                    transition_timeout_seconds=args.transition_timeout_seconds,
                    metrics_timeout_seconds=args.metrics_timeout_seconds,
                    poll_interval_seconds=args.poll_interval_seconds,
                )
                identity = capture_variant_identity(
                    kube,
                    target,
                    web_pods,
                    evidence_root / target.variant_id,
                    archived_toolchain,
                )
                identities[target.variant_id] = identity
                state["variants"][target.variant_id] = {
                    "status": "captured",
                    "completedAt": orchestrate.timestamp(),
                    "webPods": web_pods,
                    "identity": identity,
                }
                state["nextVariant"] = (
                    targets[index + 1].variant_id if index + 1 < len(targets) else None
                )
                state["updatedAt"] = orchestrate.timestamp()
                orchestrate.atomic_write_json(state_path, state)
        finally:
            if mutated:
                orchestrate.quiesce_all(
                    kube,
                    timeout_seconds=args.transition_timeout_seconds,
                    interval_seconds=args.poll_interval_seconds,
                )

        orchestrate.validate_repository_freeze(
            REQUIRED_REVISION,
            RUNTIME_IDENTITY,
        )
        if orchestrate.file_sha256(args.manifest) != manifest_sha256:
            raise orchestrate.SafetyError(
                "Snapshot manifest changed during runtime-identity capture"
            )
        for name, expected_digest in archived_input_hashes.items():
            if orchestrate.file_sha256(inputs_root / name) != expected_digest:
                raise orchestrate.SafetyError(
                    f"Archived committed input changed during capture: {name}"
                )
        final_controller_hashes, final_control_lock_sha256 = validate_control_lock()
        if (
            final_controller_hashes != controller_hashes
            or final_control_lock_sha256 != control_lock_sha256
        ):
            raise orchestrate.SafetyError(
                "Reviewed local controller binding changed during capture"
            )
        state["status"] = "generating-documents"
        state["updatedAt"] = orchestrate.timestamp()
        orchestrate.atomic_write_json(state_path, state)
        (
            archived_matrix,
            archived_schedule,
            archived_admission_identities,
            archived_bundle_manifest,
        ) = create_frozen_documents(
            template,
            manifest,
            args.manifest,
            manifest_sha256,
            identities,
            archive_root,
            archived_toolchain,
        )
        archive_file_inventory(archive_root, identities)
        (
            output_matrix,
            output_schedule,
            output_admission_identities,
        ) = publish_atomic_bundle(
            archived_matrix,
            archived_schedule,
            archived_admission_identities,
            archived_bundle_manifest,
            output_dir,
        )
        state.update(
            {
                "status": "completed",
                "completedAt": orchestrate.timestamp(),
                "updatedAt": orchestrate.timestamp(),
                "matrix": {
                    "path": str(output_matrix),
                    "sha256": orchestrate.file_sha256(output_matrix),
                },
                "schedule": {
                    "path": str(output_schedule),
                    "sha256": orchestrate.file_sha256(output_schedule),
                },
                "runtimeAdmissionIdentities": {
                    "path": str(output_admission_identities),
                    "sha256": orchestrate.file_sha256(output_admission_identities),
                },
                "bundleManifest": {
                    "path": str(output_dir / BUNDLE_NAME / "bundle-manifest.json"),
                    "sha256": orchestrate.file_sha256(
                        output_dir / BUNDLE_NAME / "bundle-manifest.json"
                    ),
                },
                "archiveManifestSha256": orchestrate.file_sha256(
                    archive_root / "archive-manifest.json"
                ),
                "sha256SumsSha256": orchestrate.file_sha256(
                    archive_root / "SHA256SUMS"
                ),
            }
        )
        orchestrate.atomic_write_json(state_path, state)
    except BaseException as error:
        state["status"] = "failed"
        state["failedAt"] = orchestrate.timestamp()
        state["updatedAt"] = orchestrate.timestamp()
        state["error"] = f"{type(error).__name__}: {error}"
        orchestrate.atomic_write_json(state_path, state)
        raise
    finally:
        fcntl.flock(run_lock.fileno(), fcntl.LOCK_UN)
        run_lock.close()


def freeze_plan(manifest_path: Path, output_dir: Path) -> dict[str, Any]:
    return {
        "formatVersion": 1,
        "clusterContacted": False,
        "benchmarkGitRevision": REQUIRED_REVISION,
        "requiredConfirmation": required_confirmation(),
        "manifest": str(manifest_path.resolve()),
        "outputDirectory": str(output_dir.resolve()),
        "archiveDirectory": str((output_dir / ARCHIVE_NAME).resolve()),
        "atomicOutputBundle": str((output_dir / BUNDLE_NAME).resolve()),
        "scaleDownExactly": [
            f"deployment/{name}" for name in orchestrate.FORMAL_DEPLOYMENTS
        ],
        "activateExactly": [
            {
                "deployment": f"deployment/{target.deployment}",
                "replicas": target.replica_count,
            }
            for target in orchestrate.TARGETS.values()
        ],
        "protectedResourcesNeverPassedToKubernetes": sorted(
            orchestrate.PROTECTED_RESOURCES
        ),
        "variants": [
            {
                "variantId": target.variant_id,
                "release": target.release,
                "deployment": target.deployment,
                "replicas": target.replica_count,
                "service": target.service,
                "servicePort": target.port,
                "configMaps": list(target.configmaps),
                "contractMode": target.contract_mode,
                "evidence": {
                    "podImages": "all exact Ready replicas",
                    "configuration": "sanitized selected ConfigMaps",
                    "runtimeSpec": "sanitized Service and Deployment",
                },
            }
            for target in orchestrate.TARGETS.values()
        ],
        "finalDocuments": [
            f"{BUNDLE_NAME}/matrix.json",
            f"{BUNDLE_NAME}/schedule.json",
            f"{BUNDLE_NAME}/runtime-admission-identities.json",
            f"{BUNDLE_NAME}/bundle-manifest.json",
        ],
    }


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate exact local inputs without contacting Kubernetes",
    )
    add_common_arguments(validate_parser)
    validate_parser.add_argument("--documents-only", action="store_true")

    dry_parser = subparsers.add_parser(
        "dry-run",
        help="render the exact freeze plan without contacting Kubernetes",
    )
    add_common_arguments(dry_parser)
    dry_parser.add_argument("--documents-only", action="store_true")
    dry_parser.add_argument(
        "--plan-output",
        type=Path,
        default=SCRIPT_DIR / "freeze-dry-run.json",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="capture runtime identities and generate the frozen documents",
    )
    add_common_arguments(run_parser)
    run_parser.add_argument("--confirm", required=True)
    run_parser.add_argument(
        "--kubeconfig",
        type=Path,
        default=orchestrate.DEFAULT_KUBECONFIG,
    )
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        args.manifest = args.manifest.resolve()
        args.output_dir = args.output_dir.resolve()
        documents_only = getattr(args, "documents_only", False)
        template, manifest = validate_local_inputs(
            args.manifest,
            documents_only=documents_only,
        )
        if args.command == "validate":
            print(
                json.dumps(
                    {
                        "valid": True,
                        "clusterContacted": False,
                        "benchmarkGitRevision": REQUIRED_REVISION,
                        "variants": list(orchestrate.TARGETS),
                        "manifestSha256": orchestrate.file_sha256(args.manifest),
                        "requiredConfirmation": required_confirmation(),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "dry-run":
            plan = freeze_plan(args.manifest, args.output_dir)
            orchestrate.atomic_write_json(args.plan_output.resolve(), plan)
            print(args.plan_output.resolve())
            return 0
        if (
            args.transition_timeout_seconds < 30
            or args.metrics_timeout_seconds < 30
            or not 0.1 <= args.poll_interval_seconds <= 30
        ):
            raise orchestrate.SafetyError("Run timeouts or polling interval are unsafe")
        execute_freeze(template, manifest, args)
        return 0
    except (
        orchestrate.SafetyError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        print(f"FORMAL FREEZE REFUSED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
