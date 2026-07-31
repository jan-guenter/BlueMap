from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from support import (  # noqa: E402
    CAPACITY_LIMITS,
    RUN_ID,
    START_EPOCH,
    analyze,
    formal_matrix as build_formal_matrix,
    iso,
    orchestrate,
    runpod_identity,
    runpod_runtime_identity,
    runpod_samples,
    schedule_entry,
    write_capacity_phase,
    write_json,
)


class RunPodAnalyzerTests(unittest.TestCase):
    @staticmethod
    def proof_manifest() -> dict[str, object]:
        return {
            "tiles": [
                "/maps/world/tiles/0/x0/z0.prbm",
                "/maps/world/tiles/1/x0/z0.prbm",
            ],
            "textures": ["/maps/world/textures.json"],
            "hotTile": "/maps/world/tiles/0/x0/z0.prbm",
            "largeTile": "/maps/world/tiles/0/x0/z0.prbm",
            "largeObject": "/maps/world/textures.json",
        }

    @staticmethod
    def complete_summary() -> dict[str, object]:
        return {
            "metrics": {
                "http_req_failed{traffic:workload}": {"value": 0},
                "bluemap_unexpected_status": {"value": 0},
                "bluemap_prohibited_edge_header": {
                    "value": 0,
                    "passes": 0,
                    "fails": 100,
                },
                "bluemap_stored_content_encoding_violation": {
                    "value": 0,
                    "passes": 0,
                    "fails": 80,
                },
                "iterations": {"values": {"count": 100, "rate": 10}},
                "dropped_iterations": {"values": {"count": 0}},
                "http_reqs": {"values": {"count": 100}},
                "data_received": {"values": {"count": 1_000}},
                "data_sent": {"values": {"count": 500}},
                "http_req_duration{traffic:workload}": {
                    "values": {
                        "med": 1,
                        "p(90)": 2,
                        "p(95)": 3,
                        "p(99)": 4,
                    }
                },
                "bluemap_ttfb": {
                    "values": {
                        "med": 1,
                        "p(90)": 2,
                        "p(95)": 3,
                        "p(99)": 4,
                    }
                },
            }
        }

    def test_analyzer_source_s_control_is_strict_and_execution_bound(self) -> None:
        identity = runpod_identity()
        control = {
            "backend": "runpod-ssh",
            "image": identity["runpod"]["image"],
            "imageDigest": identity["runpod"]["imageDigest"],
            "sourceRevision": identity["sourceRevision"],
        }
        validated = analyze.validate_load_generator_control(
            control,
            identity["sourceRevision"],
        )
        self.assertEqual(
            analyze.validate_load_generator_execution_binding(
                validated,
                identity,
            ),
            analyze.canonical_sha256(control),
        )
        for invalid in (
            {**control, "sourceRevision": "0" * 40},
            {**control, "sourceRevision": control["sourceRevision"] + "\n"},
            {**control, "unexpected": True},
        ):
            with self.assertRaises(analyze.AnalysisFailure):
                analyze.validate_load_generator_control(
                    invalid,
                    identity["sourceRevision"],
                )
        mismatched = copy.deepcopy(identity)
        mismatched["sourceRevision"] = "b" * 40
        with self.assertRaisesRegex(
            analyze.AnalysisFailure,
            "differs from execution identity",
        ):
            analyze.validate_load_generator_execution_binding(
                control,
                mismatched,
            )

    def test_preflight_attestation_is_required_and_hash_bound(self) -> None:
        formal_matrix = build_formal_matrix()
        with tempfile.TemporaryDirectory() as directory:
            formal_root = (
                Path(directory) / "artifacts" / "formal-runs" / RUN_ID
            ).resolve()
            preflight_root = formal_root.with_name(f"{RUN_ID}-preflight")
            inputs = preflight_root / "inputs"
            inputs.mkdir(parents=True)
            preflight_matrix_path = inputs / "matrix.json"
            preflight_schedule_path = inputs / "schedule.json"
            write_json(
                preflight_matrix_path,
                orchestrate.derive_preflight_matrix(formal_matrix),
            )
            orchestrate.run_checked(
                [
                    sys.executable,
                    str(orchestrate.DEFAULT_GENERATOR),
                    "generate",
                    str(preflight_matrix_path),
                    str(preflight_schedule_path),
                ],
                cwd=orchestrate.REPOSITORY_ROOT,
            )
            schedule = analyze.load_object(preflight_schedule_path)
            write_json(inputs / "provenance.json", {"formatVersion": 1})
            orchestrate.write_sha256s(
                inputs, ("matrix.json", "schedule.json", "provenance.json")
            )
            derived = orchestrate.preflight_derived_hashes(inputs)
            relay_path = preflight_root / "observability" / "relay-headroom.json"
            relay_identity = {
                "formatVersion": 1,
                "namespace": "minecraft",
                "pod": "bluemap-perf-formal-controller-test",
                "podUid": "controller-uid",
                "formalRunId": RUN_ID,
                "container": "controller",
                "serviceAccountName": "bluemap-perf-formal-controller",
                "requiredLabels": {
                    "app.kubernetes.io/name": "bluemap-perf-formal-controller",
                    "app.kubernetes.io/part-of": "bluemap-web-performance",
                    "bluemap.guenter.cloud/experiment-id": RUN_ID,
                },
                "owner": {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": "bluemap-perf-formal-controller",
                    "uid": "controller-job-uid",
                },
                "limits": {
                    "cpuCores": 2.0,
                    "memoryBytes": float(2 * 1024**3),
                },
                "source": "metrics.k8s.io/v1beta1",
            }
            write_json(
                relay_path.parent / "relay-identity.json", relay_identity
            )
            write_json(
                relay_path.parent / "relay-readiness.json",
                {
                    "formatVersion": 1,
                    "startedAt": iso(),
                    "completedAt": iso(11.1),
                    "timeoutSeconds": 180,
                    "pollIntervalSeconds": 2.0,
                    "attempts": 2,
                    "transientErrors": [
                        {"failedAt": iso(0.25), "error": "initial 404"}
                    ],
                    "ready": True,
                },
            )
            (relay_path.parent / "relay-errors.ndjson").write_text(
                "", encoding="utf-8"
            )
            relay_samples = []
            for index in range(6):
                source_offset = 10.0 + index * 10.0
                cpu_ratio = 0.10 + index * 0.02
                relay_samples.append(
                    {
                        "requestedAt": iso(source_offset + 0.5),
                        "fetchedAt": iso(source_offset + 1.0),
                        "metricsTimestamp": iso(source_offset),
                        "window": "28.454s",
                        "pod": relay_identity["pod"],
                        "podUid": relay_identity["podUid"],
                        "container": "controller",
                        "cpuCores": cpu_ratio * 2.0,
                        "memoryBytes": 0.2 * float(2 * 1024**3),
                        "cpuLimitRatio": cpu_ratio,
                        "memoryLimitRatio": 0.2,
                        "metricAgeSeconds": 1.0,
                    }
                )
            (relay_path.parent / "relay-samples.ndjson").write_text(
                "".join(json.dumps(sample, sort_keys=True) + "\n" for sample in relay_samples),
                encoding="utf-8",
            )
            relay = {
                "formatVersion": 1,
                "passed": True,
                "startedAt": iso(11),
                "stoppedAt": iso(70),
                "namespace": "minecraft",
                "pod": "bluemap-perf-formal-controller-test",
                "podUid": "controller-uid",
                "container": "controller",
                "source": "metrics.k8s.io/v1beta1",
                "limits": {
                    "cpuCores": 2.0,
                    "memoryBytes": float(2 * 1024**3),
                },
                "thresholds": {
                    "p95CpuLimitRatio": 0.70,
                    "maximumCpuLimitRatio": 0.90,
                    "maximumMemoryLimitRatio": 0.80,
                    "minimumUniqueMetricTimestamps": 6,
                    "maximumUniqueMetricTimestampGapSeconds": 30.0,
                    "maximumMetricAgeSeconds": 60.0,
                    "maximumCoverageGapSeconds": 60.0,
                },
                "checks": {
                    key: True
                    for key in (
                        "noMetricsApiErrors",
                        "minimumUniqueMetricTimestamps",
                        "maximumUniqueMetricTimestampGapSeconds",
                        "maximumMetricAgeSeconds",
                        "initialCoverageGapSeconds",
                        "finalCoverageGapSeconds",
                        "p95CpuLimitRatio",
                        "maximumCpuLimitRatio",
                        "maximumMemoryLimitRatio",
                    )
                },
                "observed": {
                    "successfulFetches": 6,
                    "errors": 0,
                    "uniqueMetricTimestamps": 6,
                    "metricWindows": ["28.454s"],
                    "maximumUniqueMetricTimestampGapSeconds": 10.0,
                    "maximumMetricAgeSeconds": 1.0,
                    "initialCoverageGapSeconds": 1.0,
                    "finalCoverageGapSeconds": 10.0,
                    "p95CpuLimitRatio": 0.2,
                    "maximumCpuLimitRatio": 0.2,
                    "maximumMemoryLimitRatio": 0.2,
                },
                "limitation": (
                    "metrics.k8s.io exposes coarse aggregate "
                    "controller-container CPU and memory only; it cannot "
                    "attribute usage to the SSH relay process or prove "
                    "bandwidth and CPU-throttling headroom"
                ),
            }
            write_json(relay_path, relay)
            matrix_digest = "1" * 64
            schedule_digest = "2" * 64
            admission_digest = "3" * 64
            bundle_digest = "4" * 64
            orchestrator_digest = "5" * 64
            traffic = {
                "mode": "ssh-l4-traefik",
                "baseUrl": "http://bluemap-test.guenter.cloud",
                "service": "bluemap-perf-public",
                "port": 8100,
                "requiresEdgeBypass": False,
                "tunnel": orchestrate.SSH_L4_TRAEFIK_TUNNEL,
            }
            loadgen_digest = "6" * 64
            loadgen = runpod_identity()
            loadgen_digest = analyze.canonical_sha256(loadgen)
            load_generator_control = {
                "backend": "runpod-ssh",
                "image": loadgen["runpod"]["image"],
                "imageDigest": loadgen["runpod"]["imageDigest"],
                "sourceRevision": loadgen["sourceRevision"],
            }
            load_generator_sha256 = analyze.canonical_sha256(
                load_generator_control
            )
            execution = {
                "formatVersion": 1,
                "namespace": "minecraft",
                "databasePod": "bluemap-perf-postgres-0",
                "loadGeneratorBackend": "runpod-ssh",
                "loadGeneratorIdentity": loadgen,
                "loadGeneratorIdentitySha256": loadgen_digest,
                "formalRunId": RUN_ID,
                "traffic": traffic,
                "runner": "/runner",
                "runnerSha256": "7" * 64,
                "benchmarkPython": "/python",
                "benchmarkPythonSha256": "8" * 64,
                "kubeconfig": "/kubeconfig",
                "kubeconfigSha256": "9" * 64,
                "prometheus": {
                    "enabled": True,
                    "url": (
                        "http://rancher-monitoring-prometheus."
                        "cattle-monitoring-system.svc:9090"
                    ),
                },
                "transitionTimeoutSeconds": 300,
                "metricsTimeoutSeconds": 180,
                "pollIntervalSeconds": 2.0,
            }
            expected_admission = {
                variant_id: "a" * 64
                for variant_id in (
                    "java-new-postgresql",
                    "rust-postgresql",
                    "java-new-postgresql-r3",
                    "rust-postgresql-r3",
                )
            }
            state_entries = {}
            lifecycle_events = []
            results = preflight_root / "results"
            logs = preflight_root / "logs"
            results.mkdir()
            logs.mkdir()
            for entry in schedule["entries"]:
                sequence = entry["sequence"]
                started = 12 + sequence * 3
                pods = [
                    f"bluemap-perf-{entry['variantId']}-pod-{index}"
                    for index in range(1, entry["replicaCount"] + 1)
                ]
                state_entries[str(sequence)] = {
                    "status": "completed",
                    "entryId": entry["entryId"],
                    "runnerCaseId": entry["runnerCaseId"],
                    "variantId": entry["variantId"],
                    "startedAt": iso(started),
                    "runnerStartedAt": iso(started + 1),
                    "completedAt": iso(started + 2),
                    "webPods": pods,
                    "admissionPodSpecIdentity": {
                        "expected": expected_admission[entry["variantId"]],
                        "actual": {
                            pod: expected_admission[entry["variantId"]]
                            for pod in pods
                        },
                    },
                    "result": "passed",
                    "runnerExitStatus": 0,
                }
                cooldown = {
                    "requiredSeconds": entry["cooldownSeconds"],
                    "runnerSatisfied": True,
                    "orchestratorWaitedSeconds": 0.01,
                    "waitStartedAt": iso(started + 1.5),
                    "completedAt": iso(started + 1.7),
                }
                state_entries[str(sequence)]["interEntryCooldown"] = cooldown
                case_dir = results / entry["runnerCaseId"]
                case_dir.mkdir()
                write_json(case_dir / "result.json", {"result": "passed"})
                (logs / f"{sequence:03d}-{entry['runnerCaseId']}.log").write_text(
                    "passed\n", encoding="utf-8"
                )
                lifecycle_events.extend(
                    [
                        {
                            "timestamp": iso(started + 0.1),
                            "sequence": sequence,
                            "event": "activation-start",
                            "entryId": entry["entryId"],
                        },
                        {
                            "timestamp": iso(started + 1.1),
                            "sequence": sequence,
                            "event": "runner-started",
                            "entryId": entry["entryId"],
                            "webPods": pods,
                        },
                        {
                            "timestamp": iso(started + 1.8),
                            "sequence": sequence,
                            "event": "inter-entry-cooldown-completed",
                            "entryId": entry["entryId"],
                            **cooldown,
                        },
                        {
                            "timestamp": iso(started + 2.1),
                            "sequence": sequence,
                            "event": "runner-completed",
                            "entryId": entry["entryId"],
                            "result": "passed",
                            "exitStatus": 0,
                        },
                    ]
                )
            state = {
                "formatVersion": 1,
                "createdAt": iso(12),
                "updatedAt": iso(34),
                "completedAt": iso(34),
                "status": "completed",
                "nextSequence": 7,
                "matrixSha256": derived["matrixSha256"],
                "scheduleSha256": derived["scheduleSha256"],
                "manifestSha256": formal_matrix["manifestSha256"],
                "runtimeAdmissionIdentitiesSha256": admission_digest,
                "bundleManifestSha256": bundle_digest,
                "orchestratorSha256": orchestrator_digest,
                "analyzerSha256": "b" * 64,
                "benchmarkGitRevision": formal_matrix["benchmarkGitRevision"],
                "loadGeneratorSha256": load_generator_sha256,
                "executionIdentity": execution,
                "entries": state_entries,
            }
            write_json(preflight_root / "state.json", state)
            (preflight_root / "events.ndjson").write_text(
                "".join(json.dumps(event, sort_keys=True) + "\n" for event in lifecycle_events),
                encoding="utf-8",
            )
            formal_root.mkdir(parents=True)
            write_json(
                formal_root / "state.json",
                {"createdAt": iso(81), "analyzerSha256": "b" * 64},
            )
            report = {
                "formatVersion": 1,
                "kind": "ssh-l4-traefik-preflight",
                "passed": True,
                "completedAt": iso(80),
                "formalRunId": RUN_ID,
                "benchmarkGitRevision": formal_matrix[
                    "benchmarkGitRevision"
                ],
                "sourceFormalInputs": {
                    "matrixSha256": matrix_digest,
                    "scheduleSha256": schedule_digest,
                    "runtimeAdmissionIdentitiesSha256": admission_digest,
                    "bundleManifestSha256": bundle_digest,
                    "manifestSha256": formal_matrix["manifestSha256"],
                },
                "derivedInputs": derived,
                "traffic": traffic,
                "loadGeneratorIdentitySha256": loadgen_digest,
                "loadGeneratorSha256": load_generator_sha256,
                "orchestratorSha256": orchestrator_digest,
                "generatorSha256": analyze.sha256_file(
                    orchestrate.DEFAULT_GENERATOR
                ),
                "controllerRelay": {
                    "pod": relay["pod"],
                    "podUid": relay["podUid"],
                    "headroomSha256": analyze.sha256_file(relay_path),
                    "passed": True,
                },
                "traefikPrometheus": {
                    "formatVersion": 1,
                    "available": False,
                    "gating": False,
                    "metric": "traefik_service_requests_total",
                    "serviceLabelRegex": (
                        r"^minecraft-bluemap-perf-public-(?:http|8100)@kubernetes$"
                    ),
                    "reason": (
                        "The configured rancher-monitoring Prometheus has no "
                        "Traefik series. Traefik's separate three-replica "
                        "metrics Service load-balances one endpoint per "
                        "scrape, so a complete counter delta cannot be proven "
                        "without expanding scope. Exact k6 status/error checks "
                        "remain the request-scoped 5xx gate."
                    ),
                },
                "entries": [
                    {
                        "sequence": entry["sequence"],
                        "entryId": entry["entryId"],
                        "runnerCaseId": entry["runnerCaseId"],
                        "variantId": entry["variantId"],
                        "status": "completed",
                        "result": "passed",
                        "runnerExitStatus": 0,
                    }
                    for entry in schedule["entries"]
                ],
                "failures": [],
            }
            evidence_path = preflight_root / "preflight-evidence.json"
            write_json(
                evidence_path,
                analyze.preflight_evidence_inventory(preflight_root),
            )
            report["evidenceManifestSha256"] = analyze.sha256_file(evidence_path)
            report_path = preflight_root / "preflight-report.json"
            write_json(report_path, report)
            attestation = {
                "formatVersion": 1,
                "report": f"../{RUN_ID}-preflight/preflight-report.json",
                "reportSha256": analyze.sha256_file(report_path),
                "matrixSha256": report["derivedInputs"]["matrixSha256"],
                "scheduleSha256": report["derivedInputs"]["scheduleSha256"],
                "evidenceManifestSha256": analyze.sha256_file(evidence_path),
                "controllerPod": relay["pod"],
                "controllerPodUid": relay["podUid"],
                "traffic": traffic,
                "loadGeneratorSha256": load_generator_sha256,
            }
            def replay(*args: object, **kwargs: object) -> dict[str, object]:
                entry = args[1]
                assert isinstance(entry, dict)
                return {
                    "sequence": entry["sequence"],
                    "result": "passed",
                    "eligibleForFormalComparison": True,
                    "timing": {
                        "resultStartedEpoch": START_EPOCH
                        + 12
                        + entry["sequence"] * 3
                        + 1.2,
                        "resultCompletedEpoch": START_EPOCH
                        + 12
                        + entry["sequence"] * 3
                        + 1.4,
                        "runnerCooldownSatisfied": True,
                    },
                    "metrics": {
                        "transportProof": {
                            "mode": "ssh-l4-traefik",
                            "passed": True,
                        }
                    },
                }

            with patch.object(analyze, "analyze_case", side_effect=replay):
                validated = analyze.validate_preflight_attestation(
                    attestation,
                    run_root=formal_root,
                    matrix=formal_matrix,
                    matrix_digest=matrix_digest,
                    schedule_digest=schedule_digest,
                    admission_digest=admission_digest,
                    bundle_digest=bundle_digest,
                    orchestrator_digest=orchestrator_digest,
                    load_generator_sha256=load_generator_sha256,
                    execution_identity=execution,
                    expected_admission=expected_admission,
                )
                self.assertTrue(validated["validated"])
                self.assertTrue(
                    validated["semanticReplay"]["rawRelayRecomputed"]
                )

                relocated_parent = (
                    Path(directory) / "relocated" / "formal-runs"
                )
                relocated_parent.parent.mkdir(parents=True)
                formal_root.parent.rename(relocated_parent)
                formal_root = relocated_parent / RUN_ID
                preflight_root = relocated_parent / f"{RUN_ID}-preflight"
                report_path = preflight_root / "preflight-report.json"
                relocated = analyze.validate_preflight_attestation(
                    attestation,
                    run_root=formal_root,
                    matrix=formal_matrix,
                    matrix_digest=matrix_digest,
                    schedule_digest=schedule_digest,
                    admission_digest=admission_digest,
                    bundle_digest=bundle_digest,
                    orchestrator_digest=orchestrator_digest,
                    load_generator_sha256=load_generator_sha256,
                    execution_identity=execution,
                    expected_admission=expected_admission,
                )
                self.assertTrue(relocated["validated"])

                samples_path = (
                    preflight_root / "observability" / "relay-samples.ndjson"
                )
                original_samples = samples_path.read_text(encoding="utf-8")
                aliased_samples = [
                    json.loads(line) for line in original_samples.splitlines()
                ]
                aliased_samples[-1]["metricsTimestamp"] = aliased_samples[-2][
                    "metricsTimestamp"
                ].replace("Z", "+00:00")
                aliased_samples[-1]["metricAgeSeconds"] = 11.0
                samples_path.write_text(
                    "".join(
                        json.dumps(sample, sort_keys=True) + "\n"
                        for sample in aliased_samples
                    ),
                    encoding="utf-8",
                )
                evidence_path = preflight_root / "preflight-evidence.json"
                write_json(
                    evidence_path,
                    analyze.preflight_evidence_inventory(preflight_root),
                )
                report["evidenceManifestSha256"] = analyze.sha256_file(
                    evidence_path
                )
                write_json(report_path, report)
                attestation["evidenceManifestSha256"] = analyze.sha256_file(
                    evidence_path
                )
                attestation["reportSha256"] = analyze.sha256_file(report_path)
                with self.assertRaisesRegex(
                    analyze.AnalysisFailure,
                    "uniqueMetricTimestamps was not recomputed",
                ):
                    analyze.validate_preflight_attestation(
                        attestation,
                        run_root=formal_root,
                        matrix=formal_matrix,
                        matrix_digest=matrix_digest,
                        schedule_digest=schedule_digest,
                        admission_digest=admission_digest,
                        bundle_digest=bundle_digest,
                        orchestrator_digest=orchestrator_digest,
                        load_generator_sha256=load_generator_sha256,
                        execution_identity=execution,
                        expected_admission=expected_admission,
                    )
                samples_path.write_text(original_samples, encoding="utf-8")
                write_json(
                    evidence_path,
                    analyze.preflight_evidence_inventory(preflight_root),
                )
                report["evidenceManifestSha256"] = analyze.sha256_file(
                    evidence_path
                )
                write_json(report_path, report)
                attestation["evidenceManifestSha256"] = analyze.sha256_file(
                    evidence_path
                )
                attestation["reportSha256"] = analyze.sha256_file(report_path)

                report["passed"] = False
                write_json(report_path, report)
                attestation["reportSha256"] = analyze.sha256_file(report_path)
                with self.assertRaisesRegex(
                    analyze.AnalysisFailure,
                    "identity or gate result differs",
                ):
                    analyze.validate_preflight_attestation(
                        attestation,
                        run_root=formal_root,
                        matrix=formal_matrix,
                        matrix_digest=matrix_digest,
                        schedule_digest=schedule_digest,
                        admission_digest=admission_digest,
                        bundle_digest=bundle_digest,
                        orchestrator_digest=orchestrator_digest,
                        load_generator_sha256=load_generator_sha256,
                        execution_identity=execution,
                        expected_admission=expected_admission,
                    )

    def test_direct_transport_proof_metrics_are_mode_and_contract_gated(
        self,
    ) -> None:
        enhanced = {**schedule_entry(), "profile": "large-object"}
        manifest = self.proof_manifest()
        summary = self.complete_summary()

        self.assertEqual(
            analyze.direct_transport_metrics(
                enhanced, "ssh-l4-traefik", manifest
            ),
            (
                "bluemap_prohibited_edge_header",
                "bluemap_stored_content_encoding_violation",
            ),
        )
        analyze.validate_required_measurement_metrics(
            summary, enhanced, "ssh-l4-traefik", manifest
        )
        analyze.validate_status_metrics(
            summary, enhanced, "measurement", "ssh-l4-traefik", manifest
        )

        for metric, proof_name in (
            ("bluemap_prohibited_edge_header", "prohibitedEdgeHeaders"),
            (
                "bluemap_stored_content_encoding_violation",
                "storedContentEncoding",
            ),
        ):
            missing = copy.deepcopy(summary)
            del missing["metrics"][metric]
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                rf"direct transport proof failed: {proof_name}",
            ):
                analyze.validate_required_measurement_metrics(
                    missing, enhanced, "ssh-l4-traefik", manifest
                )

            violated = copy.deepcopy(summary)
            violated["metrics"][metric]["value"] = 0.01
            violated["metrics"][metric]["passes"] = 1
            violated["metrics"][metric]["fails"] = 99
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                rf"direct transport proof failed: {proof_name}",
            ):
                analyze.validate_status_metrics(
                    violated,
                    enhanced,
                    "measurement",
                    "ssh-l4-traefik",
                    manifest,
                )

        legacy = {**enhanced, "contractMode": "legacy"}
        legacy_summary = copy.deepcopy(summary)
        del legacy_summary["metrics"][
            "bluemap_stored_content_encoding_violation"
        ]
        self.assertEqual(
            analyze.direct_transport_metrics(
                legacy, "ssh-l4-traefik", manifest
            ),
            ("bluemap_prohibited_edge_header",),
        )
        analyze.validate_required_measurement_metrics(
            legacy_summary, legacy, "ssh-l4-traefik", manifest
        )
        analyze.validate_status_metrics(
            legacy_summary,
            legacy,
            "measurement",
            "ssh-l4-traefik",
            manifest,
        )

        cloudflare_summary = copy.deepcopy(summary)
        del cloudflare_summary["metrics"]["bluemap_prohibited_edge_header"]
        del cloudflare_summary["metrics"][
            "bluemap_stored_content_encoding_violation"
        ]
        self.assertEqual(
            analyze.direct_transport_metrics(
                enhanced, "cloudflare-https", manifest
            ),
            (),
        )
        analyze.validate_required_measurement_metrics(
            cloudflare_summary, enhanced, "cloudflare-https", manifest
        )
        analyze.validate_status_metrics(
            cloudflare_summary,
            enhanced,
            "measurement",
            "cloudflare-https",
            manifest,
        )

    def test_stored_encoding_proof_requires_samples_only_when_applicable(
        self,
    ) -> None:
        manifest = self.proof_manifest()
        enhanced = {**schedule_entry(), "profile": "large-object"}
        no_samples = self.complete_summary()
        no_samples["metrics"][
            "bluemap_stored_content_encoding_violation"
        ] = {"value": 0, "passes": 0, "fails": 0}

        proof = analyze.transport_phase_proof(
            no_samples, enhanced, "ssh-l4-traefik", manifest
        )
        self.assertFalse(proof["storedContentEncoding"]["passed"])
        self.assertEqual(proof["storedContentEncoding"]["samples"], 0)
        self.assertFalse(proof["passed"])
        self.assertFalse(
            analyze.measurement_metrics_available(
                no_samples, enhanced, "ssh-l4-traefik", manifest
            )
        )

        live_viewers = {**enhanced, "profile": "live-viewers"}
        live_summary = copy.deepcopy(no_samples)
        del live_summary["metrics"][
            "bluemap_stored_content_encoding_violation"
        ]
        live_proof = analyze.transport_phase_proof(
            live_summary, live_viewers, "ssh-l4-traefik", manifest
        )
        self.assertTrue(live_proof["passed"])
        self.assertEqual(
            live_proof["storedContentEncoding"],
            {
                "applicable": False,
                "samples": None,
                "passes": None,
                "fails": None,
                "violationRate": None,
                "passed": None,
            },
        )
        self.assertEqual(
            analyze.direct_transport_metrics(
                live_viewers, "ssh-l4-traefik", manifest
            ),
            ("bluemap_prohibited_edge_header",),
        )
        self.assertTrue(
            analyze.measurement_metrics_available(
                live_summary, live_viewers, "ssh-l4-traefik", manifest
            )
        )

    def test_stored_compression_routes_are_manifest_scoped(self) -> None:
        manifest = self.proof_manifest()
        self.assertTrue(
            analyze.is_stored_compressed_route(
                "/maps/world/tiles/0/x0/z0.prbm", manifest
            )
        )
        self.assertTrue(
            analyze.is_stored_compressed_route(
                "/maps/world/textures.json", manifest
            )
        )
        self.assertFalse(
            analyze.is_stored_compressed_route(
                "/maps/world/tiles/1/x0/z0.prbm", manifest
            )
        )
        self.assertFalse(
            analyze.is_stored_compressed_route(
                "/not-a-manifest-route/tiles/0/x0/z0.prbm", manifest
            )
        )

    def test_failed_transport_proof_is_unavailable_without_becoming_malformed(
        self,
    ) -> None:
        manifest = self.proof_manifest()
        enhanced = {**schedule_entry(), "profile": "large-object"}
        violated = self.complete_summary()
        violated["metrics"][
            "bluemap_stored_content_encoding_violation"
        ] = {"value": 0.01, "passes": 1, "fails": 99}

        proof = analyze.transport_phase_proof(
            violated, enhanced, "ssh-l4-traefik", manifest
        )
        self.assertFalse(proof["passed"])
        self.assertEqual(
            proof["storedContentEncoding"]["violationRate"], 0.01
        )
        self.assertEqual(proof["storedContentEncoding"]["samples"], 100)
        self.assertFalse(
            analyze.measurement_metrics_available(
                violated, enhanced, "ssh-l4-traefik", manifest
            )
        )

    def test_traffic_modes_have_exact_mode_gated_identities(self) -> None:
        cloudflare = {
            "mode": "cloudflare-https",
            "baseUrl": "https://bluemap-test.guenter.cloud",
            "service": "bluemap-perf-public",
            "port": 8100,
            "requiresEdgeBypass": True,
            "tunnel": None,
        }
        self.assertEqual(
            analyze.validate_traffic_identity(cloudflare, "traffic"),
            cloudflare,
        )

        tunneled = {
            **cloudflare,
            "mode": "ssh-l4-traefik",
            "baseUrl": "http://bluemap-test.guenter.cloud",
            "requiresEdgeBypass": False,
            "tunnel": {
                "listenHost": "127.0.0.1",
                "listenPort": 18080,
                "targetHost": "rke2-traefik.kube-system.svc.cluster.local",
                "targetPort": 80,
            },
            "formalRunId": RUN_ID,
        }
        self.assertEqual(
            analyze.validate_traffic_identity(
                tunneled,
                "traffic",
                formal_run_id=RUN_ID,
            ),
            tunneled,
        )

        invalid = copy.deepcopy(tunneled)
        invalid["baseUrl"] = "https://bluemap-test.guenter.cloud"
        with self.assertRaisesRegex(analyze.AnalysisFailure, "route is invalid"):
            analyze.validate_traffic_identity(
                invalid,
                "traffic",
                formal_run_id=RUN_ID,
            )

        invalid = copy.deepcopy(tunneled)
        invalid["requiresEdgeBypass"] = True
        with self.assertRaisesRegex(
            analyze.AnalysisFailure,
            "SSH L4 Traefik controls are invalid",
        ):
            analyze.validate_traffic_identity(
                invalid,
                "traffic",
                formal_run_id=RUN_ID,
            )

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
        runtime["sourceRevision"] = "b" * 40
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
