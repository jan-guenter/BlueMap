#!/usr/bin/env python3
"""Create a non-secret, reproducible Kubernetes resource snapshot."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

SENSITIVE_ENV_NAME = re.compile(
    r"(?:password|passwd|token|credential|api[_-]?key|private[_-]?key|client[_-]?key)",
    re.IGNORECASE,
)
SENSITIVE_ASSIGNMENT = re.compile(
    r"((?:password|passwd|token|credential|api[_-]?key|private[_-]?key)"
    r"\s*=\s*)([^,\s&]+)",
    re.IGNORECASE,
)
URI_USERINFO = re.compile(r"(://[^:/@\s]+:)([^/@\s]+)(@)")
SENSITIVE_ARGUMENT = re.compile(
    r"^--?(?:password|passwd|token|credential|api[_-]?key|"
    r"private[_-]?key|client[_-]?key)$",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--captured-at", required=True)
    return parser.parse_args()


def redact_string(value: str) -> str:
    value = SENSITIVE_ASSIGNMENT.sub(r"\1<redacted>", value)
    return URI_USERINFO.sub(r"\1<redacted>\3", value)


def sanitize(value: Any) -> Any:
    if isinstance(value, list):
        sanitized_items: list[Any] = []
        redact_next = False
        for item in value:
            if redact_next and isinstance(item, str):
                sanitized_items.append("<redacted>")
                redact_next = False
                continue

            sanitized_items.append(sanitize(item))
            redact_next = (
                isinstance(item, str)
                and SENSITIVE_ARGUMENT.fullmatch(item.strip()) is not None
            )
        return sanitized_items

    if not isinstance(value, dict):
        return redact_string(value) if isinstance(value, str) else value

    sanitized: dict[str, Any] = {}
    env_name = value.get("name")
    sensitive_env = isinstance(env_name, str) and SENSITIVE_ENV_NAME.search(env_name)

    for key, child in value.items():
        if key in {"data", "stringData"} or key == "value" and sensitive_env:
            sanitized[key] = "<redacted>"
        else:
            sanitized[key] = sanitize(child)
    return sanitized


def snapshot(resource: dict[str, Any], captured_at: str) -> dict[str, Any]:
    kind = resource.get("kind")
    if kind in {"Secret", "ConfigMap"}:
        raise ValueError(f"Refusing to snapshot Kubernetes {kind} resources")

    metadata = resource.get("metadata", {})
    return {
        "capturedAt": captured_at,
        "resource": {
            "apiVersion": resource.get("apiVersion"),
            "kind": kind,
            "metadata": {
                key: metadata[key]
                for key in (
                    "name",
                    "namespace",
                    "uid",
                    "resourceVersion",
                    "generation",
                    "labels",
                )
                if key in metadata
            },
            "spec": sanitize(resource.get("spec", {})),
            "status": sanitize(resource.get("status", {})),
        },
    }


def main() -> int:
    args = parse_args()
    try:
        resource = json.load(sys.stdin)
        result = snapshot(resource, args.captured_at)
    except (json.JSONDecodeError, ValueError) as error:
        print(f"SNAPSHOT FAILURE: {error}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
