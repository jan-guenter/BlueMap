#!/usr/bin/env python3
"""Generate and validate a seeded, balanced benchmark execution schedule."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from runtime_identity import (
    validate_digest,
    validate_expected_images,
    validate_git_revision,
)


ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
DURATION = re.compile(r"^([1-9][0-9]*)(ms|s|m|h)$")
PROFILES = {
    "static",
    "hot-tile",
    "random-tiles",
    "large-tile",
    "settings",
    "textures",
    "large-object",
    "missing-tile",
    "conditional",
    "live-viewers",
    "map-data-mixed",
    "browser-mixed",
}
COMPLETION_PROGRESS_CONTROL = {
    "windowSeconds": 5,
    "startupAllowanceSeconds": 5,
    "minimumCompletionFraction": 0.1,
    "startTimeToleranceSeconds": 0.1,
}


def duration_seconds(value: str) -> float:
    match = DURATION.fullmatch(value)
    if match is None:
        raise ValueError("invalid k6 duration")
    return int(match.group(1)) * {
        "ms": 0.001,
        "s": 1.0,
        "m": 60.0,
        "h": 3600.0,
    }[match.group(2)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("matrix", type=Path)
    generate_parser.add_argument("output", type=Path)
    generate_parser.add_argument("--seed")
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("matrix", type=Path)
    validate_parser.add_argument("schedule", type=Path)
    entry_parser = subparsers.add_parser("validate-entry")
    entry_parser.add_argument("matrix", type=Path)
    entry_parser.add_argument("schedule", type=Path)
    entry_parser.add_argument("entry_id")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def matrix_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_matrix(matrix: dict[str, Any]) -> None:
    if matrix.get("formatVersion") != 4:
        raise ValueError("matrix formatVersion must be 4")
    if not isinstance(matrix.get("repetitions"), int) or matrix["repetitions"] < 1:
        raise ValueError("matrix repetitions must be a positive integer")
    validate_git_revision(
        matrix.get("benchmarkGitRevision"),
        "matrix benchmarkGitRevision",
    )
    for key in ("scheduleSeed", "traceSeed"):
        if not isinstance(matrix.get(key), str) or not matrix[key]:
            raise ValueError(f"matrix {key} must be a non-empty string")
    validate_digest(
        matrix.get("manifestSha256"),
        "matrix manifestSha256",
        prefix=False,
    )
    if (
        not isinstance(matrix.get("mapIds"), list)
        or not matrix["mapIds"]
        or matrix["mapIds"] != sorted(set(matrix["mapIds"]))
    ):
        raise ValueError("matrix mapIds must be a sorted unique non-empty array")
    controls = matrix.get("controls")
    if not isinstance(controls, dict):
        raise ValueError("matrix controls must be an object")
    for key in ("warmupDuration", "measurementDuration"):
        if not isinstance(controls.get(key), str) or re.fullmatch(
            r"[1-9][0-9]*(?:ms|s|m|h)",
            controls[key],
        ) is None:
            raise ValueError(f"matrix controls.{key} is invalid")
    for key in (
        "cooldownSeconds",
        "preAllocatedVUs",
        "maxVUs",
    ):
        if not isinstance(controls.get(key), int) or controls[key] < 1:
            raise ValueError(f"matrix controls.{key} must be positive")
    ratio = controls.get("minimumAchievedRateRatio")
    if not isinstance(ratio, (int, float)) or not 0 < ratio <= 1:
        raise ValueError("matrix minimumAchievedRateRatio must be in (0, 1]")
    progress = controls.get("completionProgress")
    if not isinstance(progress, dict) or set(progress) != {
        "windowSeconds",
        "startupAllowanceSeconds",
        "minimumCompletionFraction",
        "startTimeToleranceSeconds",
    }:
        raise ValueError("matrix completionProgress control is malformed")
    for key in (
        "windowSeconds",
        "startTimeToleranceSeconds",
    ):
        value = progress.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            raise ValueError(f"matrix completionProgress.{key} must be positive")
    allowance = progress.get("startupAllowanceSeconds")
    if (
        isinstance(allowance, bool)
        or not isinstance(allowance, (int, float))
        or allowance < 0
    ):
        raise ValueError(
            "matrix completionProgress.startupAllowanceSeconds must be nonnegative"
        )
    fraction = progress.get("minimumCompletionFraction")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not 0 < fraction <= 1
    ):
        raise ValueError(
            "matrix completionProgress.minimumCompletionFraction must be in (0, 1]"
        )
    if progress != COMPLETION_PROGRESS_CONTROL:
        raise ValueError("matrix completionProgress differs from the fixed contract")
    for key in ("warmupDuration", "measurementDuration"):
        if duration_seconds(controls[key]) < allowance + progress["windowSeconds"]:
            raise ValueError(
                f"matrix controls.{key} has no full completion progress window"
            )

    variants = matrix.get("variants")
    cases = matrix.get("cases")
    if not isinstance(variants, list) or len(variants) < 2:
        raise ValueError("matrix must define at least two variants")
    if not isinstance(cases, list) or not cases:
        raise ValueError("matrix must define at least one case")

    variant_ids = []
    for variant in variants:
        if not isinstance(variant, dict) or not ID.fullmatch(variant.get("id", "")):
            raise ValueError("every variant needs a valid unique id")
        if variant.get("contractMode") not in {"enhanced", "legacy"}:
            raise ValueError("variant contractMode must be enhanced or legacy")
        if variant.get("implementation") not in {"php", "java", "rust"}:
            raise ValueError("variant implementation must be php, java, or rust")
        if variant.get("storageType") not in {"sql", "file"}:
            raise ValueError("variant storageType must be sql or file")
        if variant.get("databaseBackend") not in {
            "postgresql",
            "mariadb",
            "none",
        }:
            raise ValueError("variant databaseBackend is invalid")
        if (
            variant["storageType"] == "file"
            and variant["databaseBackend"] != "none"
        ) or (
            variant["storageType"] == "sql"
            and variant["databaseBackend"] == "none"
        ):
            raise ValueError("variant storageType/databaseBackend are inconsistent")
        if (
            not isinstance(variant.get("replicaCount"), int)
            or variant["replicaCount"] < 1
        ):
            raise ValueError("variant replicaCount must be positive")
        validate_expected_images(variant.get("expectedImages"))
        validate_digest(
            variant.get("expectedSanitizedConfigSha256"),
            f"variant {variant['id']} expectedSanitizedConfigSha256",
            prefix=False,
        )
        validate_digest(
            variant.get("expectedSanitizedRuntimeSpecSha256"),
            f"variant {variant['id']} expectedSanitizedRuntimeSpecSha256",
            prefix=False,
        )
        variant_ids.append(variant["id"])
    if len(set(variant_ids)) != len(variant_ids):
        raise ValueError("variant ids must be unique")

    case_ids = []
    for case in cases:
        if not isinstance(case, dict) or not ID.fullmatch(case.get("id", "")):
            raise ValueError("every case needs a valid unique id")
        case_ids.append(case["id"])
        if case.get("profile") not in PROFILES:
            raise ValueError(f"case {case['id']} has an invalid profile")
        for key in ("rate", "viewers", "markerIntervalSeconds"):
            if not isinstance(case.get(key), int) or case[key] < 1:
                raise ValueError(f"case {case['id']} {key} must be positive")
        for key in ("latencyP95Milliseconds", "latencyP99Milliseconds"):
            if not isinstance(case.get(key), (int, float)) or case[key] <= 0:
                raise ValueError(f"case {case['id']} {key} must be positive")
        if case["latencyP99Milliseconds"] < case["latencyP95Milliseconds"]:
            raise ValueError(f"case {case['id']} p99 gate must be at least p95")
        if case.get("acceptEncoding") not in {"gzip", "zstd", "deflate", "identity"}:
            raise ValueError(f"case {case['id']} has invalid acceptEncoding")
        if case.get("storedEncoding") not in {"gzip", "zstd", "deflate", "identity"}:
            raise ValueError(f"case {case['id']} has invalid storedEncoding")
        if case.get("overloadPolicy") not in {"forbid", "allow-explicit"}:
            raise ValueError(f"case {case['id']} has invalid overloadPolicy")
        selected = case.get("variants")
        if not isinstance(selected, list) or len(selected) < 2:
            raise ValueError(f"case {case['id']} needs at least two variants")
        if len(set(selected)) != len(selected) or not set(selected) <= set(variant_ids):
            raise ValueError(f"case {case['id']} has invalid or duplicate variants")
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("case ids must be unique")


def seeded_shuffle(values: list[str], seed: str) -> list[str]:
    return sorted(
        values,
        key=lambda value: (
            hashlib.sha256(f"{seed}\0{value}".encode()).digest(),
            value,
        ),
    )


def build_schedule(
    matrix: dict[str, Any],
    matrix_digest: str,
    seed: str | None = None,
) -> dict[str, Any]:
    validate_matrix(matrix)
    schedule_seed = seed or matrix["scheduleSeed"]
    variants = {variant["id"]: variant for variant in matrix["variants"]}
    cases = {case["id"]: case for case in matrix["cases"]}
    entries = []
    sequence = 0
    for block in range(1, matrix["repetitions"] + 1):
        case_order = seeded_shuffle(
            list(cases),
            f"{schedule_seed}\0block\0{block}\0cases",
        )
        for case_id in case_order:
            case = cases[case_id]
            base_variant_order = seeded_shuffle(
                case["variants"],
                f"{schedule_seed}\0case\0{case_id}\0base-variants",
            )
            rotation = (block - 1) % len(base_variant_order)
            variant_order = (
                base_variant_order[rotation:] + base_variant_order[:rotation]
            )
            for ordinal, variant_id in enumerate(variant_order, start=1):
                sequence += 1
                runner_case_id = f"{case_id}-{variant_id}-b{block}"
                if len(runner_case_id) > 63:
                    raise ValueError(f"generated runner case id is too long: {runner_case_id}")
                entries.append(
                    {
                        "entryId": f"{case_id}/{variant_id}/block-{block}",
                        "sequence": sequence,
                        "block": block,
                        "ordinalWithinCase": ordinal,
                        "matrixCaseId": case_id,
                        "variantId": variant_id,
                        "runnerCaseId": runner_case_id,
                        "profile": case["profile"],
                        "rate": case["rate"],
                        "viewers": case["viewers"],
                        "markerIntervalSeconds": case["markerIntervalSeconds"],
                        "contractMode": variants[variant_id]["contractMode"],
                        "implementation": variants[variant_id]["implementation"],
                        "storageType": variants[variant_id]["storageType"],
                        "databaseBackend": variants[variant_id][
                            "databaseBackend"
                        ],
                        "replicaCount": variants[variant_id]["replicaCount"],
                        "benchmarkGitRevision": matrix["benchmarkGitRevision"],
                        "expectedImages": variants[variant_id]["expectedImages"],
                        "expectedSanitizedConfigSha256": variants[variant_id][
                            "expectedSanitizedConfigSha256"
                        ],
                        "expectedSanitizedRuntimeSpecSha256": variants[variant_id][
                            "expectedSanitizedRuntimeSpecSha256"
                        ],
                        "acceptEncoding": case["acceptEncoding"],
                        "storedEncoding": case["storedEncoding"],
                        "overloadPolicy": case["overloadPolicy"],
                        "traceSeed": matrix["traceSeed"],
                        "manifestSha256": matrix["manifestSha256"],
                        "mapIds": matrix["mapIds"],
                        "warmupDuration": matrix["controls"]["warmupDuration"],
                        "measurementDuration": matrix["controls"][
                            "measurementDuration"
                        ],
                        "cooldownSeconds": matrix["controls"]["cooldownSeconds"],
                        "minimumAchievedRateRatio": matrix["controls"][
                            "minimumAchievedRateRatio"
                        ],
                        "preAllocatedVUs": matrix["controls"]["preAllocatedVUs"],
                        "maxVUs": matrix["controls"]["maxVUs"],
                        "completionProgress": matrix["controls"][
                            "completionProgress"
                        ],
                        "latencyP95Milliseconds": case[
                            "latencyP95Milliseconds"
                        ],
                        "latencyP99Milliseconds": case[
                            "latencyP99Milliseconds"
                        ],
                    }
                )
    return {
        "formatVersion": 4,
        "matrixSha256": matrix_digest,
        "scheduleSeed": schedule_seed,
        "traceSeed": matrix["traceSeed"],
        "benchmarkGitRevision": matrix["benchmarkGitRevision"],
        "repetitions": matrix["repetitions"],
        "entries": entries,
    }


def validate_schedule(
    matrix: dict[str, Any],
    matrix_digest: str,
    schedule: dict[str, Any],
) -> None:
    expected = build_schedule(matrix, matrix_digest, schedule.get("scheduleSeed"))
    if schedule != expected:
        raise ValueError("schedule does not exactly match the seeded matrix expansion")

    counts = Counter(
        (entry["block"], entry["matrixCaseId"], entry["variantId"])
        for entry in schedule["entries"]
    )
    if any(count != 1 for count in counts.values()):
        raise ValueError("schedule is not balanced")
    expected_count = sum(
        len(case["variants"]) for case in matrix["cases"]
    ) * matrix["repetitions"]
    if len(counts) != expected_count:
        raise ValueError("schedule omits a case/variant/block combination")

    cases = {case["id"]: case for case in matrix["cases"]}
    for case_id, case in cases.items():
        for variant_id in case["variants"]:
            positions = Counter(
                entry["ordinalWithinCase"]
                for entry in schedule["entries"]
                if entry["matrixCaseId"] == case_id
                and entry["variantId"] == variant_id
            )
            counts_by_position = [
                positions.get(position, 0)
                for position in range(1, len(case["variants"]) + 1)
            ]
            if max(counts_by_position) - min(counts_by_position) > 1:
                raise ValueError("schedule is not position-balanced")


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
        matrix = load_json(args.matrix)
        digest = matrix_sha256(args.matrix)
        if args.command == "generate":
            atomic_json(args.output, build_schedule(matrix, digest, args.seed))
            return 0
        schedule = load_json(args.schedule)
        validate_schedule(matrix, digest, schedule)
        if args.command == "validate-entry":
            matches = [
                entry
                for entry in schedule["entries"]
                if entry["entryId"] == args.entry_id
            ]
            if len(matches) != 1:
                raise ValueError("schedule entry id does not resolve exactly once")
            json.dump(matches[0], sys.stdout, sort_keys=True)
            sys.stdout.write("\n")
        else:
            print(
                json.dumps(
                    {
                        "matrixSha256": digest,
                        "entries": len(schedule["entries"]),
                        "repetitions": schedule["repetitions"],
                        "balanced": True,
                        "positionBalanced": True,
                    },
                    sort_keys=True,
                )
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"SCHEDULE FAILURE: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
