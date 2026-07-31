from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from support import (  # noqa: E402
    RUN_ID,
    SOURCE_REVISION,
    analyze,
    formal_matrix,
    orchestrate,
    runpod_identity,
    schedule_entry,
    write_json,
)


def option(command: list[str], flag: str) -> str:
    index = command.index(flag)
    return command[index + 1]


class RunPodOrchestratorTests(unittest.TestCase):
    def test_traffic_mode_defaults_to_direct_ssh_l4_traefik(self) -> None:
        args = orchestrate.parse_args(["dry-run"])
        self.assertEqual(args.traffic_mode, "ssh-l4-traefik")
        self.assertEqual(
            args.traffic_base_url,
            "http://bluemap-test.guenter.cloud",
        )

    def test_allowlist_contains_only_six_disposable_deployments(self) -> None:
        orchestrate.validate_target_constants()
        self.assertEqual(len(orchestrate.TARGETS), 6)
        self.assertEqual(len(orchestrate.FORMAL_DEPLOYMENTS), 6)
        self.assertNotIn("minecraft", orchestrate.FORMAL_DEPLOYMENTS)
        for deployment in orchestrate.FORMAL_DEPLOYMENTS:
            command = orchestrate.scale_command(
                deployment,
                0,
                kubeconfig=Path("/tmp/controller-test-kubeconfig"),
            )
            self.assertIn(f"deployment/{deployment}", command)
            self.assertNotIn("deployment/minecraft", command)
        with self.assertRaises(orchestrate.SafetyError):
            orchestrate.scale_command(
                "minecraft",
                0,
                kubeconfig=Path("/tmp/controller-test-kubeconfig"),
            )

    def test_preflight_matrix_is_an_exact_deterministic_formal_derivation(
        self,
    ) -> None:
        formal = formal_matrix()
        original = copy.deepcopy(formal)
        first = orchestrate.derive_preflight_matrix(formal)
        second = orchestrate.derive_preflight_matrix(formal)
        self.assertEqual(first, second)
        self.assertEqual(formal, original)
        self.assertEqual(first["formatVersion"], 4)
        self.assertEqual(first["repetitions"], 1)
        self.assertEqual(
            first["scheduleSeed"],
            "bluemap-web-performance-ssh-l4-preflight-v1",
        )
        self.assertEqual(
            formal["controls"]["minimumAchievedRateRatio"],
            0.99,
        )
        self.assertEqual(
            first["controls"]["minimumAchievedRateRatio"],
            1.0,
        )
        self.assertEqual(first["controls"], orchestrate.PREFLIGHT_CONTROLS)
        self.assertEqual(
            orchestrate.FORMAL_OVERLOAD_POLICIES,
            {
                "map-mixed-r15": "allow-explicit",
                "map-mixed-horizontal-r40": "allow-explicit",
                "live-viewers-r15": "forbid",
                "large-object-r1": "allow-explicit",
            },
        )
        self.assertEqual(
            [variant["id"] for variant in first["variants"]],
            list(orchestrate.PREFLIGHT_VARIANTS),
        )
        formal_variants = {variant["id"]: variant for variant in formal["variants"]}
        for variant in first["variants"]:
            self.assertEqual(variant, formal_variants[variant["id"]])
        self.assertEqual(first["cases"], list(orchestrate.PREFLIGHT_CASES))
        self.assertEqual(
            [
                {
                    "id": case["id"],
                    "profile": case["profile"],
                    "rate": case["rate"],
                    "variants": case["variants"],
                    "overloadPolicy": case["overloadPolicy"],
                    "latencyP95Milliseconds": case[
                        "latencyP95Milliseconds"
                    ],
                    "latencyP99Milliseconds": case[
                        "latencyP99Milliseconds"
                    ],
                }
                for case in first["cases"]
            ],
            [
                {
                    "id": "preflight-settings-r1",
                    "profile": "settings",
                    "rate": 1,
                    "variants": [
                        "java-new-postgresql",
                        "rust-postgresql",
                    ],
                    "overloadPolicy": "forbid",
                    "latencyP95Milliseconds": 5000,
                    "latencyP99Milliseconds": 10000,
                },
                {
                    "id": "preflight-conditional-horizontal-r1",
                    "profile": "conditional",
                    "rate": 1,
                    "variants": [
                        "java-new-postgresql-r3",
                        "rust-postgresql-r3",
                    ],
                    "overloadPolicy": "forbid",
                    "latencyP95Milliseconds": 5000,
                    "latencyP99Milliseconds": 10000,
                },
                {
                    "id": "preflight-horizontal-r40",
                    "profile": "map-data-mixed",
                    "rate": 40,
                    "variants": [
                        "java-new-postgresql-r3",
                        "rust-postgresql-r3",
                    ],
                    "overloadPolicy": "allow-explicit",
                    "latencyP95Milliseconds": 5000,
                    "latencyP99Milliseconds": 10000,
                },
            ],
        )

        tampered = copy.deepcopy(formal)
        next(
            variant
            for variant in tampered["variants"]
            if variant["id"] == "java-new-postgresql"
        )["contractMode"] = "legacy"
        with self.assertRaisesRegex(
            orchestrate.SafetyError,
            "Every preflight variant must use the enhanced contract",
        ):
            orchestrate.derive_preflight_matrix(tampered)

    def test_preflight_schedule_is_generated_and_validated_as_six_entries(
        self,
    ) -> None:
        formal = formal_matrix()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix_path = root / "matrix.json"
            schedule_path = root / "schedule.json"
            orchestrate.atomic_write_json(
                matrix_path,
                orchestrate.derive_preflight_matrix(formal),
            )
            orchestrate.run_checked(
                [
                    sys.executable,
                    str(orchestrate.DEFAULT_GENERATOR),
                    "generate",
                    str(matrix_path),
                    str(schedule_path),
                ],
                cwd=orchestrate.REPOSITORY_ROOT,
            )
            matrix, schedule = orchestrate.validate_preflight_documents(
                formal,
                matrix_path,
                schedule_path,
                orchestrate.DEFAULT_GENERATOR,
            )
            self.assertEqual(matrix["repetitions"], 1)
            self.assertEqual(len(schedule["entries"]), 6)
            self.assertEqual(
                [entry["sequence"] for entry in schedule["entries"]],
                list(range(1, 7)),
            )
            self.assertEqual(
                len({entry["runnerCaseId"] for entry in schedule["entries"]}),
                6,
            )
            self.assertEqual(
                [
                    (entry["matrixCaseId"], entry["variantId"])
                    for entry in schedule["entries"]
                ],
                [
                    ("preflight-horizontal-r40", "rust-postgresql-r3"),
                    ("preflight-horizontal-r40", "java-new-postgresql-r3"),
                    ("preflight-conditional-horizontal-r1", "rust-postgresql-r3"),
                    ("preflight-conditional-horizontal-r1", "java-new-postgresql-r3"),
                    ("preflight-settings-r1", "java-new-postgresql"),
                    ("preflight-settings-r1", "rust-postgresql"),
                ],
            )
            self.assertEqual(
                [entry["overloadPolicy"] for entry in schedule["entries"]],
                [
                    "allow-explicit",
                    "allow-explicit",
                    "forbid",
                    "forbid",
                    "forbid",
                    "forbid",
                ],
            )
            self.assertTrue(
                all(entry["contractMode"] == "enhanced" for entry in schedule["entries"])
            )
            self.assertEqual(
                [entry["rate"] for entry in schedule["entries"]],
                [40, 40, 1, 1, 1, 1],
            )
            self.assertEqual(
                [entry["viewers"] for entry in schedule["entries"]],
                [40, 40, 1, 1, 1, 1],
            )
            self.assertTrue(
                all(
                    entry["latencyP95Milliseconds"] == 5000
                    and entry["latencyP99Milliseconds"] == 10000
                    for entry in schedule["entries"]
                )
            )
            self.assertTrue(
                all(
                    entry["warmupDuration"] == "30s"
                    and entry["measurementDuration"] == "2m"
                    and entry["cooldownSeconds"] == 15
                    and entry["minimumAchievedRateRatio"] == 1.0
                    and entry["preAllocatedVUs"] == 256
                    and entry["maxVUs"] == 512
                    for entry in schedule["entries"]
                )
            )

    def test_preflight_cli_is_non_resumable_and_has_a_distinct_confirmation(
        self,
    ) -> None:
        arguments = [
            "preflight",
            "--run-root",
            "/tmp/preflight",
            "--controller-pod",
            "bluemap-perf-formal-controller-test",
            "--confirm",
            orchestrate.PREFLIGHT_CONFIRMATION,
            "--load-generator-backend",
            "runpod-ssh",
            "--load-generator-identity",
            "/tmp/identity.json",
            "--load-generator-identity-key",
            "/tmp/id_ed25519",
            "--traffic-mode",
            "ssh-l4-traefik",
            "--traffic-base-url",
            "http://bluemap-test.guenter.cloud",
            "--traffic-service",
            "bluemap-perf-public",
            "--traffic-service-port",
            "8100",
            "--formal-run-id",
            RUN_ID,
        ]
        args = orchestrate.parse_args(arguments)
        self.assertFalse(hasattr(args, "resume"))
        self.assertNotEqual(
            orchestrate.PREFLIGHT_CONFIRMATION,
            orchestrate.CONFIRMATION,
        )
        with patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                orchestrate.parse_args([*arguments, "--resume"])

    def test_preflight_root_is_absent_and_shares_the_formal_global_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            benchmark_root = Path(directory)
            formal = benchmark_root / "artifacts" / "formal-runs" / RUN_ID
            preflight = formal.with_name(f"{formal.name}-preflight")
            preflight.parent.mkdir(parents=True)
            with patch.object(orchestrate, "BENCHMARK_ROOT", benchmark_root):
                self.assertEqual(
                    orchestrate.require_new_preflight_root(preflight),
                    preflight,
                )
                self.assertEqual(
                    orchestrate.global_lock_path(formal),
                    orchestrate.global_lock_path(preflight),
                )
                preflight.mkdir()
                with self.assertRaisesRegex(
                    orchestrate.SafetyError,
                    "non-resumable",
                ):
                    orchestrate.require_new_preflight_root(preflight)
                preflight.rmdir()
                target = preflight.with_name("missing-target")
                preflight.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(
                    orchestrate.SafetyError,
                    "symlink",
                ):
                    orchestrate.require_new_preflight_root(preflight)
                with self.assertRaisesRegex(
                    orchestrate.SafetyError,
                    "immediate child",
                ):
                    orchestrate.validate_run_root(formal / "nested")
                lock_path = formal.parent / ".active-formal-run.lock"
                lock_target = formal.parent / "lock-target"
                lock_target.write_text("not-a-lock\n", encoding="utf-8")
                lock_path.symlink_to(lock_target)
                with self.assertRaisesRegex(
                    orchestrate.SafetyError,
                    "symlink",
                ):
                    orchestrate.acquire_global_lock(formal)

    def test_preflight_assessor_rejects_a_completed_runner_failure(self) -> None:
        schedule = {
            "entries": [
                {
                    "sequence": sequence,
                    "entryId": f"entry-{sequence}",
                    "runnerCaseId": f"case-{sequence}",
                    "variantId": "java-new-postgresql",
                }
                for sequence in range(1, 7)
            ]
        }
        state = {
            "status": "completed",
            "nextSequence": 7,
            "entries": {
                str(entry["sequence"]): {
                    "status": "completed",
                    "entryId": entry["entryId"],
                    "runnerCaseId": entry["runnerCaseId"],
                    "variantId": entry["variantId"],
                    "result": "passed",
                    "runnerExitStatus": 0,
                }
                for entry in schedule["entries"]
            },
        }
        self.assertTrue(
            orchestrate.assess_preflight_state(state, schedule)["passed"]
        )
        state["entries"]["4"]["result"] = "failed"
        state["entries"]["4"]["runnerExitStatus"] = 1
        assessment = orchestrate.assess_preflight_state(state, schedule)
        self.assertFalse(assessment["passed"])
        self.assertTrue(any("entry 4" in item for item in assessment["failures"]))

    def test_preflight_node_noise_rejects_the_whole_incomparable_pair(self) -> None:
        schedule_entries = []
        for case_index in range(1, 4):
            for variant_index, variant in enumerate(("java", "rust"), start=1):
                schedule_entries.append(
                    {
                        "sequence": len(schedule_entries) + 1,
                        "entryId": f"case-{case_index}/{variant}/block-1",
                        "runnerCaseId": f"case-{case_index}-{variant}-b1",
                        "matrixCaseId": f"case-{case_index}",
                        "variantId": variant,
                        "block": 1,
                    }
                )
        schedule = {"entries": schedule_entries}
        helper = orchestrate.load_prometheus_capture_helper()
        query_start = 1_785_456_000
        query_end = query_start + 180
        window_start = query_start + 15
        window_end = query_start + 150
        with tempfile.TemporaryDirectory() as directory:
            preflight_root = Path(directory)
            for entry in schedule_entries:
                mean = 1.0
                if entry["matrixCaseId"] == "case-2" and entry["variantId"] == "rust":
                    mean = 1.6
                query_results = [
                    {
                        "name": "node_non_target_container_cpu_cores",
                        "response": {
                            "status": "success",
                            "data": {
                                "resultType": "matrix",
                                "result": [
                                    {
                                        "metric": {"node": "node-a"},
                                        "values": [
                                            [timestamp, str(mean)]
                                            for timestamp in range(
                                                query_start,
                                                query_end + 1,
                                                orchestrate.PROMETHEUS_STEP_SECONDS,
                                            )
                                        ],
                                    }
                                ],
                            },
                        },
                    }
                ]
                windows = [
                    {
                        "repetition": 1,
                        "start": float(window_start),
                        "end": float(window_end),
                    }
                ]
                node_noise = helper.assess_node_noise(
                    query_results,
                    ["node-a"],
                    windows,
                    orchestrate.MAXIMUM_NON_TARGET_NODE_CPU_SPREAD_CORES,
                    orchestrate.MAXIMUM_NON_TARGET_NODE_CPU_MEAN_CORES,
                    orchestrate.MAXIMUM_NON_TARGET_NODE_CPU_LEVEL_CORES,
                    query_start=query_start,
                    query_end=query_end,
                    step_seconds=orchestrate.PROMETHEUS_STEP_SECONDS,
                )
                case_root = (
                    preflight_root / "results" / entry["runnerCaseId"]
                )
                write_json(
                    case_root / "samples" / "prometheus-query-range.json",
                    {
                        "nodes": ["node-a"],
                        "range": {
                            "start": query_start,
                            "end": query_end,
                            "stepSeconds": orchestrate.PROMETHEUS_STEP_SECONDS,
                        },
                        "queries": query_results,
                        "nodeNoise": node_noise,
                    },
                )
                write_json(
                    case_root / "inputs" / "workload.json",
                    {
                        "targets": {"nodes": ["node-a"]},
                        "observability": {
                            "prometheus": {
                                "enabled": True,
                                "stepSeconds": orchestrate.PROMETHEUS_STEP_SECONDS,
                                "maximumNonTargetNodeCpuRangeCores": (
                                    orchestrate.MAXIMUM_NON_TARGET_NODE_CPU_SPREAD_CORES
                                ),
                                "maximumNonTargetNodeCpuMeanCores": (
                                    orchestrate.MAXIMUM_NON_TARGET_NODE_CPU_MEAN_CORES
                                ),
                                "maximumNonTargetNodeCpuLevelCores": (
                                    orchestrate.MAXIMUM_NON_TARGET_NODE_CPU_LEVEL_CORES
                                ),
                            }
                        },
                    },
                )
                archived_helper = case_root / "inputs" / "capture_prometheus.py"
                archived_helper.write_bytes(
                    (orchestrate.TOOLS_DIR / "capture_prometheus.py").read_bytes()
                )
                (case_root / "phases.ndjson").write_text(
                    json.dumps(
                        {
                            "timestamp": datetime.fromtimestamp(
                                window_start, UTC
                            ).isoformat().replace("+00:00", "Z"),
                            "repetition": 1,
                            "phase": "measurement",
                            "event": "start",
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "timestamp": datetime.fromtimestamp(
                                window_end, UTC
                            ).isoformat().replace("+00:00", "Z"),
                            "repetition": 1,
                            "phase": "measurement",
                            "event": "end",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

            report = orchestrate.assess_preflight_node_noise_comparability(
                preflight_root,
                schedule,
            )
            drifted_workload_path = (
                preflight_root
                / "results"
                / schedule_entries[0]["runnerCaseId"]
                / "inputs"
                / "workload.json"
            )
            drifted_workload = json.loads(
                drifted_workload_path.read_text(encoding="utf-8")
            )
            drifted_workload["observability"]["prometheus"][
                "maximumNonTargetNodeCpuRangeCores"
            ] = 0.75
            write_json(drifted_workload_path, drifted_workload)
            drifted_report = orchestrate.assess_preflight_node_noise_comparability(
                preflight_root,
                schedule,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(
            report["excludedCaseBlocks"],
            [{"caseId": "case-2", "block": 1}],
        )
        failed = next(
            item for item in report["caseBlocks"] if item["caseId"] == "case-2"
        )
        self.assertEqual(
            failed["reasons"],
            ["node-a: cross-run-background-spread"],
        )
        self.assertEqual(failed["nodes"][0]["samples"], 2)
        for item in report["caseBlocks"]:
            self.assertIs(item["comparable"], item["caseId"] != "case-2")
        self.assertFalse(drifted_report["passed"])
        drifted_case = next(
            item
            for item in drifted_report["caseBlocks"]
            if item["caseId"] == "case-1"
        )
        self.assertIn(
            "case-1/java/block-1: unavailable-invalid-or-over-limit",
            drifted_case["reasons"],
        )

    def test_preflight_node_noise_fails_closed_on_missing_raw_evidence(self) -> None:
        schedule = {
            "entries": [
                {
                    "sequence": sequence,
                    "entryId": f"case-{(sequence + 1) // 2}/variant-{sequence}/block-1",
                    "runnerCaseId": f"runner-{sequence}",
                    "matrixCaseId": f"case-{(sequence + 1) // 2}",
                    "variantId": f"variant-{sequence}",
                    "block": 1,
                }
                for sequence in range(1, 7)
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            report = orchestrate.assess_preflight_node_noise_comparability(
                Path(directory),
                schedule,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(len(report["excludedCaseBlocks"]), 3)
        self.assertTrue(
            all(
                item["reasons"]
                and all(
                    reason.endswith("unavailable-invalid-or-over-limit")
                    for reason in item["reasons"]
                )
                for item in report["caseBlocks"]
            )
        )

    @staticmethod
    def relay_headroom_report() -> dict[str, object]:
        thresholds = orchestrate.PREFLIGHT_RELAY_THRESHOLDS
        return {
            "formatVersion": 1,
            "passed": True,
            "startedAt": "2026-07-31T00:00:00Z",
            "stoppedAt": "2026-07-31T00:15:00Z",
            "namespace": "minecraft",
            "pod": "bluemap-perf-formal-controller-test",
            "podUid": "controller-uid",
            "container": "controller",
            "source": "metrics.k8s.io/v1beta1",
            "limits": {
                "cpuCores": 2.0,
                "memoryBytes": float(2 * 1024**3),
            },
            "thresholds": copy.deepcopy(thresholds),
            "checks": {
                "noMetricsApiErrors": True,
                "minimumUniqueMetricTimestamps": True,
                "maximumUniqueMetricTimestampGapSeconds": True,
                "maximumMetricAgeSeconds": True,
                "initialCoverageGapSeconds": True,
                "finalCoverageGapSeconds": True,
                "p95CpuLimitRatio": True,
                "maximumCpuLimitRatio": True,
                "maximumMemoryLimitRatio": True,
            },
            "observed": {
                "successfulFetches": 12,
                "errors": 0,
                "uniqueMetricTimestamps": 6,
                "metricWindows": ["28.454s"],
                "maximumUniqueMetricTimestampGapSeconds": thresholds[
                    "maximumUniqueMetricTimestampGapSeconds"
                ],
                "maximumMetricAgeSeconds": thresholds[
                    "maximumMetricAgeSeconds"
                ],
                "initialCoverageGapSeconds": thresholds[
                    "maximumCoverageGapSeconds"
                ],
                "finalCoverageGapSeconds": thresholds[
                    "maximumCoverageGapSeconds"
                ],
                "p95CpuLimitRatio": thresholds["p95CpuLimitRatio"],
                "maximumCpuLimitRatio": thresholds["maximumCpuLimitRatio"],
                "maximumMemoryLimitRatio": thresholds[
                    "maximumMemoryLimitRatio"
                ],
            },
            "limitation": "metrics.k8s.io exposes coarse aggregate metrics",
        }

    def test_relay_headroom_report_is_revalidated_at_exact_boundaries(self) -> None:
        self.assertEqual(
            orchestrate.validate_metrics_window("28.454s"),
            "28.454s",
        )
        for invalid in ("0s", "-1s", "nan", "28.454"):
            with self.subTest(window=invalid):
                with self.assertRaises(orchestrate.SafetyError):
                    orchestrate.validate_metrics_window(invalid)
        self.assertEqual(
            orchestrate.PREFLIGHT_RELAY_THRESHOLDS[
                "maximumUniqueMetricTimestampGapSeconds"
            ],
            45.0,
        )
        self.assertEqual(
            orchestrate.PREFLIGHT_RELAY_THRESHOLDS[
                "maximumCoverageGapSeconds"
            ],
            60.0,
        )
        report = self.relay_headroom_report()
        orchestrate.validate_relay_headroom_report(
            report,
            "bluemap-perf-formal-controller-test",
        )

        failed = copy.deepcopy(report)
        failed["passed"] = False
        failed["checks"]["noMetricsApiErrors"] = False
        failed["observed"]["errors"] = 1
        orchestrate.validate_relay_headroom_report(
            failed,
            "bluemap-perf-formal-controller-test",
        )

        malformed = copy.deepcopy(failed)
        malformed["checks"]["noMetricsApiErrors"] = True
        with self.assertRaisesRegex(orchestrate.SafetyError, "inconsistent"):
            orchestrate.validate_relay_headroom_report(
                malformed,
                "bluemap-perf-formal-controller-test",
            )

        malformed = copy.deepcopy(report)
        malformed["unexpected"] = True
        with self.assertRaisesRegex(orchestrate.SafetyError, "schema"):
            orchestrate.validate_relay_headroom_report(
                malformed,
                "bluemap-perf-formal-controller-test",
            )

    def test_failed_relay_gate_is_preserved_before_preflight_raises(self) -> None:
        schedule = {
            "entries": [
                {
                    "sequence": sequence,
                    "entryId": f"entry-{sequence}",
                    "runnerCaseId": f"case-{sequence}",
                    "variantId": "java-new-postgresql",
                }
                for sequence in range(1, 7)
            ]
        }
        state = {
            "status": "completed",
            "nextSequence": 7,
            "entries": {
                str(entry["sequence"]): {
                    "status": "completed",
                    "entryId": entry["entryId"],
                    "runnerCaseId": entry["runnerCaseId"],
                    "variantId": entry["variantId"],
                    "result": "passed",
                    "runnerExitStatus": 0,
                }
                for entry in schedule["entries"]
            },
        }
        relay = self.relay_headroom_report()
        relay["passed"] = False
        relay["checks"]["maximumUniqueMetricTimestampGapSeconds"] = False
        relay["observed"]["maximumUniqueMetricTimestampGapSeconds"] = 46.0

        with tempfile.TemporaryDirectory() as directory:
            preflight_root = Path(directory) / "preflight"
            relay_path = (
                preflight_root / "observability" / "relay-headroom.json"
            )
            write_json(relay_path, relay)
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "headroom gate failed",
            ):
                orchestrate.persist_preflight_outcome(
                    preflight_root=preflight_root,
                    state=state,
                    schedule=schedule,
                    relay_report=relay,
                    controller_pod="bluemap-perf-formal-controller-test",
                    formal_run_id=RUN_ID,
                    formal_matrix={"benchmarkGitRevision": SOURCE_REVISION},
                    provenance={
                        "sourceFormalInputs": {"matrixSha256": "a" * 64},
                        "traffic": {"mode": "ssh-l4-traefik"},
                        "loadGeneratorIdentitySha256": "b" * 64,
                        "loadGeneratorSha256": "c" * 64,
                        "orchestratorSha256": "d" * 64,
                        "generatorSha256": "e" * 64,
                    },
                    derived_hashes={"matrixSha256": "f" * 64},
                    traefik_limitation={
                        "formatVersion": 1,
                        "available": False,
                        "gating": False,
                    },
                )

            evidence_path = preflight_root / "preflight-evidence.json"
            report_path = preflight_root / "preflight-report.json"
            sums_path = preflight_root / "SHA256SUMS"
            self.assertTrue(evidence_path.is_file())
            self.assertTrue(report_path.is_file())
            self.assertTrue(sums_path.is_file())
            self.assertEqual(
                orchestrate.load_json(evidence_path),
                orchestrate.preflight_evidence_inventory(preflight_root),
            )
            report = orchestrate.load_json(report_path)
            self.assertFalse(report["passed"])
            self.assertFalse(report["controllerRelay"]["passed"])
            self.assertIn(
                "maximumUniqueMetricTimestampGapSeconds",
                report["failures"][0],
            )
            self.assertEqual(
                sums_path.read_text(encoding="utf-8"),
                (
                    f"{orchestrate.file_sha256(evidence_path)}  "
                    "preflight-evidence.json\n"
                    f"{orchestrate.file_sha256(report_path)}  "
                    "preflight-report.json\n"
                ),
            )

    @staticmethod
    def controller_pod(run_id: str = RUN_ID) -> dict[str, object]:
        name = "bluemap-perf-formal-controller-test"
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": "minecraft",
                "uid": "controller-pod-uid",
                "labels": {
                    "app.kubernetes.io/name": "bluemap-perf-formal-controller",
                    "app.kubernetes.io/part-of": "bluemap-web-performance",
                    "bluemap.guenter.cloud/experiment-id": run_id,
                },
                "ownerReferences": [
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "name": "bluemap-perf-formal-controller",
                        "uid": "controller-job-uid",
                        "controller": True,
                    }
                ],
            },
            "spec": {
                "serviceAccountName": "bluemap-perf-formal-controller",
                "containers": [
                    {
                        "name": "controller",
                        "resources": {
                            "limits": {"cpu": "2", "memory": "2Gi"}
                        },
                    }
                ],
            },
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }

    @staticmethod
    def controller_metrics() -> dict[str, object]:
        return {
            "apiVersion": "metrics.k8s.io/v1beta1",
            "kind": "PodMetrics",
            "metadata": {
                "name": "bluemap-perf-formal-controller-test",
                "namespace": "minecraft",
            },
            "timestamp": orchestrate.timestamp(),
            "window": "28.454s",
            "containers": [
                {
                    "name": "controller",
                    "usage": {"cpu": "200m", "memory": "256Mi"},
                }
            ],
        }

    def test_relay_readiness_excludes_initial_404_from_measured_errors(self) -> None:
        class FakeKube:
            def __init__(self, pod: dict[str, object]) -> None:
                self.value = pod
                self.calls = 0

            def pod(self, name: str) -> dict[str, object]:
                return self.value

            def metrics(self, name: str) -> dict[str, object]:
                self.calls += 1
                if self.calls == 1:
                    raise orchestrate.SafetyError("404 PodMetrics not found")
                return RunPodOrchestratorTests.controller_metrics()

        name = "bluemap-perf-formal-controller-test"
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"BLUEMAP_CONTROLLER_POD_NAME": name, "HOSTNAME": name},
        ):
            sampler = orchestrate.RelayHeadroomSampler(
                FakeKube(self.controller_pod()),
                name,
                RUN_ID,
                Path(directory) / "relay",
                interval_seconds=60,
            )
            sampler.start(
                readiness_timeout_seconds=1,
                poll_interval_seconds=0.001,
            )
            sampler.stop()
            readiness = orchestrate.load_json(
                Path(directory) / "relay" / "relay-readiness.json"
            )
            self.assertTrue(readiness["ready"])
            self.assertEqual(readiness["attempts"], 2)
            self.assertEqual(len(readiness["transientErrors"]), 1)
            self.assertEqual(sampler.errors, [])
            self.assertEqual(
                (Path(directory) / "relay" / "relay-errors.ndjson").read_text(
                    encoding="utf-8"
                ),
                "",
            )

    def test_relay_readiness_timeout_refuses_without_a_measured_sample(self) -> None:
        class MissingMetricsKube:
            def pod(self, name: str) -> dict[str, object]:
                return RunPodOrchestratorTests.controller_pod()

            def metrics(self, name: str) -> dict[str, object]:
                raise orchestrate.SafetyError("404 PodMetrics not found")

        name = "bluemap-perf-formal-controller-test"
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"BLUEMAP_CONTROLLER_POD_NAME": name, "HOSTNAME": name},
        ):
            sampler = orchestrate.RelayHeadroomSampler(
                MissingMetricsKube(),
                name,
                RUN_ID,
                Path(directory) / "relay",
            )
            with self.assertRaisesRegex(orchestrate.SafetyError, "Timed out"):
                sampler.start(
                    readiness_timeout_seconds=0.01,
                    poll_interval_seconds=0.001,
                )
            self.assertEqual(sampler.samples, [])
            self.assertEqual(sampler.errors, [])
            readiness = orchestrate.load_json(
                Path(directory) / "relay" / "relay-readiness.json"
            )
            self.assertFalse(readiness["ready"])

    def test_relay_identity_refuses_wrong_executing_pod_and_run(self) -> None:
        class FakeKube:
            def __init__(self, pod: dict[str, object]) -> None:
                self.value = pod

            def pod(self, name: str) -> dict[str, object]:
                return self.value

        name = "bluemap-perf-formal-controller-test"
        with tempfile.TemporaryDirectory() as directory:
            sampler = orchestrate.RelayHeadroomSampler(
                FakeKube(self.controller_pod()),
                name,
                RUN_ID,
                Path(directory) / "relay",
            )
            with patch.dict(
                os.environ,
                {
                    "BLUEMAP_CONTROLLER_POD_NAME": "bluemap-perf-formal-controller-other",
                    "HOSTNAME": name,
                },
            ), self.assertRaisesRegex(orchestrate.SafetyError, "downward-API"):
                sampler.current_identity()
            wrong_run = orchestrate.RelayHeadroomSampler(
                FakeKube(self.controller_pod("another-run")),
                name,
                RUN_ID,
                Path(directory) / "wrong-run",
            )
            with patch.dict(
                os.environ,
                {"BLUEMAP_CONTROLLER_POD_NAME": name, "HOSTNAME": name},
            ), self.assertRaisesRegex(orchestrate.SafetyError, "identity"):
                wrong_run.current_identity()

    def test_load_runpod_identity_binds_run_id_and_refuses_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_path = root / "identity.json"
            write_json(identity_path, runpod_identity())
            loaded = orchestrate.load_runpod_identity(identity_path, RUN_ID)
            self.assertEqual(loaded, runpod_identity())

            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "run ID",
            ):
                orchestrate.load_runpod_identity(identity_path, "another-run")

            symlink = root / "identity-link.json"
            symlink.symlink_to(identity_path)
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "non-symlink",
            ):
                orchestrate.load_runpod_identity(symlink, RUN_ID)

            for invalid_revision in ("0" * 40, SOURCE_REVISION + "\n"):
                invalid = runpod_identity()
                invalid["sourceRevision"] = invalid_revision
                write_json(identity_path, invalid)
                with self.assertRaisesRegex(
                    orchestrate.SafetyError,
                    "source|format",
                ):
                    orchestrate.load_runpod_identity(identity_path, RUN_ID)

    def test_bundle_to_runpod_binding_is_exact(self) -> None:
        identity = runpod_identity()
        control = {
            "backend": "runpod-ssh",
            "image": identity["runpod"]["image"],
            "imageDigest": identity["runpod"]["imageDigest"],
            "sourceRevision": identity["sourceRevision"],
        }
        validated = orchestrate.validate_load_generator_control(
            control,
            SOURCE_REVISION,
        )
        self.assertEqual(
            orchestrate.validate_load_generator_execution_binding(
                validated,
                identity,
            ),
            orchestrate.load_generator_control_sha256(control),
        )
        for field, replacement in (
            ("sourceRevision", "b" * 40),
            ("image", "ghcr.io/jan-guenter/bluemap-perf-loadgen@sha256:" + "b" * 64),
            ("imageDigest", "sha256:" + "b" * 64),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(identity)
                if field == "sourceRevision":
                    changed[field] = replacement
                else:
                    changed["runpod"][field] = replacement
                with self.assertRaisesRegex(
                    orchestrate.SafetyError,
                    "differs",
                ):
                    orchestrate.validate_load_generator_execution_binding(
                        control,
                        changed,
                    )

    def test_frozen_bundle_manifest_is_strict_and_source_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix_path = root / "matrix.json"
            schedule_path = root / "schedule.json"
            admission_path = root / "runtime-admission-identities.json"
            bundle_path = root / "bundle-manifest.json"
            write_json(matrix_path, {"input": "matrix"})
            write_json(schedule_path, {"input": "schedule"})
            write_json(
                admission_path,
                {
                    "formatVersion": 1,
                    "benchmarkGitRevision": SOURCE_REVISION,
                    "podSpecIdentityVersion": 1,
                    "variants": [
                        {
                            "variantId": variant_id,
                            "replicaCount": target.replica_count,
                            "expectedAdmissionPodSpecSha256": "a" * 64,
                        }
                        for variant_id, target in orchestrate.TARGETS.items()
                    ],
                },
            )
            identity = runpod_identity()
            bundle = {
                "formatVersion": 1,
                "createdAt": "2026-07-31T00:00:00Z",
                "benchmarkGitRevision": SOURCE_REVISION,
                "matrixSha256": orchestrate.file_sha256(matrix_path),
                "scheduleSha256": orchestrate.file_sha256(schedule_path),
                "runtimeAdmissionIdentitiesSha256": orchestrate.file_sha256(
                    admission_path
                ),
                "controllerLockSha256": "b" * 64,
                "freezerSha256": orchestrate.file_sha256(orchestrate.FREEZER),
                "orchestratorSha256": orchestrate.file_sha256(
                    Path(orchestrate.__file__)
                ),
                "analyzerSha256": orchestrate.file_sha256(orchestrate.ANALYZER),
                "loadGenerator": {
                    "backend": "runpod-ssh",
                    "image": identity["runpod"]["image"],
                    "imageDigest": identity["runpod"]["imageDigest"],
                    "sourceRevision": SOURCE_REVISION,
                },
            }
            write_json(bundle_path, bundle)
            with patch.object(
                orchestrate,
                "validate_controller_lock",
                return_value="b" * 64,
            ):
                _, validated = orchestrate.validate_formal_bundle(
                    matrix_path,
                    schedule_path,
                    admission_path,
                    bundle_path,
                    SOURCE_REVISION,
                )
                self.assertEqual(validated["loadGenerator"], bundle["loadGenerator"])
                for mutation in ("extra", "source"):
                    changed = copy.deepcopy(bundle)
                    if mutation == "extra":
                        changed["unexpected"] = True
                    else:
                        changed["loadGenerator"]["sourceRevision"] = "b" * 40
                    write_json(bundle_path, changed)
                    with self.assertRaises(orchestrate.SafetyError):
                        orchestrate.validate_formal_bundle(
                            matrix_path,
                            schedule_path,
                            admission_path,
                            bundle_path,
                            SOURCE_REVISION,
                        )

    def test_source_s_mismatch_refuses_before_roots_or_kubernetes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_path = root / "identity.json"
            identity = runpod_identity()
            identity["sourceRevision"] = "b" * 40
            write_json(identity_path, identity)
            key_path = root / "id_ed25519"
            key_path.write_text("unused-before-source-s-gate\n", encoding="utf-8")
            key_path.chmod(0o600)
            run_root = root / "formal-runs" / RUN_ID
            matrix = {"benchmarkGitRevision": SOURCE_REVISION}
            bundle = {
                "loadGenerator": {
                    "backend": "runpod-ssh",
                    "image": runpod_identity()["runpod"]["image"],
                    "imageDigest": runpod_identity()["runpod"]["imageDigest"],
                    "sourceRevision": SOURCE_REVISION,
                }
            }
            arguments = [
                "preflight",
                "--run-root",
                str(run_root),
                "--controller-pod",
                "bluemap-perf-formal-controller-test",
                "--confirm",
                orchestrate.PREFLIGHT_CONFIRMATION,
                "--load-generator-backend",
                "runpod-ssh",
                "--load-generator-identity",
                str(identity_path),
                "--load-generator-identity-key",
                str(key_path),
                "--traffic-mode",
                "ssh-l4-traefik",
                "--traffic-base-url",
                "http://bluemap-test.guenter.cloud",
                "--traffic-service",
                orchestrate.TRAFFIC_SERVICE,
                "--traffic-service-port",
                str(orchestrate.TRAFFIC_SERVICE_PORT),
                "--formal-run-id",
                RUN_ID,
            ]
            with (
                patch.object(
                    orchestrate,
                    "validate_formal_documents",
                    return_value=(matrix, {"entries": []}),
                ),
                patch.object(
                    orchestrate,
                    "validate_formal_bundle",
                    return_value=({}, bundle),
                ),
                patch.object(orchestrate, "execute_preflight") as execute,
                patch.object(orchestrate, "Kubectl") as kubectl,
                patch.object(orchestrate, "require_new_preflight_root") as new_root,
                patch.object(orchestrate, "acquire_global_lock") as lock,
                patch("sys.stderr", new=io.StringIO()),
            ):
                self.assertEqual(orchestrate.main(arguments), 2)
            execute.assert_not_called()
            kubectl.assert_not_called()
            new_root.assert_not_called()
            lock.assert_not_called()
            self.assertFalse(run_root.exists())

    def test_source_s_mismatch_refuses_formal_before_preflight_or_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_path = root / "identity.json"
            identity = runpod_identity()
            identity["runpod"]["imageDigest"] = "sha256:" + "b" * 64
            identity["runpod"]["image"] = (
                "ghcr.io/jan-guenter/bluemap-perf-loadgen@"
                + identity["runpod"]["imageDigest"]
            )
            write_json(identity_path, identity)
            key_path = root / "id_ed25519"
            key_path.write_text("unused-before-source-s-gate\n", encoding="utf-8")
            key_path.chmod(0o600)
            run_root = root / "formal-runs" / RUN_ID
            matrix = {"benchmarkGitRevision": SOURCE_REVISION}
            expected = runpod_identity()
            bundle = {
                "loadGenerator": {
                    "backend": "runpod-ssh",
                    "image": expected["runpod"]["image"],
                    "imageDigest": expected["runpod"]["imageDigest"],
                    "sourceRevision": SOURCE_REVISION,
                }
            }
            arguments = [
                "run",
                "--run-root",
                str(run_root),
                "--preflight-report",
                str(root / "missing-preflight-report.json"),
                "--controller-pod",
                "bluemap-perf-formal-controller-test",
                "--confirm",
                orchestrate.CONFIRMATION,
                "--load-generator-backend",
                "runpod-ssh",
                "--load-generator-identity",
                str(identity_path),
                "--load-generator-identity-key",
                str(key_path),
                "--traffic-mode",
                "ssh-l4-traefik",
                "--traffic-base-url",
                "http://bluemap-test.guenter.cloud",
                "--traffic-service",
                orchestrate.TRAFFIC_SERVICE,
                "--traffic-service-port",
                str(orchestrate.TRAFFIC_SERVICE_PORT),
                "--formal-run-id",
                RUN_ID,
            ]
            with (
                patch.object(
                    orchestrate,
                    "validate_formal_documents",
                    return_value=(matrix, {"entries": []}),
                ),
                patch.object(
                    orchestrate,
                    "validate_formal_bundle",
                    return_value=({}, bundle),
                ),
                patch.object(orchestrate, "validate_preflight_report") as preflight,
                patch.object(orchestrate, "execute_schedule") as execute,
                patch.object(orchestrate, "Kubectl") as kubectl,
                patch.object(orchestrate, "validate_run_root") as validate_root,
                patch.object(orchestrate, "acquire_global_lock") as lock,
                patch("sys.stderr", new=io.StringIO()),
            ):
                self.assertEqual(orchestrate.main(arguments), 2)
            preflight.assert_not_called()
            execute.assert_not_called()
            kubectl.assert_not_called()
            validate_root.assert_not_called()
            lock.assert_not_called()
            self.assertFalse(run_root.exists())

    def test_runpod_controls_require_private_owned_key_and_public_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_path = root / "identity.json"
            key_path = root / "id_ed25519"
            write_json(identity_path, runpod_identity())
            key_path.write_text("private-test-key\n", encoding="utf-8")
            key_path.chmod(0o600)
            args = argparse.Namespace(
                load_generator_backend="runpod-ssh",
                load_generator_identity=identity_path,
                load_generator_identity_key=key_path,
                formal_run_id=RUN_ID,
                traffic_mode="cloudflare-https",
                traffic_base_url="https://bluemap-test.guenter.cloud",
                traffic_service=orchestrate.TRAFFIC_SERVICE,
                traffic_service_port=orchestrate.TRAFFIC_SERVICE_PORT,
                require_edge_bypass=True,
            )
            self.assertEqual(
                orchestrate.validate_runpod_controls(args),
                runpod_identity(),
            )

            non_exact = runpod_identity()
            non_exact["runpod"]["vcpuCount"] = 4
            write_json(identity_path, non_exact)
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "exact cpu5c/8-vCPU/500/100",
            ):
                orchestrate.validate_runpod_controls(args)
            write_json(identity_path, runpod_identity())

            key_path.chmod(0o640)
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "group or others",
            ):
                orchestrate.validate_runpod_controls(args)
            key_path.chmod(0o600)

            with patch.object(
                orchestrate.os,
                "getuid",
                return_value=os.getuid() + 1,
            ):
                with self.assertRaisesRegex(
                    orchestrate.SafetyError,
                    "owned by the controller user",
                ):
                    orchestrate.validate_runpod_controls(args)

            args.traffic_base_url = "http://127.0.0.1:8100"
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "Traffic base URL",
            ):
                orchestrate.validate_runpod_controls(args)

            args.traffic_mode = "ssh-l4-traefik"
            args.traffic_base_url = "https://bluemap-test.guenter.cloud"
            args.require_edge_bypass = False
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "Traffic base URL for ssh-l4-traefik",
            ):
                orchestrate.validate_runpod_controls(args)
            args.traffic_base_url = "http://bluemap-test.guenter.cloud"
            self.assertEqual(
                orchestrate.validate_runpod_controls(args),
                runpod_identity(),
            )
            args.require_edge_bypass = True
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "cannot claim an edge bypass",
            ):
                orchestrate.validate_runpod_controls(args)

    def test_execution_identity_is_canonical_and_never_archives_key_material(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_path = root / "identity.json"
            key_path = root / "id_ed25519"
            runner = root / "run_origin_case.sh"
            benchmark_python = root / "python"
            kubeconfig = root / "kubeconfig"
            write_json(identity_path, runpod_identity())
            key_path.write_text("never-archive-this-secret\n", encoding="utf-8")
            key_path.chmod(0o600)
            runner.write_text("#!/bin/sh\n", encoding="utf-8")
            benchmark_python.write_bytes(b"python-test-binary")
            kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
            args = argparse.Namespace(
                load_generator_backend="runpod-ssh",
                load_generator_identity=identity_path,
                load_generator_identity_key=key_path,
                formal_run_id=RUN_ID,
                traffic_mode="cloudflare-https",
                traffic_base_url="https://bluemap-test.guenter.cloud",
                traffic_service=orchestrate.TRAFFIC_SERVICE,
                traffic_service_port=orchestrate.TRAFFIC_SERVICE_PORT,
                require_edge_bypass=True,
                runner=runner,
                benchmark_python=benchmark_python,
                kubeconfig=kubeconfig,
                no_prometheus=False,
                prometheus_url=orchestrate.DEFAULT_PROMETHEUS_URL,
                transition_timeout_seconds=300,
                metrics_timeout_seconds=180,
                poll_interval_seconds=2.0,
            )
            identity = orchestrate.execution_identity(args)
            self.assertEqual(
                identity["loadGeneratorIdentitySha256"],
                analyze.canonical_sha256(identity["loadGeneratorIdentity"]),
            )
            self.assertEqual(identity, analyze.validate_execution_identity(identity))
            serialized = json.dumps(identity, sort_keys=True)
            self.assertNotIn("never-archive-this-secret", serialized)
            self.assertNotIn(str(key_path), serialized)
            self.assertNotIn("loadgenPod", serialized)
            self.assertEqual(
                identity["traffic"],
                {
                    "mode": "cloudflare-https",
                    "baseUrl": "https://bluemap-test.guenter.cloud",
                    "service": "bluemap-perf-public",
                    "port": 8100,
                    "requiresEdgeBypass": True,
                    "tunnel": None,
                },
            )

            tampered = copy.deepcopy(identity)
            tampered["loadGeneratorIdentity"]["runpod"]["machineId"] = "changed"
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "identity digest differs",
            ):
                analyze.validate_execution_identity(tampered)

            args.traffic_mode = "ssh-l4-traefik"
            args.traffic_base_url = "http://bluemap-test.guenter.cloud"
            args.require_edge_bypass = False
            tunnel_identity = orchestrate.execution_identity(args)
            self.assertEqual(
                tunnel_identity["traffic"]["tunnel"],
                orchestrate.SSH_L4_TRAEFIK_TUNNEL,
            )
            self.assertEqual(
                tunnel_identity,
                analyze.validate_execution_identity(tunnel_identity),
            )

            invalid_tunnel = copy.deepcopy(tunnel_identity)
            invalid_tunnel["traffic"]["tunnel"]["backends"][0][
                "targetPort"
            ] = 443
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "SSH L4 Traefik controls are invalid",
            ):
                analyze.validate_execution_identity(invalid_tunnel)

    def test_runner_command_uses_runpod_public_traffic_and_direct_origin(
        self,
    ) -> None:
        entry = schedule_entry()
        entry["overloadPolicy"] = "allow-explicit"
        target = orchestrate.TARGETS[entry["variantId"]]
        web_pods = [
            f"{target.deployment}-resolved-pod-{index}"
            for index in range(1, target.replica_count + 1)
        ]
        options = orchestrate.RunnerOptions(
            runner=Path(
                "/opt/bluemap/benchmarks/web-performance/tools/run_origin_case.sh"
            ),
            matrix=Path("/frozen/matrix.json"),
            schedule=Path("/frozen/schedule.json"),
            manifest=Path("/frozen/manifest.json"),
            artifact_root=Path("/artifacts/results"),
            benchmark_python=Path("/opt/venv/bin/python"),
            kubeconfig=Path("/opt/controller/kubeconfig"),
            prometheus_url=orchestrate.DEFAULT_PROMETHEUS_URL,
            load_generator_identity=Path("/identity/identity.json"),
            load_generator_identity_key=Path("/credentials/id_ed25519"),
            traffic_mode="cloudflare-https",
            traffic_base_url="https://bluemap-test.guenter.cloud",
            traffic_service=orchestrate.TRAFFIC_SERVICE,
            traffic_service_port=orchestrate.TRAFFIC_SERVICE_PORT,
            formal_run_id=RUN_ID,
            require_edge_bypass=True,
        )
        command = orchestrate.build_runner_command(
            entry,
            target,
            web_pods,
            options,
        )
        self.assertEqual(option(command, "--load-generator-backend"), "runpod-ssh")
        self.assertEqual(option(command, "--traffic-mode"), "cloudflare-https")
        self.assertEqual(
            option(command, "--traffic-base-url"),
            "https://bluemap-test.guenter.cloud",
        )
        self.assertEqual(
            option(command, "--origin-base-url"),
            (
                f"http://{target.service}.minecraft.svc.cluster.local:"
                f"{target.port}"
            ),
        )
        self.assertEqual(
            option(command, "--prometheus-url"),
            orchestrate.DEFAULT_PROMETHEUS_URL,
        )
        self.assertEqual(option(command, "--traffic-service"), "bluemap-perf-public")
        self.assertEqual(option(command, "--traffic-service-port"), "8100")
        self.assertEqual(option(command, "--formal-run-id"), RUN_ID)
        self.assertEqual(
            option(command, "--overload-policy"),
            "allow-explicit",
        )
        self.assertIn("--require-edge-bypass", command)
        self.assertEqual(
            [
                command[index + 1]
                for index, value in enumerate(command)
                if value == "--web-pod"
            ],
            web_pods,
        )
        joined = "\n".join(command)
        self.assertNotIn("--loadgen-pod", joined)
        self.assertNotIn("bluemap-perf-loadgen", joined)
        for protected in orchestrate.PROTECTED_RESOURCES:
            self.assertNotIn(protected, joined)

        tunnel_options = orchestrate.RunnerOptions(
            **{
                **options.__dict__,
                "traffic_mode": "ssh-l4-traefik",
                "traffic_base_url": "http://bluemap-test.guenter.cloud",
                "require_edge_bypass": False,
            }
        )
        tunnel_command = orchestrate.build_runner_command(
            entry,
            target,
            web_pods,
            tunnel_options,
        )
        self.assertEqual(
            option(tunnel_command, "--traffic-mode"),
            "ssh-l4-traefik",
        )
        self.assertEqual(
            option(tunnel_command, "--traffic-base-url"),
            "http://bluemap-test.guenter.cloud",
        )
        self.assertNotIn("--require-edge-bypass", tunnel_command)

    def test_static_target_validation_has_no_kubernetes_loadgen_resource(
        self,
    ) -> None:
        target = orchestrate.TARGETS["java-new-postgresql"]
        calls: list[tuple[str, str]] = []

        class FakeKube:
            @staticmethod
            def service(name: str) -> dict[str, object]:
                calls.append(("service", name))
                return {
                    "kind": "Service",
                    "metadata": {
                        "name": name,
                        "namespace": orchestrate.NAMESPACE,
                    },
                }

            @staticmethod
            def configmap(name: str) -> dict[str, object]:
                calls.append(("configmap", name))
                return {
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": name,
                        "namespace": orchestrate.NAMESPACE,
                    },
                }

            @staticmethod
            def pod(name: str) -> dict[str, object]:
                raise AssertionError(f"unexpected Kubernetes Pod lookup: {name}")

        orchestrate.validate_static_target_resources(FakeKube(), target)
        self.assertEqual(
            calls,
            [
                ("service", target.service),
                ("service", orchestrate.TRAFFIC_SERVICE),
                *(("configmap", name) for name in target.configmaps),
            ],
        )
        self.assertFalse(
            any("loadgen" in name for _kind, name in calls),
            calls,
        )


if __name__ == "__main__":
    unittest.main()
