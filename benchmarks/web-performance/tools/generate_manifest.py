#!/usr/bin/env python3
"""Generate a non-sensitive k6 request manifest from a BlueMap webroot."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import zlib
from pathlib import Path


COMPRESSION_SUFFIXES = (".gz", ".zst", ".deflate", ".lz4")
MAP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("webroot", type=Path)
    parser.add_argument(
        "--map-id",
        action="append",
        dest="map_ids",
        help=(
            "Include exactly this map id. Repeat for more than one map. "
            "If omitted, all maps are included."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file. Defaults to stdout.",
    )
    parser.add_argument(
        "--players-fixture",
        type=Path,
        help="override live players expectations without modifying the webroot",
    )
    parser.add_argument(
        "--markers-fixture",
        type=Path,
        help="override live marker expectations without modifying the webroot",
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


def identity_expectation(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "decodedSha256": hashlib.sha256(payload).hexdigest(),
        "decodedSize": len(payload),
        "sourceEncoding": "identity",
        "sourceSize": len(payload),
    }


def generate(
    webroot: Path,
    requested_map_ids: list[str] | None = None,
    players_fixture: Path | None = None,
    markers_fixture: Path | None = None,
) -> dict[str, object]:
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
    settings: list[str] = []
    textures: list[str] = []
    object_sizes: list[tuple[int, str]] = []
    assets: list[str] = []
    players: list[str] = []
    markers: list[str] = []

    available_map_directories = {
        path.name: path
        for path in maps_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    }
    if requested_map_ids:
        requested = sorted(set(requested_map_ids))
        invalid = [
            map_id for map_id in requested if MAP_ID_PATTERN.fullmatch(map_id) is None
        ]
        if invalid:
            raise ValueError(f"Invalid map ids: {', '.join(invalid)}")
        missing = [
            map_id for map_id in requested if map_id not in available_map_directories
        ]
        if missing:
            raise ValueError(f"Requested map ids do not exist: {', '.join(missing)}")
        map_directories = [available_map_directories[map_id] for map_id in requested]
    else:
        map_directories = [
            available_map_directories[map_id]
            for map_id in sorted(available_map_directories)
        ]
    if not map_directories:
        raise ValueError("No map directories were selected")

    fixture_paths = {
        "players": players_fixture.resolve(strict=True) if players_fixture else None,
        "markers": markers_fixture.resolve(strict=True) if markers_fixture else None,
    }

    for map_root in map_directories:
        for path in regular_files(map_root / "tiles"):
            route = url_path(path, webroot, strip_compression=True)
            tiles.append(route)
            tile_sizes.append((path.stat().st_size, route))
            expected[route] = expectation(path)

        settings_path = map_root / "settings.json"
        if settings_path.is_file():
            route = url_path(settings_path, webroot)
            settings.append(route)
            object_sizes.append((settings_path.stat().st_size, route))
            expected[route] = expectation(settings_path)

        for name in (
            "textures.json",
            "textures.json.gz",
            "textures.json.zst",
            "textures.json.deflate",
            "textures.json.lz4",
        ):
            path = map_root / name
            if path.is_file():
                route = url_path(path, webroot, strip_compression=True)
                if route not in textures:
                    textures.append(route)
                    object_sizes.append((path.stat().st_size, route))
                    expected[route] = expectation(path)

        for path in regular_files(map_root / "assets"):
            route = url_path(path, webroot)
            assets.append(route)
            object_sizes.append((path.stat().st_size, route))
            expected[route] = expectation(path)

        player_path = map_root / "live" / "players.json"
        if player_path.is_file() or fixture_paths["players"] is not None:
            route = url_path(player_path, webroot)
            players.append(route)
            source = fixture_paths["players"] or player_path
            object_sizes.append((source.stat().st_size, route))
            expected[route] = (
                identity_expectation(source)
                if fixture_paths["players"] is not None
                else expectation(source)
            )

        marker_path = map_root / "live" / "markers.json"
        if marker_path.is_file() or fixture_paths["markers"] is not None:
            route = url_path(marker_path, webroot)
            markers.append(route)
            source = fixture_paths["markers"] or marker_path
            object_sizes.append((source.stat().st_size, route))
            expected[route] = (
                identity_expectation(source)
                if fixture_paths["markers"] is not None
                else expectation(source)
            )

    if not tiles:
        raise ValueError("No map tiles were found")

    tile_sizes.sort()
    median_tile = tile_sizes[len(tile_sizes) // 2][1]
    largest_tile = tile_sizes[-1][1]
    if not object_sizes:
        raise ValueError("No map-data objects were found")
    object_sizes.sort()
    largest_object = object_sizes[-1][1]

    return {
        "mapIds": [path.name for path in map_directories],
        "static": sorted(set(static)),
        "tiles": sorted(set(tiles)),
        "settings": sorted(set(settings)),
        "textures": sorted(set(textures)),
        "assets": sorted(set(assets)),
        "players": sorted(set(players)),
        "markers": sorted(set(markers)),
        "hotTile": median_tile,
        "largeTile": largest_tile,
        "largeObject": largest_object,
        "missingTile": (
            f"/maps/{map_directories[0].name}"
            "/tiles/0/x2147483647/z2147483647.prbm"
        ),
        "expected": expected,
        "counts": {
            "static": len(set(static)),
            "tiles": len(set(tiles)),
            "settings": len(set(settings)),
            "textures": len(set(textures)),
            "assets": len(set(assets)),
            "players": len(set(players)),
            "markers": len(set(markers)),
        },
        "fixtures": {
            kind: (
                {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
                if path is not None
                else None
            )
            for kind, path in fixture_paths.items()
        },
    }


def main() -> None:
    args = parse_args()
    manifest = json.dumps(
        generate(
            args.webroot,
            args.map_ids,
            args.players_fixture,
            args.markers_fixture,
        ),
        indent=2,
        sort_keys=True,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{manifest}\n", encoding="utf-8")
    else:
        print(manifest)


if __name__ == "__main__":
    main()
