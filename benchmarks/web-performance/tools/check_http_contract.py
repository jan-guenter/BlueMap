#!/usr/bin/env python3
"""Validate BlueMap data responses before accepting benchmark results."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import itertools
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from email.message import Message
from pathlib import Path

import zstandard

HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REQUEST_IDS = itertools.count(1)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Message
    body: bytes


class ContractFailure(AssertionError):
    pass


def emit_http_event(event: str, **details: object) -> None:
    """Write one machine-readable diagnostic event without request secrets."""
    print(
        json.dumps(
            {
                "source": "bluemap-http-contract",
                "event": event,
                **details,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=sys.stderr,
        flush=True,
    )


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
    safe_path = urllib.parse.urlsplit(url).path or "/"
    request_id = next(REQUEST_IDS)
    started = time.monotonic()
    phase = "open"
    emit_http_event(
        "request-start",
        requestId=request_id,
        method=method,
        path=safe_path,
    )
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        try:
            opened = HTTP_OPENER.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            opened = error

        with opened as response:
            status = int(response.status)
            emit_http_event(
                "response-headers",
                requestId=request_id,
                method=method,
                path=safe_path,
                status=status,
                contentLength=response.headers.get("Content-Length"),
                contentEncoding=response.headers.get(
                    "Content-Encoding", "identity"
                ),
                elapsedMilliseconds=round(
                    (time.monotonic() - started) * 1000,
                    3,
                ),
            )
            phase = "body"
            body = response.read()
            emit_http_event(
                "response-complete",
                requestId=request_id,
                method=method,
                path=safe_path,
                status=status,
                bodyBytes=len(body),
                elapsedMilliseconds=round(
                    (time.monotonic() - started) * 1000,
                    3,
                ),
            )
            return Response(status, response.headers, body)
    except Exception as error:
        emit_http_event(
            "request-error",
            requestId=request_id,
            method=method,
            path=safe_path,
            phase=phase,
            errorType=type(error).__name__,
            elapsedMilliseconds=round(
                (time.monotonic() - started) * 1000,
                3,
            ),
        )
        raise


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


def check_strong_sha256_etag(response: Response, etag: str, path: str) -> None:
    match = re.fullmatch(r'"([0-9a-fA-F]{64})"', etag)
    if match is None:
        return
    transferred_digest = hashlib.sha256(response.body).hexdigest()
    require(
        transferred_digest == match.group(1).lower(),
        f"{path}: strong SHA-256 ETag differs from transferred representation",
    )


def validate_manifest(manifest: dict[str, object]) -> None:
    map_ids = manifest.get("mapIds")
    require(
        isinstance(map_ids, list)
        and bool(map_ids)
        and all(isinstance(map_id, str) and map_id for map_id in map_ids),
        "manifest mapIds must be a non-empty string array",
    )
    require(len(map_ids) == len(set(map_ids)), "manifest mapIds contains duplicates")
    prefixes = tuple(f"/maps/{map_id}/" for map_id in map_ids)
    for field in (
        "tiles",
        "settings",
        "textures",
        "assets",
        "players",
        "markers",
    ):
        paths = manifest.get(field)
        require(isinstance(paths, list), f"manifest {field} must be an array")
        require(
            all(
                isinstance(path, str) and path.startswith(prefixes)
                for path in paths
            ),
            f"manifest {field} contains a route outside mapIds",
        )
    for field in ("hotTile", "largeTile", "largeObject", "missingTile"):
        path = manifest.get(field)
        require(
            isinstance(path, str) and path.startswith(prefixes),
            f"manifest {field} is missing or outside mapIds",
        )
    expected = manifest.get("expected")
    require(isinstance(expected, dict), "manifest expected must be an object")
    for path in (
        list(manifest["tiles"])
        + list(manifest["settings"])
        + list(manifest["textures"])
        + list(manifest["assets"])
        + list(manifest["players"])
        + list(manifest["markers"])
    ):
        require(path in expected, f"manifest has no body expectation for {path}")


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
    require("no-transform" in cache_control, f"{tile}: transformations are not forbidden")
    require(
        "must-revalidate" in cache_control,
        f"{tile}: tile response does not require stale revalidation",
    )
    require(
        "no-transform" in cache_control,
        f"{tile}: tile response permits intermediary transformation",
    )

    etag = response.headers.get("ETag")
    last_modified = response.headers.get("Last-Modified")
    require(bool(etag), f"{tile}: ETag is missing")
    require(bool(last_modified), f"{tile}: Last-Modified is missing")
    check_strong_sha256_etag(response, str(etag), tile)
    content_length = response.headers.get("Content-Length")
    require(bool(content_length), f"{tile}: Content-Length is missing")
    require(
        int(str(content_length)) == len(response.body),
        f"{tile}: Content-Length differs from the transferred representation",
    )

    head = fetch(
        base_url,
        tile,
        {**base_headers, "Accept-Encoding": stored_encoding},
        method="HEAD",
    )
    require(head.status == 200, f"{tile}: HEAD expected 200, got {head.status}")
    require(head.body == b"", f"{tile}: HEAD returned a body")
    require(head.headers.get("ETag") == etag, f"{tile}: HEAD ETag differs from GET")
    require(
        head.headers.get("Last-Modified") == last_modified,
        f"{tile}: HEAD Last-Modified differs from GET",
    )
    require(
        head.headers.get("Content-Length") == content_length,
        f"{tile}: HEAD Content-Length differs from GET",
    )
    require(
        head.headers.get("Content-Encoding", "identity").lower()
        == actual_encoding,
        f"{tile}: HEAD Content-Encoding differs from GET",
    )
    require(
        header_tokens(head, "Vary") == header_tokens(response, "Vary"),
        f"{tile}: HEAD Vary differs from GET",
    )
    require(
        header_tokens(head, "Cache-Control") == cache_control,
        f"{tile}: HEAD Cache-Control differs from GET",
    )

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
        not_modified.headers.get("Last-Modified") == last_modified,
        f"{tile}: 304 omitted or changed Last-Modified",
    )
    require(
        not_modified.headers.get("Content-Encoding", "identity").lower()
        == actual_encoding,
        f"{tile}: 304 omitted or changed Content-Encoding",
    )
    require(
        header_tokens(not_modified, "Vary") == header_tokens(response, "Vary"),
        f"{tile}: 304 Vary differs from GET",
    )
    require(
        header_tokens(not_modified, "Cache-Control") == cache_control,
        f"{tile}: 304 Cache-Control differs from GET",
    )

    alternate_strength_etag = (
        str(etag)[2:] if str(etag).startswith("W/") else f"W/{etag}"
    )
    weak_not_modified = fetch(
        base_url,
        tile,
        {
            **base_headers,
            "Accept-Encoding": stored_encoding,
            "If-None-Match": alternate_strength_etag,
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

    for offered_encoding in ("gzip", "deflate", "zstd", "identity", "lz4"):
        if offered_encoding == stored_encoding:
            continue
        accept_encoding = (
            "identity;q=1, *;q=0"
            if offered_encoding == "identity"
            else f"{offered_encoding};q=1, identity;q=0, *;q=0"
        )
        unsupported = fetch(
            base_url,
            tile,
            {
                **base_headers,
                "Accept-Encoding": accept_encoding,
            },
        )
        require(
            unsupported.status == 406,
            f"{tile}: offering only {offered_encoding} expected 406, "
            f"got {unsupported.status}",
        )
        require(
            unsupported.headers.get("X-BlueMap-Required-Content-Encoding")
            == stored_encoding,
            f"{tile}: 406 omitted the required encoding",
        )
        require(
            unsupported.headers.get_content_type() == "application/problem+json",
            f"{tile}: 406 Content-Type is not application/problem+json",
        )
        require(
            "no-store" in header_tokens(unsupported, "Cache-Control"),
            f"{tile}: 406 is cacheable",
        )
        require(
            "no-transform" in header_tokens(unsupported, "Cache-Control"),
            f"{tile}: 406 permits intermediary transformation",
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
            problem.get("code") == "bluemap_required_content_encoding",
            f"{tile}: 406 JSON has the wrong problem code",
        )
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
            {"private", "no-store", "no-transform"}.issubset(player_cache),
            f"{player}: player positions are not private, no-store, no-transform",
        )
        require(
            "no-transform" in player_cache,
            f"{player}: player response permits transformations",
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
        marker_cache = header_tokens(marker_response, "Cache-Control")
        require(
            {"no-cache", "no-transform"}.issubset(marker_cache),
            f"{marker}: marker data is not revalidated",
        )
        require(
            "no-transform" in marker_cache,
            f"{marker}: marker response permits transformations",
        )

    for field in ("settings", "textures", "assets"):
        if not manifest[field]:
            continue
        path = str(manifest[field][0])
        data_response = fetch(
            base_url,
            path,
            {**base_headers, "Accept-Encoding": stored_encoding},
        )
        require(
            data_response.status == 200,
            f"{path}: expected 200, got {data_response.status}",
        )
        check_body(data_response, manifest["expected"][path], path)
        data_cache = header_tokens(data_response, "Cache-Control")
        require(
            "no-cache" in data_cache,
            f"{path}: {field} data is not revalidated",
        )
        require(
            "no-transform" in data_cache,
            f"{path}: {field} response permits transformations",
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
    require(
        "no-transform" in header_tokens(missing_response, "Cache-Control"),
        f"{missing}: missing tile result permits intermediary transformation",
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
    require(
        {"no-store", "no-transform"}.issubset(
            header_tokens(post, "Cache-Control")
        ),
        f"{tile}: 405 is cacheable or permits transformation",
    )

    map_root = tile.split("/tiles/", 1)[0]
    unknown = f"{map_root}/not-a-real-map-data-route"
    not_found = fetch(
        base_url,
        unknown,
        {**base_headers, "Accept-Encoding": stored_encoding},
    )
    require(
        not_found.status == 404,
        f"{unknown}: expected 404, got {not_found.status}",
    )
    require(
        {"no-store", "no-transform"}.issubset(
            header_tokens(not_found, "Cache-Control")
        ),
        f"{unknown}: 404 is cacheable or permits transformation",
    )


def check_legacy_contract(
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

    for field in ("settings", "textures", "assets", "players", "markers"):
        if not manifest[field]:
            continue
        path = str(manifest[field][0])
        data_response = fetch(
            base_url,
            path,
            {**base_headers, "Accept-Encoding": stored_encoding},
        )
        require(
            data_response.status == 200,
            f"{path}: expected 200, got {data_response.status}",
        )
        check_body(data_response, manifest["expected"][path], path)

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


def main() -> int:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    headers = {"User-Agent": args.user_agent}
    try:
        validate_manifest(manifest)
        if args.mode == "enhanced":
            check_enhanced_contract(
                args.base_url,
                manifest,
                args.stored_encoding,
                headers,
            )
        else:
            check_legacy_contract(
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
