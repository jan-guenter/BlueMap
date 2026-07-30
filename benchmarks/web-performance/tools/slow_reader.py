#!/usr/bin/env python3
"""Read one large HTTP response slowly for graceful-drain verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--bytes-per-second", type=int, default=32 * 1024)
    parser.add_argument("--chunk-size", type=int, default=4096)
    parser.add_argument("--initial-delay-seconds", type=float, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--expected-status", type=int, default=200)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--accept-encoding", default="zstd")
    parser.add_argument("--user-agent", default="BlueMap-Slow-Reader/local")
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def safe_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain credentials")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.bytes_per_second < 1:
        raise ValueError("--bytes-per-second must be positive")
    if args.chunk_size < 1:
        raise ValueError("--chunk-size must be positive")
    if args.initial_delay_seconds < 0:
        raise ValueError("--initial-delay-seconds must not be negative")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")

    recorded_url = safe_url(args.url)
    request = urllib.request.Request(
        args.url,
        headers={
            "Accept-Encoding": args.accept_encoding,
            "User-Agent": args.user_agent,
        },
        method="GET",
    )
    started = time.monotonic()
    deadline = started + args.timeout_seconds
    started_at = time.time()
    digest = hashlib.sha256()
    byte_count = 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    try:
        response = opener.open(request, timeout=args.timeout_seconds)
    except urllib.error.HTTPError as error:
        response = error

    with response:
        status = response.status
        content_length_header = response.headers.get("Content-Length")
        content_length = (
            int(content_length_header) if content_length_header is not None else None
        )
        ready = {
            "url": recorded_url,
            "status": status,
            "contentLength": content_length,
            "contentEncoding": response.headers.get("Content-Encoding", "identity"),
            "etag": response.headers.get("ETag"),
            "readyAtEpochSeconds": time.time(),
        }
        if status != args.expected_status:
            raise RuntimeError(
                f"expected HTTP {args.expected_status}, received HTTP {status}"
            )
        atomic_json_write(args.ready_file, ready)

        if args.initial_delay_seconds:
            if time.monotonic() + args.initial_delay_seconds > deadline:
                raise RuntimeError("overall response timeout elapsed before reading")
            time.sleep(args.initial_delay_seconds)

        read_started = time.monotonic()
        while True:
            chunk = response.read(args.chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)
            target_elapsed = byte_count / args.bytes_per_second
            remaining = target_elapsed - (time.monotonic() - read_started)
            if remaining > 0:
                if time.monotonic() + remaining > deadline:
                    raise RuntimeError("overall response timeout elapsed while reading")
                time.sleep(remaining)
            if time.monotonic() > deadline:
                raise RuntimeError("overall response timeout elapsed while reading")

    transferred_sha256 = digest.hexdigest()
    if content_length is not None and byte_count != content_length:
        raise RuntimeError(
            f"incomplete response: expected {content_length} bytes, received {byte_count}"
        )
    if (
        args.expected_sha256 is not None
        and transferred_sha256.lower() != args.expected_sha256.lower()
    ):
        raise RuntimeError(
            "transferred representation SHA-256 did not match --expected-sha256"
        )

    ended_at = time.time()
    return {
        **ready,
        "startedAtEpochSeconds": started_at,
        "completedAtEpochSeconds": ended_at,
        "durationSeconds": ended_at - started_at,
        "bytesRead": byte_count,
        "transferredSha256": transferred_sha256,
        "complete": True,
    }


def main() -> int:
    args = parse_args()
    try:
        result = execute(args)
    except (OSError, RuntimeError, ValueError) as error:
        atomic_json_write(
            args.output,
            {
                "complete": False,
                "error": str(error),
                "failedAtEpochSeconds": time.time(),
            },
        )
        print(f"SLOW READER FAILURE: {error}", file=sys.stderr)
        return 1

    atomic_json_write(args.output, result)
    print(
        f"Slow response completed: {result['bytesRead']} bytes in "
        f"{result['durationSeconds']:.3f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
