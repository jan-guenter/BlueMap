#!/usr/bin/env python3
"""Create and validate immutable identities for formal benchmark runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import sanitize_configmap


SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")
CONTAINER_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
RESOURCE_NAME = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*$"
)
IMAGE_KINDS = {
    "container": ("containers", "containerStatuses"),
    "ephemeralContainer": (
        "ephemeralContainers",
        "ephemeralContainerStatuses",
    ),
    "initContainer": ("initContainers", "initContainerStatuses"),
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def validate_digest(value: object, name: str, *, prefix: bool) -> str:
    pattern = SHA256 if prefix else HEX_SHA256
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        expected = "sha256: plus 64" if prefix else "64"
        raise ValueError(
            f"{name} must contain {expected} lowercase hexadecimal characters"
        )
    hexadecimal = value.removeprefix("sha256:")
    if set(hexadecimal) == {"0"}:
        raise ValueError(f"{name} is an unresolved all-zero placeholder")
    return value


def validate_git_revision(value: object, name: str) -> str:
    if not isinstance(value, str) or GIT_REVISION.fullmatch(value) is None:
        raise ValueError(f"{name} must be an exact 40-character Git revision")
    if set(value) == {"0"}:
        raise ValueError(f"{name} is an unresolved all-zero placeholder")
    return value


def validate_expected_images(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("expectedImages must be a non-empty array")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"kind", "name", "digest"}:
            raise ValueError(
                f"expectedImages[{index}] must contain exactly kind, name, and digest"
            )
        kind = item["kind"]
        name = item["name"]
        if not isinstance(kind, str) or kind not in IMAGE_KINDS:
            raise ValueError(f"expectedImages[{index}].kind is invalid")
        if (
            not isinstance(name, str)
            or len(name) > 63
            or CONTAINER_NAME.fullmatch(name) is None
        ):
            raise ValueError(f"expectedImages[{index}].name is invalid")
        digest = validate_digest(
            item["digest"],
            f"expectedImages[{index}].digest",
            prefix=True,
        )
        key = (kind, name)
        if key in seen:
            raise ValueError(f"expectedImages contains duplicate {kind} {name!r}")
        seen.add(key)
        normalized.append({"kind": kind, "name": name, "digest": digest})

    sorted_value = sorted(normalized, key=lambda item: (item["kind"], item["name"]))
    if normalized != sorted_value:
        raise ValueError("expectedImages must be sorted by kind and name")
    return normalized


def image_digest(image_id: object, label: str) -> str:
    if not isinstance(image_id, str):
        raise ValueError(f"{label} has no imageID")
    match = re.search(r"(sha256:[0-9a-f]{64})$", image_id)
    if match is None:
        raise ValueError(f"{label} imageID is not an immutable SHA-256 OCI digest")
    return validate_digest(match.group(1), f"{label} imageID", prefix=True)


def container_names(
    resource: dict[str, Any],
    field: str,
    label: str,
) -> set[str]:
    spec = resource.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("Pod has no spec")
    containers = spec.get(field, [])
    if not isinstance(containers, list):
        raise ValueError(f"Pod spec.{field} must be an array")

    names: set[str] = set()
    for item in containers:
        if not isinstance(item, dict):
            raise ValueError(f"Pod spec.{field} contains a non-object")
        name = item.get("name")
        if (
            not isinstance(name, str)
            or len(name) > 63
            or CONTAINER_NAME.fullmatch(name) is None
        ):
            raise ValueError(f"Pod spec.{field} contains an invalid name")
        if name in names:
            raise ValueError(f"Pod spec.{field} contains duplicate container {name!r}")
        names.add(name)
    return names


def pod_images(value: dict[str, Any]) -> list[dict[str, str]]:
    resource = value.get("resource", value)
    if not isinstance(resource, dict):
        raise ValueError("pod-images input has no Pod object")
    if resource.get("kind") != "Pod":
        raise ValueError("pod-images input must be a Pod or sanitized Pod snapshot")

    status = resource.get("status")
    if not isinstance(status, dict):
        raise ValueError("Pod has no status")

    images: list[dict[str, str]] = []
    for kind, (spec_field, status_field) in IMAGE_KINDS.items():
        expected_names = container_names(resource, spec_field, kind)
        statuses = status.get(status_field, [])
        if not isinstance(statuses, list):
            raise ValueError(f"Pod status.{status_field} must be an array")
        status_names: set[str] = set()
        for item in statuses:
            if not isinstance(item, dict):
                raise ValueError(f"Pod status.{status_field} contains a non-object")
            name = item.get("name")
            if (
                not isinstance(name, str)
                or len(name) > 63
                or CONTAINER_NAME.fullmatch(name) is None
            ):
                raise ValueError(f"Pod status.{status_field} contains an invalid name")
            if name in status_names:
                raise ValueError(
                    f"Pod status.{status_field} contains duplicate container {name!r}"
                )
            status_names.add(name)
            images.append(
                {
                    "kind": kind,
                    "name": name,
                    "digest": image_digest(item.get("imageID"), f"{kind} {name!r}"),
                }
            )
        if status_names != expected_names:
            raise ValueError(
                f"Pod {kind} spec/status container names do not exactly match"
            )

    if not images:
        raise ValueError("Pod status contains no container image identities")
    images.sort(key=lambda item: (item["kind"], item["name"]))
    validate_expected_images(images)
    return images


def config_identity_from_snapshots(
    snapshots: list[dict[str, Any]],
) -> dict[str, object]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for snapshot in snapshots:
        resource = snapshot.get("resource", snapshot)
        if not isinstance(resource, dict):
            raise ValueError("configuration identity contains a non-object")
        if resource.get("kind") != "ConfigMap":
            raise ValueError("configuration identity accepts only ConfigMaps")
        metadata = resource.get("metadata", {})
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if (
            not isinstance(name, str)
            or len(name) > 253
            or RESOURCE_NAME.fullmatch(name) is None
        ):
            raise ValueError("ConfigMap has no valid metadata.name")
        if name in seen:
            raise ValueError(f"configuration contains duplicate ConfigMap {name!r}")
        seen.add(name)
        data = resource.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"ConfigMap {name!r} has no sanitized data object")
        if any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in data.items()
        ):
            raise ValueError(f"ConfigMap {name!r} data entries must be strings")
        entries.append(
            {
                "name": name,
                "sanitizedDataSha256": hashlib.sha256(
                    canonical_json(data)
                ).hexdigest(),
            }
        )

    if not entries:
        raise ValueError("configuration identity requires at least one ConfigMap")
    entries.sort(key=lambda item: item["name"])
    return {
        "configMaps": entries,
        "sanitizedConfigSha256": hashlib.sha256(canonical_json(entries)).hexdigest(),
    }


def sanitize_configmaps(value: dict[str, Any]) -> list[dict[str, Any]]:
    if value.get("kind") == "List":
        resources = value.get("items")
        if not isinstance(resources, list):
            raise ValueError("ConfigMap List has no items array")
    else:
        resources = [value]
    snapshots = []
    for resource in resources:
        if not isinstance(resource, dict):
            raise ValueError("ConfigMap input contains a non-object")
        snapshots.append(sanitize_configmap.snapshot(resource, "identity"))
    return snapshots


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("pod-images")
    snapshots = subparsers.add_parser("config-snapshots")
    snapshots.add_argument("snapshot", nargs="+", type=Path)
    subparsers.add_parser("configmaps")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "pod-images":
            resource = json.load(sys.stdin)
            if not isinstance(resource, dict):
                raise ValueError("Pod input must be a JSON object")
            result: object = pod_images(resource)
        elif args.command == "config-snapshots":
            result = config_identity_from_snapshots(
                [load_json(path) for path in args.snapshot]
            )
        else:
            resource = json.load(sys.stdin)
            if not isinstance(resource, dict):
                raise ValueError("ConfigMap input must be a JSON object")
            result = config_identity_from_snapshots(sanitize_configmaps(resource))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"RUNTIME IDENTITY FAILURE: {error}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
