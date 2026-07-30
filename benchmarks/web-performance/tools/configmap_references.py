#!/usr/bin/env python3
"""List ConfigMaps referenced by an exact Pod or Deployment JSON resource."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from typing import Any


IGNORED_SYSTEM_CONFIGMAPS = {"kube-root-ca.crt"}


def container_references(containers: Iterable[dict[str, Any]]) -> set[str]:
    references: set[str] = set()
    for container in containers:
        for source in container.get("envFrom", []):
            name = source.get("configMapRef", {}).get("name")
            if name:
                references.add(name)
        for variable in container.get("env", []):
            name = (
                variable.get("valueFrom", {})
                .get("configMapKeyRef", {})
                .get("name")
            )
            if name:
                references.add(name)
    return references


def pod_spec_references(spec: dict[str, Any]) -> set[str]:
    references: set[str] = set()
    for volume in spec.get("volumes", []):
        direct_name = volume.get("configMap", {}).get("name")
        if direct_name:
            references.add(direct_name)
        for source in volume.get("projected", {}).get("sources", []):
            projected_name = source.get("configMap", {}).get("name")
            if projected_name:
                references.add(projected_name)

    for container_field in ("initContainers", "containers", "ephemeralContainers"):
        references.update(container_references(spec.get(container_field, [])))
    return references - IGNORED_SYSTEM_CONFIGMAPS


def references(resource: dict[str, Any]) -> list[str]:
    kind = resource.get("kind")
    if kind == "Deployment":
        spec = resource.get("spec", {}).get("template", {}).get("spec", {})
    elif kind == "Pod":
        spec = resource.get("spec", {})
    else:
        raise ValueError("resource kind must be Pod or Deployment")
    if not isinstance(spec, dict):
        raise ValueError("resource has no Pod spec")
    return sorted(pod_spec_references(spec))


def main() -> int:
    try:
        resource = json.load(sys.stdin)
        json.dump(references(resource), sys.stdout)
        sys.stdout.write("\n")
    except (json.JSONDecodeError, ValueError) as error:
        print(f"CONFIGMAP REFERENCE FAILURE: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
