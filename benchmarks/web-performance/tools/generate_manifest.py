#!/usr/bin/env python3
"""Generate a non-sensitive k6 request manifest from a BlueMap webroot."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import zlib
from pathlib import Path


COMPRESSION_SUFFIXES = (".gz", ".zst", ".deflate", ".lz4")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("webroot", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file. Defaults to stdout.",
    )
    return parser.parse_args()


def url_path(path: Path, webroot: Path, strip_compression: bool = False) -> str:
    relative = path.relative_to(webroot).as_posix()
    if strip_compression:
        for suffix in COMPRESSION_SUFFIXES:
            if relative.endswith(suffix):
                relative = relative[: -len(suffix)]
                break
    return f"/{relative}"


def regular_files(root: Path):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def source_encoding(path: Path) -> str:
    if path.name.endswith(".gz"):
        return "gzip"
    if path.name.endswith(".zst"):
        return "zstd"
    if path.name.endswith(".deflate"):
        return "deflate"
    if path.name.endswith(".lz4"):
        return "lz4"
    return "identity"


def decoded_bytes(path: Path) -> bytes:
    encoding = source_encoding(path)
    if encoding == "gzip":
        with gzip.open(path, "rb") as stream:
            return stream.read()
    if encoding == "zstd":
        import zstandard

        with path.open("rb") as stream:
            return zstandard.ZstdDecompressor().stream_reader(stream).read()
    if encoding == "deflate":
        return zlib.decompress(path.read_bytes())
    if encoding == "lz4":
        raise ValueError(f"Cannot create a decoded golden hash for Java LZ4 data: {path}")
    return path.read_bytes()


def expectation(path: Path) -> dict[str, object]:
    decoded = decoded_bytes(path)
    return {
        "decodedSha256": hashlib.sha256(decoded).hexdigest(),
        "decodedSize": len(decoded),
        "sourceEncoding": source_encoding(path),
        "sourceSize": path.stat().st_size,
    }


def generate(webroot: Path) -> dict[str, object]:
    webroot = webroot.resolve()
    maps_root = webroot / "maps"
    if not (webroot / "index.html").is_file():
        raise ValueError(f"{webroot} does not look like a BlueMap webroot")
    if not maps_root.is_dir():
        raise ValueError(f"{maps_root} is missing")

    static: list[str] = ["/"]
    expected: dict[str, dict[str, object]] = {
        "/": expectation(webroot / "index.html"),
    }
    for path in regular_files(webroot):
        relative = path.relative_to(webroot)
        if relative.parts[0] == "maps":
            continue
        if path.name == "sql.php":
            continue
        route = url_path(path, webroot)
        static.append(route)
        expected[route] = expectation(path)

    tiles: list[str] = []
    tile_sizes: list[tuple[int, str]] = []
    metadata: list[str] = []
    assets: list[str] = []
    players: list[str] = []
    markers: list[str] = []

    map_directories = sorted(path for path in maps_root.iterdir() if path.is_dir())
    for map_root in map_directories:
        for path in regular_files(map_root / "tiles"):
            route = url_path(path, webroot, strip_compression=True)
            tiles.append(route)
            tile_sizes.append((path.stat().st_size, route))
            expected[route] = expectation(path)

        for name in ("settings.json", "textures.json", "textures.json.gz", "textures.json.zst",
                     "textures.json.deflate", "textures.json.lz4"):
            path = map_root / name
            if path.is_file():
                route = url_path(path, webroot, strip_compression=True)
                if route not in metadata:
                    metadata.append(route)
                    expected[route] = expectation(path)

        for path in regular_files(map_root / "assets"):
            route = url_path(path, webroot)
            assets.append(route)
            expected[route] = expectation(path)

        player_path = map_root / "live" / "players.json"
        if player_path.is_file():
            route = url_path(player_path, webroot)
            players.append(route)
            expected[route] = expectation(player_path)

        marker_path = map_root / "live" / "markers.json"
        if marker_path.is_file():
            route = url_path(marker_path, webroot)
            markers.append(route)
            expected[route] = expectation(marker_path)

    if not tiles:
        raise ValueError("No map tiles were found")

    tile_sizes.sort()
    median_tile = tile_sizes[len(tile_sizes) // 2][1]
    largest_tile = tile_sizes[-1][1]

    return {
        "static": sorted(set(static)),
        "tiles": sorted(set(tiles)),
        "metadata": sorted(set(metadata)),
        "assets": sorted(set(assets)),
        "players": sorted(set(players)),
        "markers": sorted(set(markers)),
        "hotTile": median_tile,
        "largeTile": largest_tile,
        "missingTile": (
            f"/maps/{map_directories[0].name}"
            "/tiles/0/x2147483647/z2147483647.prbm"
        ),
        "expected": expected,
        "counts": {
            "static": len(set(static)),
            "tiles": len(set(tiles)),
            "metadata": len(set(metadata)),
            "assets": len(set(assets)),
            "players": len(set(players)),
            "markers": len(set(markers)),
        },
    }


def main() -> None:
    args = parse_args()
    manifest = json.dumps(generate(args.webroot), indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{manifest}\n", encoding="utf-8")
    else:
        print(manifest)


if __name__ == "__main__":
    main()
