#!/usr/bin/env python3
"""Evaluate scheduled k6 iterations without using wall-clock summary rates."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any


DURATION = re.compile(r"^([1-9][0-9]*)(ms|s|m|h)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--rate", required=True, type=float)
    parser.add_argument("--viewers", required=True, type=float)
    parser.add_argument("--marker-interval-seconds", required=True, type=float)
    parser.add_argument("--markers-present", action="store_true")
    parser.add_argument("--duration", required=True)
    parser.add_argument("--minimum-achieved-ratio", required=True, type=float)
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


def metric_value(
    summary: dict[str, Any],
    metric: str,
    value: str,
) -> float | None:
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        return None
    metric_data = metrics.get(metric, {})
    if not isinstance(metric_data, dict):
        return None
    values = metric_data.get("values")
    raw = (
        values.get(value)
        if isinstance(values, dict)
        else metric_data.get(value)
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    number = float(raw)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def scenario_definitions(
    profile: str,
    rate: float,
    viewers: float,
    marker_interval_seconds: float,
    markers_present: bool,
) -> list[tuple[str, float]]:
    if min(rate, viewers, marker_interval_seconds) <= 0:
        raise ValueError("rates and marker interval must be positive")
    if profile != "live-viewers":
        return [("workload", rate)]

    scenarios = [("playerPolling", viewers)]
    if markers_present:
        scenarios.append(("markerPolling", viewers / marker_interval_seconds))
    return scenarios


def evaluate(
    summary: dict[str, Any],
    *,
    profile: str,
    rate: float,
    viewers: float,
    marker_interval_seconds: float,
    markers_present: bool,
    duration: str,
    minimum_achieved_ratio: float,
) -> dict[str, Any]:
    if not isinstance(summary, dict):
        raise ValueError("k6 summary must be a JSON object")
    if not 0 < minimum_achieved_ratio <= 1:
        raise ValueError("minimum achieved ratio must be in (0, 1]")

    configured_seconds = duration_seconds(duration)
    definitions = scenario_definitions(
        profile,
        rate,
        viewers,
        marker_interval_seconds,
        markers_present,
    )
    scenarios = []
    for name, offered_rate in definitions:
        metric = f"iterations{{scenario:{name}}}"
        completed = metric_value(summary, metric, "count")
        expected = offered_rate * configured_seconds
        minimum = expected * minimum_achieved_ratio
        scenarios.append(
            {
                "scenario": name,
                "metric": metric,
                "offeredIterationsPerSecond": offered_rate,
                "expectedScheduledIterations": expected,
                "minimumCompletedIterations": minimum,
                "completedIterations": completed,
                "achievedIterationsPerSecondOverConfiguredDuration": (
                    completed / configured_seconds
                    if completed is not None
                    else None
                ),
                "passed": completed is not None and completed >= minimum,
            }
        )

    overall_completed = metric_value(summary, "iterations", "count")
    dropped = metric_value(summary, "dropped_iterations", "count")
    wall_clock_rate = metric_value(summary, "iterations", "rate")
    scenario_total = (
        sum(scenario["completedIterations"] for scenario in scenarios)
        if all(scenario["completedIterations"] is not None for scenario in scenarios)
        else None
    )
    scenario_counts_equal_overall = (
        scenario_total is not None
        and overall_completed is not None
        and scenario_total == overall_completed
    )
    total_offered_rate = sum(
        scenario["offeredIterationsPerSecond"] for scenario in scenarios
    )

    passed = (
        dropped == 0
        and scenario_counts_equal_overall
        and all(scenario["passed"] for scenario in scenarios)
    )
    return {
        "formatVersion": 1,
        "configuredDuration": duration,
        "configuredDurationSeconds": configured_seconds,
        "minimumAchievedRatio": minimum_achieved_ratio,
        "scenarios": scenarios,
        "totals": {
            "offeredIterationsPerSecond": total_offered_rate,
            "expectedScheduledIterations": (
                total_offered_rate * configured_seconds
            ),
            "minimumCompletedIterations": (
                total_offered_rate
                * configured_seconds
                * minimum_achieved_ratio
            ),
            "completedIterations": overall_completed,
            "achievedIterationsPerSecondOverConfiguredDuration": (
                overall_completed / configured_seconds
                if overall_completed is not None
                else None
            ),
            "k6WallClockIterationsPerSecond": wall_clock_rate,
        },
        "droppedIterations": dropped,
        "scenarioCompletedIterations": scenario_total,
        "scenarioCountsEqualOverall": scenario_counts_equal_overall,
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
            summary,
            profile=args.profile,
            rate=args.rate,
            viewers=args.viewers,
            marker_interval_seconds=args.marker_interval_seconds,
            markers_present=args.markers_present,
            duration=args.duration,
            minimum_achieved_ratio=args.minimum_achieved_ratio,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        result = {"passed": False, "errors": [str(error)]}
    atomic_json(args.output, result)
    if not result["passed"]:
        print("ARRIVAL GATE FAILURE", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
