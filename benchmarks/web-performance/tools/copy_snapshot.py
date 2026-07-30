#!/usr/bin/env python3
"""Copy and verify a content-addressed BlueMap benchmark snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


FORMAT_VERSION = 1
ALLOWED_DESTINATION_ENTRIES = {"lost+found"}


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    sha256: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_webroot", type=Path)
    parser.add_argument(
        "destination_volume",
        type=Path,
        help="blank PVC mount; /bluemap/web and SNAPSHOT.json are created below it",
    )
    return parser.parse_args()


def normalized_relative_path(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    value = relative.as_posix()
    if value in {"", "."} or value.startswith("../") or "\0" in value:
        raise ValueError(f"Unsafe snapshot path: {relative}")
    return value


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def inventory(root: Path) -> list[FileRecord]:
    records: list[FileRecord] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in [*directory_names, *file_names]:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise ValueError(f"Snapshot source contains a symlink: {candidate}")
        for name in file_names:
            candidate = directory_path / name
            mode = candidate.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"Snapshot source contains a non-regular file: {candidate}")
            digest, size = sha256_file(candidate)
            records.append(
                FileRecord(
                    normalized_relative_path(candidate, root),
                    size,
                    digest,
                )
            )
    records.sort(key=lambda record: record.path.encode("utf-8"))
    return records


def tree_digest(records: list[FileRecord]) -> str:
    digest = hashlib.sha256(b"bluemap-benchmark-snapshot-v1\0")
    for record in records:
        path_bytes = record.path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(record.size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(record.sha256))
    return digest.hexdigest()


def ensure_blank_destination(destination_volume: Path) -> None:
    if destination_volume.is_symlink() or not destination_volume.is_dir():
        raise ValueError("Destination volume must be a real directory")
    unexpected = sorted(
        entry.name
        for entry in destination_volume.iterdir()
        if entry.name not in ALLOWED_DESTINATION_ENTRIES
    )
    if unexpected:
        raise ValueError(
            "Destination volume is not blank; unexpected entries: "
            + ", ".join(unexpected)
        )


def copy_tree(source: Path, destination: Path, records: list[FileRecord]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for record in records:
        source_file = source / Path(record.path)
        destination_file = destination / Path(record.path)
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, destination_file)


def make_tree_read_only(root: Path) -> None:
    for directory, directory_names, file_names in os.walk(root):
        directory_path = Path(directory)
        for name in file_names:
            (directory_path / name).chmod(0o444)
        for name in directory_names:
            (directory_path / name).chmod(0o555)
    root.chmod(0o555)


def atomic_receipt(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_name, 0o444)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def create_snapshot(source_webroot: Path, destination_volume: Path) -> dict[str, object]:
    source_webroot = source_webroot.resolve(strict=True)
    if not source_webroot.is_dir():
        raise ValueError("Source webroot must be a directory")
    ensure_blank_destination(destination_volume)

    source_records = inventory(source_webroot)
    if not source_records:
        raise ValueError("Source webroot contains no regular files")
    source_digest = tree_digest(source_records)

    destination_webroot = destination_volume / "bluemap" / "web"
    copy_tree(source_webroot, destination_webroot, source_records)
    destination_records = inventory(destination_webroot)
    destination_digest = tree_digest(destination_records)
    if destination_records != source_records or destination_digest != source_digest:
        raise RuntimeError("Destination verification did not match the source inventory")

    receipt: dict[str, object] = {
        "formatVersion": FORMAT_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sourceRelativeRoot": "bluemap/web",
        "treeSha256": source_digest,
        "fileCount": len(source_records),
        "totalBytes": sum(record.size for record in source_records),
        "files": [
            {"path": record.path, "size": record.size, "sha256": record.sha256}
            for record in source_records
        ],
    }
    make_tree_read_only(destination_webroot)
    atomic_receipt(destination_volume / "SNAPSHOT.json", receipt)
    return receipt


def main() -> None:
    args = parse_args()
    receipt = create_snapshot(args.source_webroot, args.destination_volume)
    print(
        "SNAPSHOT_VERIFIED "
        f"treeSha256={receipt['treeSha256']} "
        f"files={receipt['fileCount']} "
        f"bytes={receipt['totalBytes']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
