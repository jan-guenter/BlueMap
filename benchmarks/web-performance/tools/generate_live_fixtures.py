#!/usr/bin/env python3
"""Generate deterministic, representative BlueMap live-data fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--players", type=int, default=32)
    parser.add_argument("--markers", type=int, default=64)
    return parser.parse_args()


def players_fixture(count: int) -> dict[str, object]:
    if count < 8:
        raise ValueError("players count must be at least eight")
    players = []
    for index in range(count):
        players.append(
            {
                "uuid": f"00000000-0000-4000-8000-{index + 1:012x}",
                "name": f"BenchmarkPlayer{index + 1:02d}",
                "foreign": index % 11 == 0,
                "position": {
                    "x": -384.5 + ((index * 47) % 769),
                    "y": 48 + ((index * 7) % 96),
                    "z": -384.5 + ((index * 83) % 769),
                },
                "rotation": {
                    "pitch": -45 + ((index * 13) % 91),
                    "yaw": (index * 37) % 360,
                    "roll": 0,
                },
            }
        )
    return {"players": players}


def markers_fixture(count: int) -> dict[str, object]:
    if count < 8:
        raise ValueError("markers count must be at least eight")
    markers: dict[str, object] = {}
    for index in range(count):
        markers[f"benchmark-poi-{index + 1:03d}"] = {
            "type": "poi",
            "label": f"Benchmark point {index + 1}",
            "position": {
                "x": -512 + ((index * 61) % 1025),
                "y": 52 + ((index * 5) % 80),
                "z": -512 + ((index * 97) % 1025),
            },
            "sorting": index,
            "listed": True,
            "minDistance": 0,
            "maxDistance": 10000000,
            "classes": ["benchmark-marker"],
            "detail": f"Deterministic benchmark marker {index + 1}",
            "icon": "assets/poi.svg",
            "anchor": {"x": 25, "y": 45},
        }
    return {
        "benchmark-live-data": {
            "label": "Benchmark live data",
            "toggleable": True,
            "defaultHidden": False,
            "sorting": 0,
            "markers": markers,
        }
    }


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def write_fixture(path: Path, payload: bytes) -> dict[str, object]:
    path.write_bytes(payload)
    return {
        "file": path.name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def generate(output_directory: Path, players: int, markers: int) -> dict[str, object]:
    output_directory.mkdir(parents=True, exist_ok=True)
    if any(output_directory.iterdir()):
        raise ValueError("output directory must be blank")

    summary = {
        "players": {
            "count": players,
            **write_fixture(
                output_directory / "players.json",
                canonical_bytes(players_fixture(players)),
            ),
        },
        "markers": {
            "count": markers,
            **write_fixture(
                output_directory / "markers.json",
                canonical_bytes(markers_fixture(markers)),
            ),
        },
    }
    (output_directory / "SHA256SUMS").write_text(
        f"{summary['markers']['sha256']}  markers.json\n"
        f"{summary['players']['sha256']}  players.json\n",
        encoding="ascii",
    )
    return summary


def main() -> None:
    args = parse_args()
    summary = generate(args.output_directory, args.players, args.markers)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
