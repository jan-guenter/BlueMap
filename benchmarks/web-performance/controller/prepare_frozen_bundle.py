#!/usr/bin/env python3
"""Initialize or publish a tracked, revision-bound formal controller bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


CONTROLLER_DIR = Path(__file__).resolve().parent
FORMAL_DIR = CONTROLLER_DIR / "formal"
FROZEN_DIR = CONTROLLER_DIR / "frozen"
BENCHMARK_ROOT = CONTROLLER_DIR.parent
REPOSITORY_ROOT = BENCHMARK_ROOT.parents[1]
ARTIFACT_ROOT = BENCHMARK_ROOT / "artifacts"
DEFAULT_MANIFEST = ARTIFACT_ROOT / "snapshot" / "manifest.json"
CONTROL_LOCK = FROZEN_DIR / "controller-lock.json"
CONTROLLERS = (
    ("freeze.py", FORMAL_DIR / "freeze.py"),
    ("orchestrate.py", FORMAL_DIR / "orchestrate.py"),
    ("analyze.py", FORMAL_DIR / "analyze.py"),
)


class BundleError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BundleError(f"Could not load {path}: {error}") from error
    if not isinstance(value, dict):
        raise BundleError(f"{path} must contain a JSON object")
    return value


def revision() -> str:
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "--verify", "HEAD^{commit}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", result) is None:
        raise BundleError("Git HEAD is not a full lowercase commit SHA")
    tracked = subprocess.run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "status",
            "--porcelain",
            "--untracked-files=no",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if tracked:
        raise BundleError(
            "Bundle initialization/publication requires a clean tracked commit"
        )
    return result


def expected_lock(git_revision: str) -> dict[str, Any]:
    for _, path in CONTROLLERS:
        if not path.is_file() or path.is_symlink():
            raise BundleError(f"Controller source is unavailable: {path}")
    return {
        "formatVersion": 1,
        "requiredRevision": git_revision,
        "controllers": [
            {"path": name, "sha256": sha256(path)} for name, path in CONTROLLERS
        ],
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def init_lock() -> None:
    git_revision = revision()
    if CONTROL_LOCK.exists():
        raise BundleError(f"Refusing to overwrite {CONTROL_LOCK}")
    atomic_json(CONTROL_LOCK, expected_lock(git_revision))
    print(CONTROL_LOCK)


def publish(source_root: Path, manifest_path: Path) -> None:
    git_revision = revision()
    if load_object(CONTROL_LOCK) != expected_lock(git_revision):
        raise BundleError("Controller lock does not bind the current clean commit")
    if source_root.is_symlink():
        raise BundleError("Freeze source must not be a symlink")
    source_root = source_root.resolve()
    try:
        relative_source = source_root.relative_to(ARTIFACT_ROOT.resolve())
    except ValueError as error:
        raise BundleError(
            f"Freeze source must be below the ignored artifact root {ARTIFACT_ROOT}"
        ) from error
    if (
        not relative_source.parts
        or source_root == (ARTIFACT_ROOT / "snapshot").resolve()
    ):
        raise BundleError(
            "Freeze source must be a dedicated fresh child directory, not the "
            "preserved default snapshot"
        )
    source_inputs = source_root / "formal-inputs"
    manifest_path = manifest_path.resolve()
    required_sources = {
        "matrix.json": source_inputs / "matrix.json",
        "schedule.json": source_inputs / "schedule.json",
        "runtime-admission-identities.json": (
            source_inputs / "runtime-admission-identities.json"
        ),
        "bundle-manifest.json": source_inputs / "bundle-manifest.json",
        "manifest.json": manifest_path,
    }
    for name, path in required_sources.items():
        if not path.is_file() or path.is_symlink():
            raise BundleError(f"Fresh freeze output is unavailable: {name}: {path}")
    for name in (
        "matrix.json",
        "schedule.json",
        "runtime-admission-identities.json",
        "bundle-manifest.json",
    ):
        value = load_object(required_sources[name])
        if value.get("benchmarkGitRevision") != git_revision:
            raise BundleError(f"{name} targets another benchmark revision")

    subprocess.run(
        [
            sys.executable,
            str(FORMAL_DIR / "orchestrate.py"),
            "validate",
            "--documents-only",
            "--matrix",
            str(required_sources["matrix.json"]),
            "--schedule",
            str(required_sources["schedule.json"]),
            "--runtime-admission-identities",
            str(required_sources["runtime-admission-identities.json"]),
            "--bundle-manifest",
            str(required_sources["bundle-manifest.json"]),
            "--manifest",
            str(required_sources["manifest.json"]),
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
    )

    destinations = {
        name: (
            FROZEN_DIR / name
            if name == "manifest.json"
            else FROZEN_DIR / "formal-inputs" / name
        )
        for name in required_sources
    }
    for path in destinations.values():
        if path.exists():
            raise BundleError(f"Refusing to overwrite published input: {path}")
    for name, source in required_sources.items():
        destination = destinations[name]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if sha256(source) != sha256(destination):
            raise BundleError(f"Published file changed while copying: {name}")

    subprocess.run(
        [
            sys.executable,
            str(FORMAL_DIR / "orchestrate.py"),
            "validate",
            "--documents-only",
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
    )
    print(FROZEN_DIR)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-lock")
    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument(
        "--source-root",
        required=True,
        type=Path,
        help="dedicated fresh freeze output containing formal-inputs/",
    )
    publish_parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="exact snapshot manifest used by the fresh freeze",
    )
    args = parser.parse_args()
    if args.command == "init-lock":
        init_lock()
    else:
        publish(args.source_root, args.manifest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BundleError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"FROZEN BUNDLE REFUSED: {error}") from error
