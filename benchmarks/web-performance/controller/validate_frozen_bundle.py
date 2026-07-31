#!/usr/bin/env python3
"""Fail closed when a controller image contains a stale or legacy formal bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


class ValidationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"could not load {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    return value


def require_file(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValidationError(f"required regular file is missing: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()

    repository = args.repository.resolve()
    revision = args.revision
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValidationError("revision must be a full lowercase Git SHA")

    benchmark = repository / "benchmarks" / "web-performance"
    controller = benchmark / "controller"
    orchestrator_dir = controller / "formal"
    analysis_dir = controller / "formal"
    frozen_dir = controller / "frozen"
    inputs_dir = frozen_dir / "formal-inputs"
    files = {
        "orchestrate.py": orchestrator_dir / "orchestrate.py",
        "freeze.py": orchestrator_dir / "freeze.py",
        "analyze.py": analysis_dir / "analyze.py",
        "controller-lock.json": frozen_dir / "controller-lock.json",
        "matrix.json": inputs_dir / "matrix.json",
        "schedule.json": inputs_dir / "schedule.json",
        "runtime-admission-identities.json": (
            inputs_dir / "runtime-admission-identities.json"
        ),
        "bundle-manifest.json": inputs_dir / "bundle-manifest.json",
        "manifest.json": frozen_dir / "manifest.json",
    }
    for path in files.values():
        require_file(path)

    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != revision:
        raise ValidationError(f"checkout {head} differs from requested {revision}")

    matrix = json_object(files["matrix.json"])
    schedule = json_object(files["schedule.json"])
    lock = json_object(files["controller-lock.json"])
    if matrix.get("benchmarkGitRevision") != revision:
        raise ValidationError("matrix does not target the controller image revision")
    if schedule.get("benchmarkGitRevision") != revision:
        raise ValidationError("schedule does not target the controller image revision")
    if lock.get("requiredRevision") != revision:
        raise ValidationError("controller lock is bound to a different revision")

    expected_paths = {
        "orchestrate.py": files["orchestrate.py"],
        "freeze.py": files["freeze.py"],
        "analyze.py": files["analyze.py"],
    }
    controllers = lock.get("controllers")
    if not isinstance(controllers, list) or len(controllers) != 3:
        raise ValidationError("controller lock must bind exactly three controllers")
    seen: set[str] = set()
    for item in controllers:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValidationError("controller lock entry is malformed")
        name = item.get("path")
        if name not in expected_paths or name in seen:
            raise ValidationError(f"unexpected controller lock path: {name!r}")
        if item.get("sha256") != sha256(expected_paths[name]):
            raise ValidationError(f"controller lock digest differs for {name}")
        seen.add(name)
    if seen != set(expected_paths):
        raise ValidationError("controller lock is incomplete")

    analyzer = files["analyze.py"].read_text(encoding="utf-8")
    legacy_literals = ("loadgenPodIdentity", '"loadgenPod"')
    present_legacy = [value for value in legacy_literals if value in analyzer]
    if present_legacy:
        raise ValidationError(
            "analyzer still requires legacy Kubernetes load-generator identity: "
            + ", ".join(present_legacy)
        )
    required_analyzer_literals = (
        "runpod-ssh",
        "loadGeneratorIdentity",
        "loadGeneratorCapacity",
    )
    missing = [value for value in required_analyzer_literals if value not in analyzer]
    if missing:
        raise ValidationError(
            "analyzer does not declare frozen RunPod identity/capacity evidence: "
            + ", ".join(missing)
        )

    runner = benchmark / "tools" / "run_origin_case.sh"
    require_file(runner)
    runner_text = runner.read_text(encoding="utf-8")
    for literal in (
        "--load-generator-backend",
        "--traffic-base-url",
        "--traffic-service",
        "--traffic-service-port",
        "--formal-run-id",
        "--require-edge-bypass",
    ):
        if literal not in runner_text:
            raise ValidationError(f"runner lacks required RunPod control {literal}")

    print(
        json.dumps(
            {
                "valid": True,
                "revision": revision,
                "matrixSha256": sha256(files["matrix.json"]),
                "scheduleSha256": sha256(files["schedule.json"]),
                "manifestSha256": sha256(files["manifest.json"]),
                "analyzerSha256": sha256(files["analyze.py"]),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError, ValidationError) as error:
        raise SystemExit(f"FORMAL CONTROLLER IMAGE REFUSED: {error}") from error
