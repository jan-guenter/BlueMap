from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from support import (  # noqa: E402
    CAPACITY_LIMITS,
    RUN_ID,
    START_EPOCH,
    analyze,
    iso,
    runpod_identity,
    runpod_runtime_identity,
    runpod_samples,
    write_capacity_phase,
    write_json,
)


class RunPodAnalyzerTests(unittest.TestCase):
    def test_finished_phase_tracks_completed_repetition_count(self) -> None:
        self.assertTrue(
            analyze.is_finished_phase("repetition-00/finished", completed=0)
        )
        self.assertTrue(
            analyze.is_finished_phase("repetition-01/finished", completed=1)
        )
        self.assertFalse(
            analyze.is_finished_phase("repetition-00/finished", completed=1)
        )
        self.assertFalse(
            analyze.is_finished_phase("repetition-01/finished", completed=0)
        )

    def test_public_ingress_is_exact_and_stable(self) -> None:
        ingress = {
            "resource": {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": {
                    "name": "bluemap-perf-public",
                    "namespace": "minecraft",
                    "uid": "ingress-test-uid",
                    "labels": {
                        "app.kubernetes.io/part-of": "bluemap-web-performance",
                        "bluemap.guenter.cloud/experiment-id": "runpod-public-route",
                    },
                },
                "spec": {
                    "ingressClassName": "traefik",
                    "rules": [
                        {
                            "host": "bluemap-test.guenter.cloud",
                            "http": {
                                "paths": [
                                    {
                                        "backend": {
                                            "service": {
                                                "name": "bluemap-perf-public",
                                                "port": {"name": "http"},
                                            }
                                        },
                                        "path": "/",
                                        "pathType": "Prefix",
                                    }
                                ]
                            },
                        }
                    ],
                },
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            case_dir = Path(directory)
            for label in ("before", "after"):
                target = case_dir / "cluster" / label
                target.mkdir(parents=True)
                write_json(target / "ingress-bluemap-perf-public.json", ingress)
            self.assertEqual(
                analyze.validate_public_ingress(case_dir, "entry")["uid"],
                "ingress-test-uid",
            )
            changed = copy.deepcopy(ingress)
            changed["resource"]["spec"]["rules"][0]["host"] = "other.example"
            write_json(
                case_dir
                / "cluster"
                / "after"
                / "ingress-bluemap-perf-public.json",
                changed,
            )
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "does not exactly bind",
            ):
                analyze.validate_public_ingress(case_dir, "entry")

    def test_frozen_runpod_identity_is_exact_and_cross_field_bound(self) -> None:
        identity = runpod_identity()
        self.assertEqual(
            analyze.validate_runpod_identity(identity, "frozen generator"),
            identity,
        )

        extra = copy.deepcopy(identity)
        extra["runpod"]["unreviewedField"] = "accepted-by-accident"
        with self.assertRaisesRegex(
            analyze.AnalysisFailure,
            "runpod is malformed",
        ):
            analyze.validate_runpod_identity(extra, "frozen generator")

        wrong_host = copy.deepcopy(identity)
        wrong_host["ssh"]["host"] = "203.0.113.11"
        with self.assertRaisesRegex(
            analyze.AnalysisFailure,
            "invalid frozen controls|invalid",
        ):
            analyze.validate_runpod_identity(wrong_host, "frozen generator")

        wrong_image = copy.deepcopy(identity)
        wrong_image["runpod"]["imageDigest"] = "sha256:" + "b" * 64
        with self.assertRaisesRegex(
            analyze.AnalysisFailure,
            "image and digest differ",
        ):
            analyze.validate_runpod_identity(wrong_image, "frozen generator")

    def test_runtime_identity_must_match_the_frozen_runpod(self) -> None:
        frozen = runpod_identity()
        runtime = runpod_runtime_identity()
        self.assertEqual(
            analyze.validate_runpod_runtime_identity(
                runtime,
                frozen,
                "runtime generator",
            ),
            runtime,
        )
        runtime_v1 = runpod_runtime_identity()
        runtime_v1["runtime"]["cgroupVersion"] = 1
        runtime_v1["runtime"]["cpu"]["cgroupCpuMax"] = "max 100000"
        runtime_v1["runtime"]["cpu"]["quotaMicros"] = None
        runtime_v1["runtime"]["cpu"]["quotaVcpuCount"] = None
        runtime_v1["runtime"]["cpu"]["cpusetEffective"] = (
            "7-8,10,23,31-32,34,47"
        )
        runtime_v1["runtime"]["cpu"]["affinity"] = "7-8,10,23,31-32,34,47"
        self.assertEqual(
            analyze.validate_runpod_runtime_identity(
                runtime_v1,
                frozen,
                "runtime v1 generator",
            ),
            runtime_v1,
        )
        runtime["runpod"]["configuredVcpuCount"] = 4
        with self.assertRaisesRegex(
            analyze.AnalysisFailure,
            "differs from the frozen RunPod identity",
        ):
            analyze.validate_runpod_runtime_identity(
                runtime,
                frozen,
                "runtime generator",
            )

        runtime = runpod_runtime_identity()
        runtime["runtime"]["cpu"]["cgroupCpuMax"] = "400000 100000"
        runtime["runtime"]["cpu"]["quotaMicros"] = 400000
        runtime["runtime"]["cpu"]["quotaVcpuCount"] = 4
        runtime["runtime"]["cpu"]["effectiveVcpuCount"] = 4
        with self.assertRaisesRegex(
            analyze.AnalysisFailure,
            "independently observed CPU capacity is not exactly 8",
        ):
            analyze.validate_runpod_runtime_identity(
                runtime,
                frozen,
                "runtime generator",
            )

    def test_capacity_recomputes_from_raw_telemetry_and_passes(self) -> None:
        frozen = runpod_identity()
        runtime = runpod_runtime_identity()
        samples = runpod_samples()
        observed = analyze.recompute_runpod_capacity(samples, frozen, runtime)
        self.assertAlmostEqual(observed["cpuRatio"]["p95"], 0.05)
        self.assertAlmostEqual(
            observed["networkMbps"]["receiveP95Ratio"],
            0.032,
        )
        with tempfile.TemporaryDirectory() as directory:
            phase_dir = Path(directory) / "measurement"
            write_capacity_phase(
                phase_dir,
                samples,
                identity=frozen,
                runtime_identity=runtime,
            )
            evidence = analyze.validate_runpod_capacity_artifact(
                phase_dir,
                frozen,
                runtime,
                "measurement",
                (START_EPOCH, START_EPOCH + 10),
                10.0,
            )
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["limits"], CAPACITY_LIMITS)
        self.assertEqual(evidence["capacity"]["vcpuCount"], 8)
        self.assertEqual(evidence["observed"], observed)

    def test_capacity_uses_exact_subsecond_timestamp_differences(self) -> None:
        samples = runpod_samples()
        for sample, offset in zip(samples, (0.0, 1.011, 2.025), strict=True):
            sample["capturedAt"] = iso(offset)

        observed = analyze.recompute_runpod_capacity(
            samples,
            runpod_identity(),
            runpod_runtime_identity(),
        )

        self.assertEqual(observed["maximumSampleGapSeconds"], 1.014)

    def test_semantic_json_equality_accepts_integral_float_round_trip(self) -> None:
        python_identity = runpod_identity()
        python_identity["runpod"]["maxDownloadMbps"] = 1000.0
        python_identity["runpod"]["maxUploadMbps"] = 500.0
        jq_identity = copy.deepcopy(python_identity)
        jq_identity["runpod"]["maxDownloadMbps"] = 1000
        jq_identity["runpod"]["maxUploadMbps"] = 500

        self.assertNotEqual(
            analyze.canonical_sha256(python_identity),
            analyze.canonical_sha256(jq_identity),
        )
        self.assertTrue(analyze.equal_json_numbers(python_identity, jq_identity))

    def test_capacity_gate_reports_saturated_generator_as_failed_evidence(
        self,
    ) -> None:
        frozen = runpod_identity()
        runtime = runpod_runtime_identity()
        samples = runpod_samples(saturated=True)
        observed = analyze.recompute_runpod_capacity(samples, frozen, runtime)
        self.assertGreater(observed["cpuRatio"]["p95"], 0.70)
        with tempfile.TemporaryDirectory() as directory:
            phase_dir = Path(directory) / "measurement"
            write_capacity_phase(
                phase_dir,
                samples,
                identity=frozen,
                runtime_identity=runtime,
                passed=False,
            )
            evidence = analyze.validate_runpod_capacity_artifact(
                phase_dir,
                frozen,
                runtime,
                "measurement",
                (START_EPOCH, START_EPOCH + 10),
                10.0,
            )
        self.assertFalse(evidence["passed"])

    def test_capacity_rejects_reported_result_that_does_not_recompute(self) -> None:
        frozen = runpod_identity()
        runtime = runpod_runtime_identity()
        samples = runpod_samples(saturated=True)
        with tempfile.TemporaryDirectory() as directory:
            phase_dir = Path(directory) / "measurement"
            write_capacity_phase(
                phase_dir,
                samples,
                identity=frozen,
                runtime_identity=runtime,
                passed=True,
            )
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "reported RunPod capacity result does not recompute",
            ):
                analyze.validate_runpod_capacity_artifact(
                    phase_dir,
                    frozen,
                    runtime,
                    "measurement",
                    (START_EPOCH, START_EPOCH + 10),
                    10.0,
                )

    def test_capacity_rejects_post_hoc_telemetry_mutation(self) -> None:
        frozen = runpod_identity()
        runtime = runpod_runtime_identity()
        samples = runpod_samples()
        with tempfile.TemporaryDirectory() as directory:
            phase_dir = Path(directory) / "warmup"
            write_capacity_phase(
                phase_dir,
                samples,
                identity=frozen,
                runtime_identity=runtime,
            )
            mutated = copy.deepcopy(samples)
            mutated[-1]["network"]["rxBytes"] += 25_000_000
            (phase_dir / "load-generator-resources.ndjson").write_text(
                "".join(
                    json.dumps(sample, sort_keys=True) + "\n"
                    for sample in mutated
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "does not recompute",
            ):
                analyze.validate_runpod_capacity_artifact(
                    phase_dir,
                    frozen,
                    runtime,
                    "warmup",
                    (START_EPOCH, START_EPOCH + 10),
                    10.0,
                )

    def test_capacity_rejects_telemetry_without_phase_window_coverage(self) -> None:
        frozen = runpod_identity()
        runtime = runpod_runtime_identity()
        samples = runpod_samples()
        with tempfile.TemporaryDirectory() as directory:
            phase_dir = Path(directory) / "measurement"
            write_capacity_phase(
                phase_dir,
                samples,
                identity=frozen,
                runtime_identity=runtime,
            )
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "does not cover its phase window",
            ):
                analyze.validate_runpod_capacity_artifact(
                    phase_dir,
                    frozen,
                    runtime,
                    "measurement",
                    (START_EPOCH - 30, START_EPOCH - 20),
                    10.0,
                )

    def test_failed_warmup_may_omit_never_started_measurement_capacity(
        self,
    ) -> None:
        frozen = runpod_identity()
        runtime = runpod_runtime_identity()
        entry = {
            "entryId": "failed-warmup-entry",
            "warmupDuration": "10s",
            "measurementDuration": "10s",
        }
        timing = {
            "phaseWindows": {"warmup": (START_EPOCH, START_EPOCH + 10)}
        }
        with tempfile.TemporaryDirectory() as directory:
            repetition = Path(directory) / "repetitions" / "01"
            write_capacity_phase(
                repetition / "warmup",
                runpod_samples(),
                identity=frozen,
                runtime_identity=runtime,
            )
            evidence = analyze.validate_runpod_capacity_phases(
                repetition,
                frozen,
                runtime,
                entry,
                timing,
                "failed",
            )

        self.assertEqual(set(evidence), {"warmup"})
        self.assertTrue(evidence["warmup"]["passed"])

    def test_passed_result_requires_both_capacity_phases(self) -> None:
        entry = {
            "entryId": "incomplete-passed-entry",
            "warmupDuration": "10s",
            "measurementDuration": "10s",
        }
        timing = {
            "phaseWindows": {"warmup": (START_EPOCH, START_EPOCH + 10)}
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "passed result lacks RunPod capacity phase evidence: measurement",
            ):
                analyze.validate_runpod_capacity_phases(
                    Path(directory),
                    runpod_identity(),
                    runpod_runtime_identity(),
                    entry,
                    timing,
                    "passed",
                )

    def test_started_phase_still_requires_capacity_evidence(self) -> None:
        entry = {
            "entryId": "missing-started-phase-entry",
            "warmupDuration": "10s",
            "measurementDuration": "10s",
        }
        timing = {
            "phaseWindows": {
                "warmup": (START_EPOCH, START_EPOCH + 10),
                "measurement": (START_EPOCH + 10, START_EPOCH + 20),
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            repetition = Path(directory)
            write_capacity_phase(
                repetition / "warmup",
                runpod_samples(),
                identity=runpod_identity(),
                runtime_identity=runpod_runtime_identity(),
            )
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "RunPod resource telemetry is missing",
            ):
                analyze.validate_runpod_capacity_phases(
                    repetition,
                    runpod_identity(),
                    runpod_runtime_identity(),
                    entry,
                    timing,
                    "failed",
                )

    def test_capacity_continuity_accepts_missing_failed_phase_only(self) -> None:
        evidence = {
            "limits": CAPACITY_LIMITS,
            "capacity": {
                "vcpuCount": 8,
                "memoryCapacityBytes": 16_000_000_000,
                "minimumDownloadMbps": 500,
                "minimumUploadMbps": 100,
            },
            "passed": True,
        }
        failed_row = {
            "result": "failed",
            "controlIdentity": {
                "loadGeneratorCapacity": {"warmup": copy.deepcopy(evidence)}
            },
        }
        passed_row = {
            "result": "passed",
            "controlIdentity": {
                "loadGeneratorCapacity": {
                    "warmup": copy.deepcopy(evidence),
                    "measurement": copy.deepcopy(evidence),
                }
            },
        }

        continuity = analyze.summarize_runpod_capacity_control_continuity(
            [failed_row, passed_row]
        )

        self.assertEqual(set(continuity["controls"]), {"warmup", "measurement"})
        self.assertEqual(continuity["casePhaseEvidenceCount"], 3)
        self.assertTrue(continuity["allPassed"])

        failed_capacity = copy.deepcopy(passed_row)
        failed_capacity["result"] = "failed"
        failed_capacity["controlIdentity"]["loadGeneratorCapacity"][
            "measurement"
        ]["passed"] = False
        failed_continuity = (
            analyze.summarize_runpod_capacity_control_continuity(
                [failed_row, failed_capacity]
            )
        )
        self.assertFalse(failed_continuity["allPassed"])

        changed = copy.deepcopy(passed_row)
        changed["controlIdentity"]["loadGeneratorCapacity"]["measurement"][
            "capacity"
        ]["vcpuCount"] = 4
        with self.assertRaisesRegex(
            analyze.AnalysisFailure,
            "capacity controls changed",
        ):
            analyze.summarize_runpod_capacity_control_continuity(
                [failed_row, changed]
            )

        incomplete_passed = copy.deepcopy(failed_row)
        incomplete_passed["result"] = "passed"
        with self.assertRaisesRegex(
            analyze.AnalysisFailure,
            "passed result lacks complete",
        ):
            analyze.summarize_runpod_capacity_control_continuity(
                [incomplete_passed]
            )

    def test_block_noise_preserves_pre_block_metric_eligibility(self) -> None:
        rows = []
        for block in range(1, 6):
            eligibility = {
                "http": True,
                "webResource": block != 3,
                "webPrometheus": True,
            }
            rows.append(
                {
                    "entryId": f"case/variant/block-{block}",
                    "caseId": "case",
                    "variantId": "variant",
                    "block": block,
                    "eligibleForFormalComparison": True,
                    "metricEligibility": eligibility,
                    "failedGates": [],
                    "gates": {},
                    "metrics": {
                        "nodeNoise": {
                            "enabled": True,
                            "available": True,
                            "passed": block != 2,
                            "repetitions": [
                                {
                                    "nodes": [
                                        {
                                            "node": "node-a",
                                            "meanCores": 1.0,
                                            "maximumCores": 1.25,
                                        }
                                    ]
                                }
                            ],
                        }
                    },
                }
            )
        control_identity = {
            "nodes": ["node-a"],
            "observability": {
                "prometheus": {
                    "enabled": True,
                    "maximumNonTargetNodeCpuRangeCores": 0.5,
                }
            },
        }

        result = analyze.apply_block_noise_comparability(rows, control_identity)

        self.assertEqual(result["excludedCaseBlocks"], [{"caseId": "case", "block": 2}])
        self.assertEqual(
            rows[1]["preBlockMetricEligibility"],
            {"http": True, "webResource": True, "webPrometheus": True},
        )
        self.assertEqual(
            rows[1]["metricEligibility"],
            {"http": False, "webResource": False, "webPrometheus": False},
        )
        self.assertEqual(
            analyze.metric_eligibility_counts(
                rows, "preBlockMetricEligibility"
            ),
            {"http": 5, "webResource": 4, "webPrometheus": 5},
        )
        self.assertEqual(
            analyze.metric_eligibility_counts(rows, "metricEligibility"),
            {"http": 4, "webResource": 3, "webPrometheus": 4},
        )

    def test_kubernetes_sampler_rejects_legacy_loadgen_role(self) -> None:
        workload = {
            "workload": {
                "measurement": "10s",
                "metricsIntervalSeconds": 5,
            },
            "targets": {
                "webPods": ["bluemap-perf-rust-postgresql-abcde"],
                "databasePods": ["bluemap-perf-postgres-0"],
            },
            "formalSchedule": {
                "entry": {
                    "expectedImages": [
                        {
                            "kind": "container",
                            "name": "bluemap-web",
                            "digest": "sha256:" + "a" * 64,
                        }
                    ]
                }
            },
        }
        old_loadgen_sample = {
            "phase": "measurement",
            "role": "loadgen",
            "capturedAt": iso(),
            "metricTimestamp": iso(),
            "window": "5s",
            "pod": "bluemap-perf-loadgen-abcde",
            "expectedPod": "bluemap-perf-loadgen-abcde",
            "containers": [
                {"name": "k6", "cpu": "10m", "memory": "128Mi"}
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            sample_path = Path(directory) / "resource-usage.ndjson"
            sample_path.write_text(
                json.dumps(old_loadgen_sample) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "malformed resource sample",
            ):
                analyze.summarize_resource_samples(
                    sample_path,
                    "measurement",
                    workload,
                    (START_EPOCH, START_EPOCH + 10),
                )


if __name__ == "__main__":
    unittest.main()
