#!/usr/bin/env python3
"""Capture an explicitly selected ConfigMap without leaking credentials."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from sanitize_kubernetes_resource import redact_string


SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|token|credential|api[_-]?key|private[_-]?key|"
    r"client[_-]?key|secret)",
    re.IGNORECASE,
)
SENSITIVE_STRUCTURED_VALUE = re.compile(
    r"""(?ix)
    (
        ["']?
        (?:password|passwd|token|credential|api[_-]?key|private[_-]?key|
           client[_-]?key|secret)
        ["']?
        \s*[:=]\s*
    )
    (?:
        ["'][^"'\r\n]*["']
        |
        [^\s,}\r\n]+
    )
    """
)
PRIVATE_KEY_MARKER = "-----BEGIN PRIVATE KEY-----"
ENCRYPTED_PRIVATE_KEY_MARKER = "-----BEGIN ENCRYPTED PRIVATE KEY-----"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--captured-at", required=True)
    return parser.parse_args()


def sanitize_text(value: str) -> str:
    if (
        PRIVATE_KEY_MARKER in value
        or ENCRYPTED_PRIVATE_KEY_MARKER in value
        or "-----BEGIN RSA PRIVATE KEY-----" in value
        or "-----BEGIN EC PRIVATE KEY-----" in value
    ):
        raise ValueError("ConfigMap contains private-key material")
    return SENSITIVE_STRUCTURED_VALUE.sub(r"\1<redacted>", redact_string(value))


def snapshot(resource: dict[str, Any], captured_at: str) -> dict[str, Any]:
    if resource.get("kind") != "ConfigMap":
        raise ValueError("Only Kubernetes ConfigMap resources are accepted")
    if resource.get("binaryData"):
        raise ValueError("ConfigMaps with binaryData are not safe to snapshot")

    metadata = resource.get("metadata", {})
    data = resource.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("ConfigMap data must be an object")

    sanitized_data: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("ConfigMap data entries must be strings")
        sanitized_data[key] = (
            "<redacted>" if SENSITIVE_KEY.search(key) else sanitize_text(value)
        )

    return {
        "capturedAt": captured_at,
        "resource": {
            "apiVersion": resource.get("apiVersion"),
            "kind": "ConfigMap",
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
            "immutable": resource.get("immutable", False),
            "data": sanitized_data,
        },
    }


def main() -> int:
    args = parse_args()
    try:
        resource = json.load(sys.stdin)
        result = snapshot(resource, args.captured_at)
    except (json.JSONDecodeError, ValueError) as error:
        print(f"CONFIG SNAPSHOT FAILURE: {error}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
