#!/usr/bin/env python3
"""Fail closed when accepted k6 completions stop making forward progress."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


DURATION = re.compile(r"^([1-9][0-9]*)(ms|s|m|h)$")
COMPLETION_METRIC = "bluemap_valid_completion_offset_seconds"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--rate", required=True, type=float)
    parser.add_argument("--viewers", required=True, type=float)
    parser.add_argument("--marker-interval-seconds", required=True, type=float)
    parser.add_argument("--markers-present", action="store_true")
    parser.add_argument("--duration", required=True)
    parser.add_argument("--window-seconds", required=True, type=float)
    parser.add_argument(
        "--startup-allowance-seconds", required=True, type=float
    )
    parser.add_argument(
        "--minimum-completion-fraction", required=True, type=float
    )
    parser.add_argument(
        "--start-time-tolerance-seconds", required=True, type=float
    )
    return parser.parse_args()


def duration_seconds(value: str) -> float:
    match = DURATION.fullmatch(value)
    if match is None:
        raise ValueError(
            "duration must be a positive integer followed by ms, s, m, or h"
        )
    amount = int(match.group(1))
    multiplier = {
        "ms": 0.001,
        "s": 1.0,
        "m": 60.0,
        "h": 3600.0,
    }[match.group(2)]
    return amount * multiplier


def timestamp_epoch(value: Any) -> float:
    if not isinstance(value, str) or not value:
        raise ValueError("completion point timestamp is missing")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("completion point timestamp is malformed") from error
    if parsed.tzinfo is None:
        raise ValueError("completion point timestamp has no timezone")
    epoch = parsed.timestamp()
    if not math.isfinite(epoch):
        raise ValueError("completion point timestamp is not finite")
    return epoch


def finite_number(value: Any, label: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{label} must be finite and at least {minimum}")
    return number


def integer_count(value: Any, label: str) -> int:
    number = finite_number(value, label)
    if not number.is_integer():
        raise ValueError(f"{label} must be an integer")
    return int(number)


def metric_count(summary: dict[str, Any], metric: str) -> int:
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("k6 summary metrics are missing")
    data = metrics.get(metric)
    if not isinstance(data, dict):
        raise ValueError(f"k6 summary metric {metric} is missing")
    values = data.get("values")
    raw = values.get("count") if isinstance(values, dict) else data.get("count")
    return integer_count(raw, f"k6 summary {metric}.count")


def scenario_definitions(
    profile: str,
    rate: float,
    viewers: float,
    marker_interval_seconds: float,
    markers_present: bool,
) -> dict[str, tuple[float, float]]:
    if min(rate, viewers, marker_interval_seconds) <= 0:
        raise ValueError("rates and marker interval must be positive")
    if profile != "live-viewers":
        return {"workload": (rate, 0.0)}
    scenarios = {"playerPolling": (viewers, 0.0)}
    if markers_present:
        scenarios["markerPolling"] = (
            viewers / marker_interval_seconds,
            0.5,
        )
    return scenarios


def parse_raw_completion_evidence(
    raw: Path,
    scenarios: dict[str, tuple[float, float]],
    *,
    start_time_tolerance_seconds: float,
) -> dict[str, Any]:
    if not raw.is_file() or raw.is_symlink():
        raise ValueError("raw k6 completion evidence is missing or is a symlink")
    declarations = 0
    completion_points: list[tuple[float, float, str]] = []
    raw_iterations = 0
    raw_dropped = 0
    iteration_declarations = 0
    dropped_iteration_declarations = 0
    with raw.open(encoding="utf-8") as lines:
        for line_number, line in enumerate(lines, start=1):
            if line.endswith("\n"):
                line = line[:-1]
            if not line:
                raise ValueError(f"raw k6 evidence line {line_number} is empty")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"raw k6 evidence line {line_number} is malformed JSON"
                ) from error
            if not isinstance(row, dict):
                raise ValueError(
                    f"raw k6 evidence line {line_number} is not an object"
                )
            row_type = row.get("type")
            metric = row.get("metric")
            if row_type == "Metric" and metric == COMPLETION_METRIC:
                data = row.get("data")
                if (
                    not isinstance(data, dict)
                    or data.get("name") != COMPLETION_METRIC
                    or data.get("type") != "trend"
                ):
                    raise ValueError("completion metric declaration is malformed")
                declarations += 1
                continue
            if row_type == "Metric" and metric == "iterations":
                data = row.get("data")
                if (
                    not isinstance(data, dict)
                    or data.get("name") != "iterations"
                    or data.get("type") != "counter"
                ):
                    raise ValueError("iterations metric declaration is malformed")
                iteration_declarations += 1
                continue
            if row_type == "Metric" and metric == "dropped_iterations":
                data = row.get("data")
                if (
                    not isinstance(data, dict)
                    or data.get("name") != "dropped_iterations"
                    or data.get("type") != "counter"
                ):
                    raise ValueError(
                        "dropped_iterations metric declaration is malformed"
                    )
                dropped_iteration_declarations += 1
                continue
            if row_type != "Point" or metric not in {
                COMPLETION_METRIC,
                "iterations",
                "dropped_iterations",
            }:
                continue
            data = row.get("data")
            if not isinstance(data, dict):
                raise ValueError(
                    f"raw k6 point line {line_number} has no data object"
                )
            value = finite_number(
                data.get("value"), f"raw line {line_number} value"
            )
            tags = data.get("tags")
            if not isinstance(tags, dict):
                raise ValueError(
                    f"raw k6 point line {line_number} has no tags object"
                )
            scenario = tags.get("scenario")
            if scenario not in scenarios:
                raise ValueError(
                    f"raw k6 point line {line_number} has an unexpected scenario"
                )
            epoch = timestamp_epoch(data.get("time"))
            if metric == COMPLETION_METRIC:
                completion_points.append((epoch, value, scenario))
            elif metric == "iterations":
                if value != 1:
                    raise ValueError("raw iterations points must each have value 1")
                raw_iterations += 1
            else:
                if value != 1:
                    raise ValueError(
                        "raw dropped_iterations points must each have value 1"
                    )
                raw_dropped += 1

    if declarations != 1:
        raise ValueError("raw k6 evidence must declare the completion metric once")
    if iteration_declarations != 1:
        raise ValueError("raw k6 evidence must declare iterations once")
    if dropped_iteration_declarations != (1 if raw_dropped else 0):
        raise ValueError(
            "raw k6 evidence must declare dropped_iterations exactly when drops exist"
        )
    if not completion_points:
        raise ValueError("raw k6 evidence has no accepted completion points")

    inferred_origins = [
        epoch - offset - scenarios[scenario][1]
        for epoch, offset, scenario in completion_points
    ]
    origin_minimum = min(inferred_origins)
    origin_maximum = max(inferred_origins)
    origin_spread = origin_maximum - origin_minimum
    if origin_spread > start_time_tolerance_seconds:
        raise ValueError("completion points do not attest one scenario start time")
    origin = statistics.median(inferred_origins)
    completion_epochs = sorted(epoch for epoch, _, _ in completion_points)
    for epoch, offset, scenario in completion_points:
        expected_offset = epoch - origin
        reported_offset = offset + scenarios[scenario][1]
        if abs(expected_offset - reported_offset) > start_time_tolerance_seconds:
            raise ValueError("completion offset does not match its raw timestamp")

    return {
        "completionEpochs": completion_epochs,
        "completionPointCount": len(completion_points),
        "rawIterationCount": raw_iterations,
        "rawDroppedIterationCount": raw_dropped,
        "scenarioOriginEpochSeconds": origin,
        "scenarioOriginMinimumEpochSeconds": origin_minimum,
        "scenarioOriginMaximumEpochSeconds": origin_maximum,
        "scenarioOriginSpreadSeconds": origin_spread,
    }


def evaluate(
    raw: Path,
    summary: dict[str, Any],
    *,
    profile: str,
    rate: float,
    viewers: float,
    marker_interval_seconds: float,
    markers_present: bool,
    duration: str,
    window_seconds: float,
    startup_allowance_seconds: float,
    minimum_completion_fraction: float,
    start_time_tolerance_seconds: float,
) -> dict[str, Any]:
    if not isinstance(summary, dict):
        raise ValueError("k6 summary must be a JSON object")
    for value, label in (
        (window_seconds, "window seconds"),
        (startup_allowance_seconds, "startup allowance seconds"),
        (minimum_completion_fraction, "minimum completion fraction"),
        (start_time_tolerance_seconds, "start-time tolerance seconds"),
    ):
        finite_number(value, label)
    if window_seconds <= 0 or start_time_tolerance_seconds <= 0:
        raise ValueError("window and start-time tolerance must be positive")
    if not 0 < minimum_completion_fraction <= 1:
        raise ValueError("minimum completion fraction must be in (0, 1]")

    configured_seconds = duration_seconds(duration)
    if configured_seconds < startup_allowance_seconds + window_seconds:
        raise ValueError(
            "duration must contain a full progress window after startup allowance"
        )
    scenarios = scenario_definitions(
        profile,
        rate,
        viewers,
        marker_interval_seconds,
        markers_present,
    )
    raw_evidence = parse_raw_completion_evidence(
        raw,
        scenarios,
        start_time_tolerance_seconds=start_time_tolerance_seconds,
    )
    summary_iterations = metric_count(summary, "iterations")
    summary_dropped = metric_count(summary, "dropped_iterations")
    summary_available = metric_count(summary, "bluemap_available_responses")
    summary_overload = metric_count(summary, "bluemap_overload_responses")
    accepted = summary_available + summary_overload
    if raw_evidence["completionPointCount"] != accepted:
        raise ValueError(
            "raw accepted completion count differs from the k6 summary"
        )
    if raw_evidence["rawIterationCount"] != summary_iterations:
        raise ValueError("raw iteration count differs from the k6 summary")
    if raw_evidence["rawDroppedIterationCount"] != summary_dropped:
        raise ValueError("raw dropped-iteration count differs from the k6 summary")

    offered_rate = sum(definition[0] for definition in scenarios.values())
    minimum_completed = max(
        1,
        math.ceil(
            offered_rate * window_seconds * minimum_completion_fraction
        ),
    )
    origin = raw_evidence["scenarioOriginEpochSeconds"]
    evaluation_start = origin + startup_allowance_seconds
    evaluation_end = origin + configured_seconds
    final_window_start = evaluation_end - window_seconds
    completion_epochs = raw_evidence["completionEpochs"]
    candidate_starts = {
        evaluation_start,
        final_window_start,
        *(
            epoch
            for epoch in completion_epochs
            if evaluation_start <= epoch <= final_window_start
        ),
    }
    windows = []
    for start in sorted(candidate_starts):
        end = start + window_seconds
        # Continuous rolling windows use (start, end]. A minimum can only
        # begin at the evaluation boundary or exactly when completions leave.
        completed = bisect.bisect_right(
            completion_epochs, end
        ) - bisect.bisect_right(completion_epochs, start)
        windows.append((completed, start, end))
    minimum_observed, worst_start, worst_end = min(windows)
    passed = minimum_observed >= minimum_completed
    return {
        "formatVersion": 1,
        "completionMetric": COMPLETION_METRIC,
        "configuredDuration": duration,
        "configuredDurationSeconds": configured_seconds,
        "controls": {
            "windowSeconds": window_seconds,
            "startupAllowanceSeconds": startup_allowance_seconds,
            "minimumCompletionFraction": minimum_completion_fraction,
            "startTimeToleranceSeconds": start_time_tolerance_seconds,
        },
        "offeredIterationsPerSecond": offered_rate,
        "minimumCompletedIterationsPerWindow": minimum_completed,
        "windowBoundary": "(start,end]",
        "evaluatedContinuousWindows": len(windows),
        "acceptedCompletionPoints": raw_evidence["completionPointCount"],
        "summaryCounts": {
            "availableResponses": summary_available,
            "overloadResponses": summary_overload,
            "acceptedResponses": accepted,
            "completedIterations": summary_iterations,
            "droppedIterations": summary_dropped,
        },
        "rawCounts": {
            "acceptedCompletionPoints": raw_evidence["completionPointCount"],
            "completedIterations": raw_evidence["rawIterationCount"],
            "droppedIterations": raw_evidence["rawDroppedIterationCount"],
        },
        "timing": {
            "scenarioOriginEpochSeconds": origin,
            "minimumInferredOriginEpochSeconds": raw_evidence[
                "scenarioOriginMinimumEpochSeconds"
            ],
            "maximumInferredOriginEpochSeconds": raw_evidence[
                "scenarioOriginMaximumEpochSeconds"
            ],
            "inferredOriginSpreadSeconds": raw_evidence[
                "scenarioOriginSpreadSeconds"
            ],
            "evaluationStartOffsetSeconds": startup_allowance_seconds,
            "evaluationEndOffsetSeconds": configured_seconds,
        },
        "worstWindow": {
            "startOffsetSeconds": worst_start - origin,
            "endOffsetSeconds": worst_end - origin,
            "completedIterations": minimum_observed,
        },
        "passed": passed,
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    try:
        summary = json.loads(args.summary.read_text(encoding="utf-8"))
        result = evaluate(
            args.raw,
            summary,
            profile=args.profile,
            rate=args.rate,
            viewers=args.viewers,
            marker_interval_seconds=args.marker_interval_seconds,
            markers_present=args.markers_present,
            duration=args.duration,
            window_seconds=args.window_seconds,
            startup_allowance_seconds=args.startup_allowance_seconds,
            minimum_completion_fraction=args.minimum_completion_fraction,
            start_time_tolerance_seconds=args.start_time_tolerance_seconds,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        result = {"formatVersion": 1, "passed": False, "errors": [str(error)]}
    atomic_json(args.output, result)
    if not result["passed"]:
        print("COMPLETION PROGRESS GATE FAILURE", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
