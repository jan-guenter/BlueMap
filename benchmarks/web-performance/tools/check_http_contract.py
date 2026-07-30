#!/usr/bin/env python3
"""Validate BlueMap data responses before accepting benchmark results."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from email.message import Message
from pathlib import Path

import zstandard


@dataclass(frozen=True)
class Response:
    status: int
    headers: Message
    body: bytes


class ContractFailure(AssertionError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--mode",
        choices=("enhanced", "legacy"),
        default="enhanced",
    )
    parser.add_argument(
        "--stored-encoding",
        choices=("gzip", "zstd", "deflate", "identity"),
        default="zstd",
    )
    parser.add_argument("--user-agent", default="BlueMap-Contract-Check/local")
    return parser.parse_args()


def fetch(
    base_url: str,
    path: str,
    headers: dict[str, str],
    method: str = "GET",
) -> Response:
    url = urllib.parse.urljoin(f"{base_url.rstrip('/')}/", path.lstrip("/"))
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return Response(response.status, response.headers, response.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, error.headers, error.read())


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractFailure(message)


def header_tokens(response: Response, name: str) -> set[str]:
    return {
        token.strip().lower()
        for value in response.headers.get_all(name, [])
        for token in value.split(",")
        if token.strip()
    }


def decode_body(response: Response) -> bytes:
    encoding = response.headers.get("Content-Encoding", "identity").lower()
    if encoding == "identity":
        return response.body
    if encoding == "gzip":
        return gzip.decompress(response.body)
    if encoding == "deflate":
        return zlib.decompress(response.body)
    if encoding == "zstd":
        return zstandard.ZstdDecompressor().decompress(response.body)
    raise ContractFailure(f"Unsupported response Content-Encoding {encoding!r}")


def check_body(response: Response, expected: dict[str, object], path: str) -> None:
    body = decode_body(response)
    digest = hashlib.sha256(body).hexdigest()
    require(
        digest == expected["decodedSha256"],
        f"{path}: decoded SHA-256 differs ({digest})",
    )
    require(
        len(body) == expected["decodedSize"],
        f"{path}: decoded body size differs ({len(body)})",
    )


def check_enhanced_contract(
    base_url: str,
    manifest: dict[str, object],
    stored_encoding: str,
    base_headers: dict[str, str],
) -> None:
    tile = str(manifest["hotTile"])
    expected = manifest["expected"][tile]
    response = fetch(
        base_url,
        tile,
        {**base_headers, "Accept-Encoding": stored_encoding},
    )
    require(response.status == 200, f"{tile}: expected 200, got {response.status}")
    check_body(response, expected, tile)

    actual_encoding = response.headers.get("Content-Encoding", "identity").lower()
    require(
        actual_encoding == stored_encoding,
        f"{tile}: expected Content-Encoding {stored_encoding}, got {actual_encoding}",
    )
    require(
        "accept-encoding" in header_tokens(response, "Vary"),
        f"{tile}: Vary does not include Accept-Encoding",
    )
    cache_control = header_tokens(response, "Cache-Control")
    require("public" in cache_control, f"{tile}: tile response is not public-cacheable")
    require(
        "must-revalidate" in cache_control,
        f"{tile}: tile response does not require stale revalidation",
    )

    etag = response.headers.get("ETag")
    last_modified = response.headers.get("Last-Modified")
    require(bool(etag), f"{tile}: ETag is missing")
    require(bool(last_modified), f"{tile}: Last-Modified is missing")

    head = fetch(
        base_url,
        tile,
        {**base_headers, "Accept-Encoding": stored_encoding},
        method="HEAD",
    )
    require(head.status == 200, f"{tile}: HEAD expected 200, got {head.status}")
    require(head.body == b"", f"{tile}: HEAD returned a body")
    require(head.headers.get("ETag") == etag, f"{tile}: HEAD ETag differs from GET")

    not_modified = fetch(
        base_url,
        tile,
        {
            **base_headers,
            "Accept-Encoding": stored_encoding,
            "If-None-Match": str(etag),
        },
    )
    require(
        not_modified.status == 304,
        f"{tile}: matching If-None-Match expected 304, got {not_modified.status}",
    )
    require(not_modified.body == b"", f"{tile}: 304 returned a body")
    require(
        not_modified.headers.get("ETag") == etag,
        f"{tile}: 304 omitted or changed ETag",
    )
    require(
        "accept-encoding" in header_tokens(not_modified, "Vary"),
        f"{tile}: 304 omitted Vary: Accept-Encoding",
    )

    weak_not_modified = fetch(
        base_url,
        tile,
        {
            **base_headers,
            "Accept-Encoding": stored_encoding,
            "If-None-Match": f"W/{etag}",
        },
    )
    require(
        weak_not_modified.status == 304,
        f"{tile}: weak matching If-None-Match expected 304, got {weak_not_modified.status}",
    )

    time_not_modified = fetch(
        base_url,
        tile,
        {
            **base_headers,
            "Accept-Encoding": stored_encoding,
            "If-Modified-Since": str(last_modified),
        },
    )
    require(
        time_not_modified.status == 304,
        f"{tile}: matching If-Modified-Since expected 304, got {time_not_modified.status}",
    )

    precedence = fetch(
        base_url,
        tile,
        {
            **base_headers,
            "Accept-Encoding": stored_encoding,
            "If-None-Match": '"not-the-current-tag"',
            "If-Modified-Since": "Fri, 31 Dec 9999 23:59:59 GMT",
        },
    )
    require(
        precedence.status == 200,
        f"{tile}: If-None-Match did not take precedence over If-Modified-Since",
    )

    unsupported = fetch(
        base_url,
        tile,
        {
            **base_headers,
            "Accept-Encoding": (
                "gzip;q=0, deflate;q=0, zstd;q=0, identity;q=1, *;q=0"
                if stored_encoding != "identity"
                else "identity;q=0, *;q=0"
            ),
        },
    )
    require(
        unsupported.status == 406,
        f"{tile}: unsupported encoding expected 406, got {unsupported.status}",
    )
    require(
        unsupported.headers.get("X-BlueMap-Required-Content-Encoding")
        == stored_encoding,
        f"{tile}: 406 omitted the required encoding",
    )
    require(
        "no-store" in header_tokens(unsupported, "Cache-Control"),
        f"{tile}: 406 is cacheable",
    )
    require(
        "accept-encoding" in header_tokens(unsupported, "Vary"),
        f"{tile}: 406 omitted Vary: Accept-Encoding",
    )
    try:
        problem = json.loads(unsupported.body)
    except json.JSONDecodeError as error:
        raise ContractFailure(f"{tile}: 406 body is not JSON") from error
    require(
        problem.get("requiredEncoding") == stored_encoding,
        f"{tile}: 406 JSON omitted the required encoding",
    )

    if manifest["players"]:
        player = str(manifest["players"][0])
        player_response = fetch(
            base_url,
            player,
            {**base_headers, "Accept-Encoding": stored_encoding},
        )
        require(
            player_response.status == 200,
            f"{player}: expected 200, got {player_response.status}",
        )
        check_body(player_response, manifest["expected"][player], player)
        player_cache = header_tokens(player_response, "Cache-Control")
        require(
            {"private", "no-store"}.issubset(player_cache),
            f"{player}: player positions are not private, no-store",
        )

    if manifest["markers"]:
        marker = str(manifest["markers"][0])
        marker_response = fetch(
            base_url,
            marker,
            {**base_headers, "Accept-Encoding": stored_encoding},
        )
        require(
            marker_response.status == 200,
            f"{marker}: expected 200, got {marker_response.status}",
        )
        check_body(marker_response, manifest["expected"][marker], marker)
        require(
            "no-cache" in header_tokens(marker_response, "Cache-Control"),
            f"{marker}: marker data is not revalidated",
        )

    missing = str(manifest["missingTile"])
    missing_response = fetch(
        base_url,
        missing,
        {**base_headers, "Accept-Encoding": stored_encoding},
    )
    require(
        missing_response.status == 204,
        f"{missing}: expected 204, got {missing_response.status}",
    )
    require(
        "no-store" in header_tokens(missing_response, "Cache-Control"),
        f"{missing}: missing tile result is cacheable",
    )

    post = fetch(
        base_url,
        tile,
        {**base_headers, "Accept-Encoding": stored_encoding},
        method="POST",
    )
    require(post.status == 405, f"{tile}: POST expected 405, got {post.status}")
    require(
        {"get", "head"}.issubset(header_tokens(post, "Allow")),
        f"{tile}: 405 Allow header is incomplete",
    )


def check_legacy_body(
    base_url: str,
    manifest: dict[str, object],
    stored_encoding: str,
    base_headers: dict[str, str],
) -> None:
    tile = str(manifest["hotTile"])
    response = fetch(
        base_url,
        tile,
        {**base_headers, "Accept-Encoding": stored_encoding},
    )
    require(response.status == 200, f"{tile}: expected 200, got {response.status}")
    check_body(response, manifest["expected"][tile], tile)


def main() -> int:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    headers = {"User-Agent": args.user_agent}
    try:
        if args.mode == "enhanced":
            check_enhanced_contract(
                args.base_url,
                manifest,
                args.stored_encoding,
                headers,
            )
        else:
            check_legacy_body(
                args.base_url,
                manifest,
                args.stored_encoding,
                headers,
            )
    except ContractFailure as error:
        print(f"CONTRACT FAILURE: {error}", file=sys.stderr)
        return 1

    print(f"{args.mode} HTTP contract passed for {args.base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
