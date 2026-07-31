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

    def test_capacity_gate_rejects_saturated_generator_even_when_reported_false(
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
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "failed its capacity gate",
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
