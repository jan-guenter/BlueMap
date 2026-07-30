#!/usr/bin/env python3
"""Probe cold, warm, and client-revalidated BlueMap delivery behavior."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


PROBE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--probe-id", required=True)
    parser.add_argument("--accept-encoding", default="zstd")
    parser.add_argument("--user-agent", default="BlueMap-Cache-Probe/1")
    parser.add_argument("--require-cloudflare-cache", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", default=30, type=float)
    return parser.parse_args()


def safe_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base URL must use http or https and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain a query or fragment")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )


def selected_targets(manifest: dict[str, Any]) -> list[dict[str, str]]:
    targets = [{"class": "tile", "path": manifest["hotTile"]}]
    for endpoint_class, field in (
        ("settings", "settings"),
        ("markers", "markers"),
        ("players", "players"),
    ):
        values = manifest.get(field, [])
        if values:
            targets.append({"class": endpoint_class, "path": values[0]})
    return targets


def probe_url(base_url: str, path: str, probe_id: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    separator = "&" if "?" in normalized_path else "?"
    return (
        f"{base_url}{normalized_path}{separator}"
        f"bluemap-cache-probe={urllib.parse.quote(probe_id, safe='')}"
    )


def request(
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    request_value = urllib.request.Request(url, headers=headers, method="GET")
    started = time.monotonic()
    try:
        response = opener.open(request_value, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read()
        response_headers = response.headers
        return {
            "status": response.status,
            "durationMilliseconds": (time.monotonic() - started) * 1000,
            "transferredBytes": len(body),
            "transferredSha256": hashlib.sha256(body).hexdigest(),
            "age": response_headers.get("Age"),
            "cfCacheStatus": response_headers.get("CF-Cache-Status"),
            "etag": response_headers.get("ETag"),
            "lastModified": response_headers.get("Last-Modified"),
            "cacheControl": response_headers.get("Cache-Control"),
            "contentEncoding": response_headers.get(
                "Content-Encoding",
                "identity",
            ),
            "contentLength": response_headers.get("Content-Length"),
        }


def execute(args: argparse.Namespace) -> dict[str, Any]:
    base_url = safe_base_url(args.base_url)
    if not PROBE_ID.fullmatch(args.probe_id):
        raise ValueError("probe id must be 1-63 lowercase letters, digits, or hyphens")
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    targets = selected_targets(manifest)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    common_headers = {
        "Accept-Encoding": args.accept_encoding,
        "User-Agent": args.user_agent,
    }
    errors = []
    results = []
    for target in targets:
        url = probe_url(base_url, target["path"], args.probe_id)
        cold = request(opener, url, common_headers, args.timeout)
        warm = request(opener, url, common_headers, args.timeout)
        validator = warm["etag"] or cold["etag"]
        revalidated = None
        if validator:
            revalidated = request(
                opener,
                url,
                {**common_headers, "If-None-Match": validator},
                args.timeout,
            )
        else:
            errors.append(f"{target['class']}: response has no ETag")

        if cold["status"] != 200 or warm["status"] != 200:
            errors.append(f"{target['class']}: cold/warm status was not 200")
        if cold["transferredSha256"] != warm["transferredSha256"]:
            errors.append(f"{target['class']}: cold/warm bodies differ")
        if cold["etag"] != warm["etag"]:
            errors.append(f"{target['class']}: cold/warm ETags differ")
        if revalidated is not None and revalidated["status"] != 304:
            errors.append(f"{target['class']}: revalidated status was not 304")
        if revalidated is not None and revalidated["transferredBytes"] != 0:
            errors.append(f"{target['class']}: 304 transferred a body")

        cache_control = (warm["cacheControl"] or "").lower()
        if target["class"] == "players":
            if not {"private", "no-store"} <= {
                token.strip() for token in cache_control.split(",")
            }:
                errors.append("players: Cache-Control is not private, no-store")
            player_responses = [
                (phase, response)
                for phase, response in (
                    ("cold", cold),
                    ("warm", warm),
                    ("revalidated", revalidated),
                )
                if response is not None
            ]
            hit_phases = [
                phase
                for phase, response in player_responses
                if (response["cfCacheStatus"] or "").upper() == "HIT"
            ]
            age_phases = [
                phase
                for phase, response in player_responses
                if response["age"] is not None
            ]
            if hit_phases:
                errors.append(
                    "players: CF-Cache-Status was HIT for "
                    + ", ".join(hit_phases)
                )
            if age_phases:
                errors.append(
                    "players: Age was present for " + ", ".join(age_phases)
                )
        if args.require_cloudflare_cache and target["class"] == "tile":
            if cold["cfCacheStatus"] != "MISS":
                errors.append("tile: cold Cloudflare response was not MISS")
            if warm["cfCacheStatus"] != "HIT":
                errors.append("tile: warm Cloudflare response was not HIT")
            if warm["age"] is None:
                errors.append("tile: warm Cloudflare response has no Age")

        results.append(
            {
                **target,
                "url": url,
                "cold": cold,
                "warm": warm,
                "revalidated": revalidated,
            }
        )

    return {
        "formatVersion": 1,
        "baseUrl": base_url,
        "probeId": args.probe_id,
        "acceptEncoding": args.accept_encoding,
        "requireCloudflareCache": args.require_cloudflare_cache,
        "targets": results,
        "errors": errors,
        "passed": not errors,
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    try:
        result = execute(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        result = {"passed": False, "errors": [str(error)]}
    atomic_json(args.output, result)
    if not result["passed"]:
        for error in result["errors"]:
            print(f"CACHE PROBE FAILURE: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
