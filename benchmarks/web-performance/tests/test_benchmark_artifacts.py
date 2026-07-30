from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

BENCHMARK_ROOT = Path(__file__).parents[1]
TOOLS_DIR = BENCHMARK_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import capture_prometheus
import check_arrival_gate
import configmap_references
import generate_schedule
import probe_delivery_cache
import sanitize_kubernetes_resource
import sanitize_configmap
import slow_reader


class KubernetesSnapshotTests(unittest.TestCase):
    def test_redacts_literal_credentials_but_preserves_references_and_digests(
        self,
    ) -> None:
        resource = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "bluemap-perf-web-abc",
                "namespace": "minecraft",
                "uid": "pod-uid",
                "annotations": {"example.invalid/password": "must-not-appear"},
            },
            "spec": {
                "containers": [
                    {
                        "name": "web",
                        "image": "example.invalid/web:test",
                        "args": [
                            "--database=password=literal-secret",
                            "https://user:literal-secret@example.invalid/",
                            "--token",
                            "second-literal-secret",
                        ],
                        "env": [
                            {"name": "PGPASSWORD", "value": "literal-secret"},
                            {"name": "PUBLIC_MODE", "value": "benchmark"},
                            {
                                "name": "DATABASE_PASSWORD",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": "bluemap-perf-database",
                                        "key": "password",
                                    }
                                },
                            },
                        ],
                    }
                ]
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": "web",
                        "image": "example.invalid/web:test",
                        "imageID": "example.invalid/web@sha256:0123",
                        "ready": True,
                        "restartCount": 2,
                    }
                ]
            },
        }

        result = sanitize_kubernetes_resource.snapshot(
            resource,
            "2026-07-30T00:00:00.000Z",
        )

        pod = result["resource"]
        self.assertNotIn("annotations", pod["metadata"])
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["env"][0]["value"], "<redacted>")
        self.assertEqual(container["env"][1]["value"], "benchmark")
        self.assertEqual(
            container["env"][2]["valueFrom"]["secretKeyRef"]["name"],
            "bluemap-perf-database",
        )
        self.assertNotIn("literal-secret", " ".join(container["args"]))
        self.assertNotIn("second-literal-secret", " ".join(container["args"]))
        self.assertEqual(
            pod["status"]["containerStatuses"][0]["imageID"],
            "example.invalid/web@sha256:0123",
        )
        self.assertEqual(
            pod["status"]["containerStatuses"][0]["restartCount"],
            2,
        )

    def test_refuses_secret_resources(self) -> None:
        with self.assertRaisesRegex(ValueError, "Refusing"):
            sanitize_kubernetes_resource.snapshot(
                {"apiVersion": "v1", "kind": "Secret", "metadata": {}},
                "2026-07-30T00:00:00.000Z",
            )


class ConfigMapSnapshotTests(unittest.TestCase):
    def test_preserves_non_secret_config_and_redacts_credentials(self) -> None:
        result = sanitize_configmap.snapshot(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "bluemap-perf-web", "namespace": "minecraft"},
                "data": {
                    "webserver.conf": (
                        "port = 8100\n"
                        "password = literal-secret\n"
                        "url = postgresql://user:literal-secret@db/bluemap\n"
                    ),
                    "client-secret": "literal-secret",
                },
            },
            "2026-07-30T00:00:00.000Z",
        )

        data = result["resource"]["data"]
        self.assertIn("port = 8100", data["webserver.conf"])
        self.assertNotIn("literal-secret", json.dumps(data))
        self.assertEqual(data["client-secret"], "<redacted>")

    def test_refuses_private_keys_and_binary_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "private-key"):
            sanitize_configmap.snapshot(
                {
                    "kind": "ConfigMap",
                    "metadata": {},
                    "data": {"tls.key": "-----BEGIN PRIVATE KEY-----\nvalue"},
                },
                "2026-07-30T00:00:00.000Z",
            )

        with self.assertRaisesRegex(ValueError, "binaryData"):
            sanitize_configmap.snapshot(
                {
                    "kind": "ConfigMap",
                    "metadata": {},
                    "binaryData": {"blob": "AA=="},
                },
                "2026-07-30T00:00:00.000Z",
            )


class PrometheusCaptureTests(unittest.TestCase):
    def test_inspects_cluster_service_url_without_credentials(self) -> None:
        result = capture_prometheus.inspect_url(
            "http://rancher-monitoring-prometheus." "cattle-monitoring-system.svc:9090"
        )

        self.assertEqual(
            result["clusterService"],
            {
                "service": "rancher-monitoring-prometheus",
                "namespace": "cattle-monitoring-system",
                "port": 9090,
                "path": "",
                },
            )

    def test_derives_standard_pod_configmap_references(self) -> None:
        resource = {
            "kind": "Deployment",
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [
                            {"configMap": {"name": "bluemap-perf-direct"}},
                            {
                                "projected": {
                                    "sources": [
                                        {
                                            "configMap": {
                                                "name": "bluemap-perf-projected"
                                            }
                                        },
                                        {"configMap": {"name": "kube-root-ca.crt"}},
                                    ]
                                }
                            },
                        ],
                        "initContainers": [
                            {
                                "envFrom": [
                                    {
                                        "configMapRef": {
                                            "name": "bluemap-perf-init-env"
                                        }
                                    }
                                ]
                            }
                        ],
                        "containers": [
                            {
                                "env": [
                                    {
                                        "valueFrom": {
                                            "configMapKeyRef": {
                                                "name": "bluemap-perf-key"
                                            }
                                        }
                                    }
                                ]
                            }
                        ],
                    }
                }
            },
        }

        self.assertEqual(
            configmap_references.references(resource),
            [
                "bluemap-perf-direct",
                "bluemap-perf-init-env",
                "bluemap-perf-key",
                "bluemap-perf-projected",
            ],
        )

        with self.assertRaisesRegex(ValueError, "must not contain credentials"):
            capture_prometheus.inspect_url("https://benchmark:secret@example.invalid")

    def test_queries_are_anchored_to_exact_selected_pods(self) -> None:
        targets = capture_prometheus.parse_pod_targets(
            [
                "loadgen=bluemap-perf-loadgen",
                "web=bluemap-perf-web.a",
                "database=bluemap-perf-postgres-0",
            ]
        )
        queries = capture_prometheus.build_queries(
            "minecraft",
            targets,
            ["contabo1", "contabo2"],
        )
        cpu_query = next(
            query["query"]
            for query in queries
            if query["name"] == "container_cpu_cores"
        )
        database_query = next(
            query["query"]
            for query in queries
            if query["name"] == "postgres_connections"
        )

        self.assertIn(
            r'pod=~"^(?:bluemap-perf-loadgen|'
            r'bluemap-perf-web\\.a|bluemap-perf-postgres-0)$"',
            cpu_query,
        )
        self.assertIn(
            r'pod=~"^(?:bluemap-perf-postgres-0)$"',
            database_query,
        )
        self.assertNotIn("bluemap-perf-web", database_query)
        node_cpu_query = next(
            query["query"]
            for query in queries
            if query["name"] == "node_non_target_container_cpu_cores"
        )
        node_disk_query = next(
            query["query"]
            for query in queries
            if query["name"] == "node_disk_read_bytes_rate"
        )
        self.assertIn(r'node=~"^(?:contabo1|contabo2)$"', node_cpu_query)
        self.assertIn(r'pod!~"^(?:bluemap-perf-loadgen|', node_cpu_query)
        self.assertIn(r'nodename=~"^(?:contabo1|contabo2)$"', node_disk_query)

    def test_captures_every_query_for_the_exact_requested_range(self) -> None:
        requests: list[dict[str, list[str]]] = []
        paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                paths.append(parsed.path)
                requests.append(parse_qs(parsed.query))
                body = json.dumps(
                    {
                        "status": "success",
                        "data": {"resultType": "matrix", "result": []},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "prometheus.json"
                phases = Path(directory) / "phases.ndjson"
                phases.write_text(
                    '{"timestamp":"2026-07-30T00:00:10Z","repetition":1,'
                    '"phase":"measurement","event":"start"}\n'
                    '{"timestamp":"2026-07-30T00:04:50Z","repetition":1,'
                    '"phase":"measurement","event":"end"}\n',
                    encoding="utf-8",
                )
                capture_prometheus.capture(
                    argparse.Namespace(
                        base_url=f"http://127.0.0.1:{server.server_port}",
                        source_url="http://prometheus.monitoring.svc:9090",
                        start=1_785_369_600,
                        end=1_785_369_900,
                        step=15,
                        namespace="minecraft",
                        pod=[
                            "loadgen=bluemap-perf-loadgen",
                            "web=bluemap-perf-web-abc",
                            "database=bluemap-perf-postgres-0",
                        ],
                        node=["contabo1"],
                        phase_events=phases,
                        max_non_target_node_cpu_range_cores=0.5,
                        max_non_target_node_cpu_mean_cores=2.0,
                        max_non_target_node_cpu_maximum_cores=3.0,
                        output=output,
                        timeout=5,
                    )
                )
                bundle = json.loads(output.read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        self.assertGreaterEqual(len(requests), 20)
        self.assertEqual(paths, ["/api/v1/query_range"] * len(requests))
        for request in requests:
            self.assertEqual(request["start"], ["1785369600"])
            self.assertEqual(request["end"], ["1785369900"])
            self.assertEqual(request["step"], ["15"])
        self.assertEqual(bundle["range"]["start"], 1_785_369_600)
        self.assertEqual(bundle["range"]["end"], 1_785_369_900)
        self.assertEqual(
            bundle["prometheus"]["baseUrl"],
            "http://prometheus.monitoring.svc:9090",
        )
        self.assertEqual(bundle["nodes"], ["contabo1"])
        self.assertFalse(bundle["nodeNoise"]["passed"])

    def test_node_noise_is_assessed_per_measurement_repetition(self) -> None:
        query_results = [
            {
                "name": "node_non_target_container_cpu_cores",
                "response": {
                    "data": {
                        "result": [
                            {
                                "metric": {"node": "contabo1"},
                                "values": [
                                    [10, "0.20"],
                                    [20, "0.25"],
                                    [110, "0.20"],
                                    [120, "1.10"],
                                    [210, "2.50"],
                                    [220, "2.60"],
                                ],
                            }
                        ]
                    }
                },
            }
        ]
        assessment = capture_prometheus.assess_node_noise(
            query_results,
            ["contabo1"],
            [
                {"repetition": 1, "start": 1, "end": 30},
                {"repetition": 2, "start": 100, "end": 130},
                {"repetition": 3, "start": 200, "end": 230},
            ],
            0.5,
            2.0,
            3.0,
        )

        self.assertEqual(assessment["noisyRepetitions"], [2, 3])
        self.assertFalse(assessment["passed"])


class OriginRunnerStaticTests(unittest.TestCase):
    def test_help_does_not_require_cluster_access(self) -> None:
        result = subprocess.run(
            ["bash", str(TOOLS_DIR / "run_origin_case.sh"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("bluemap-perf-loadgen", result.stdout)
        self.assertIn("--database-pod", result.stdout)
        self.assertIn("--prometheus-url", result.stdout)
        self.assertIn("--python", result.stdout)
        self.assertIn("--map-id", result.stdout)
        self.assertIn("--configmap", result.stdout)
        self.assertIn("--min-achieved-rate-ratio", result.stdout)
        self.assertIn("--trace-seed", result.stdout)
        self.assertIn("--latency-p95-ms", result.stdout)
        self.assertIn("--max-non-target-node-cpu-range-cores", result.stdout)
        self.assertIn("--schedule-entry", result.stdout)
        self.assertIn("--variant-id", result.stdout)
        self.assertIn("--implementation", result.stdout)
        self.assertIn("--storage-type", result.stdout)
        self.assertIn("--database-backend", result.stdout)
        runner = (TOOLS_DIR / "run_origin_case.sh").read_text(encoding="utf-8")
        self.assertIn("configmap_references.py", runner)
        self.assertIn("check_arrival_gate.py", runner)
        self.assertIn("DESIRED_WEB_REPLICA_COUNT", runner)

    def test_formal_schedule_is_seeded_balanced_and_tamper_evident(self) -> None:
        matrix_path = BENCHMARK_ROOT / "matrix.example.json"
        matrix = generate_schedule.load_json(matrix_path)
        digest = generate_schedule.matrix_sha256(matrix_path)
        first = generate_schedule.build_schedule(matrix, digest)
        second = generate_schedule.build_schedule(matrix, digest)

        self.assertEqual(first, second)
        generate_schedule.validate_schedule(matrix, digest, first)
        counts = {}
        for entry in first["entries"]:
            key = (
                entry["block"],
                entry["matrixCaseId"],
                entry["variantId"],
            )
            counts[key] = counts.get(key, 0) + 1
            variant = next(
                item
                for item in matrix["variants"]
                if item["id"] == entry["variantId"]
            )
            self.assertEqual(entry["implementation"], variant["implementation"])
            self.assertEqual(entry["storageType"], variant["storageType"])
            self.assertEqual(
                entry["databaseBackend"],
                variant["databaseBackend"],
            )
            self.assertEqual(entry["replicaCount"], variant["replicaCount"])
        self.assertTrue(all(count == 1 for count in counts.values()))

        for case in matrix["cases"]:
            for variant_id in case["variants"]:
                positions = Counter(
                    entry["ordinalWithinCase"]
                    for entry in first["entries"]
                    if entry["matrixCaseId"] == case["id"]
                    and entry["variantId"] == variant_id
                )
                counts_by_position = [
                    positions.get(position, 0)
                    for position in range(1, len(case["variants"]) + 1)
                ]
                self.assertLessEqual(
                    max(counts_by_position) - min(counts_by_position),
                    1,
                )

        tampered = json.loads(json.dumps(first))
        tampered["entries"][0]["variantId"] = "tampered"
        with self.assertRaisesRegex(ValueError, "does not exactly match"):
            generate_schedule.validate_schedule(matrix, digest, tampered)

    def test_k6_workload_has_formal_arrival_and_exact_status_gates(self) -> None:
        script = (BENCHMARK_ROOT / "k6" / "bluemap.js").read_text(encoding="utf-8")

        self.assertIn('dropped_iterations: ["count==0"]', script)
        self.assertIn("MIN_ACHIEVED_RATE_RATIO", script)
        self.assertIn('case "missing-tile":', script)
        self.assertIn('request(manifest.missingTile, "tile-missing", [204])', script)
        self.assertIn("conditional-seed", script)
        self.assertIn('recordStatus(response, [304])', script)
        self.assertIn("playerPolling", script)
        self.assertIn("markerPolling", script)
        self.assertIn("exec.scenario.iterationInTest", script)
        self.assertIn("TRACE_SEED", script)
        self.assertIn("http_req_duration", script)
        self.assertIn('"iterations{scenario:workload}"', script)
        self.assertIn('"iterations{scenario:playerPolling}"', script)
        self.assertIn('"iterations{scenario:markerPolling}"', script)
        self.assertIn("minimumIterationCountThreshold", script)
        self.assertNotRegex(script, r"iterations\s*:\s*\[\s*`rate>=")
        self.assertNotIn("Math.random()", script)

    def test_file_cases_do_not_require_a_database_target(self) -> None:
        runner = (TOOLS_DIR / "run_origin_case.sh").read_text(encoding="utf-8")

        self.assertNotIn("At least one --database-pod is required", runner)
        self.assertIn('json_array "${DATABASE_PODS[@]}"', runner)
        self.assertIn("sample_service_endpoints", runner)
        self.assertIn('if [[ "$phase" == */measurement ]]', runner)
        self.assertIn('$phase-end"', runner)

    def test_imports_use_disposable_snapshot_and_live_fixtures(self) -> None:
        imports = (
            BENCHMARK_ROOT / "kubernetes" / "import-jobs.yaml"
        ).read_text(encoding="utf-8")
        snapshot = (
            BENCHMARK_ROOT / "kubernetes" / "snapshot-copy.yaml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("claimName: minecraft-data", imports)
        self.assertIn("claimName: bluemap-perf-snapshot", imports)
        self.assertIn("--players-fixture /fixtures/players.json", imports)
        self.assertIn("claimName: minecraft-data", snapshot)
        self.assertIn("readOnly: true", snapshot)
        self.assertIn("claimName: bluemap-perf-snapshot", snapshot)

    def test_documented_slow_reader_uses_guarded_verified_expectations(
        self,
    ) -> None:
        readme = (BENCHMARK_ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## Graceful-drain slow-reader check", maxsplit=1)[1]

        self.assertIn("run_guarded_slow_reader.py", section)
        self.assertIn("--confirm-delete-pod \"$WEB_POD\"", section)
        self.assertIn("expected response headers/hash/length", section)


class SlowReaderTests(unittest.TestCase):
    def test_reads_and_hashes_the_complete_transferred_representation(self) -> None:
        body = b"BlueMap large response" * 4096

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Encoding", "zstd")
                self.send_header("ETag", '"test"')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = slow_reader.execute(
                    argparse.Namespace(
                        url=f"http://127.0.0.1:{server.server_port}/large",
                        bytes_per_second=100_000_000,
                        chunk_size=4096,
                        initial_delay_seconds=0,
                        timeout_seconds=5,
                        expected_status=200,
                        expected_sha256=hashlib.sha256(body).hexdigest(),
                        expected_length=len(body),
                        accept_encoding="zstd",
                        user_agent="BlueMap-Slow-Reader/test",
                        ready_file=root / "ready.json",
                        output=root / "result.json",
                    )
                )
                ready = json.loads((root / "ready.json").read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        self.assertTrue(result["complete"])
        self.assertEqual(result["bytesRead"], len(body))
        self.assertEqual(ready["contentLength"], len(body))

    def test_requires_an_independent_length_or_hash_expectation(self) -> None:
        with self.assertRaisesRegex(ValueError, "is required"):
            slow_reader.execute(
                argparse.Namespace(
                    url="http://127.0.0.1:1/large",
                    bytes_per_second=1024,
                    chunk_size=1024,
                    initial_delay_seconds=0,
                    timeout_seconds=1,
                    expected_status=200,
                    expected_sha256=None,
                    expected_length=None,
                    accept_encoding="zstd",
                    user_agent="BlueMap-Slow-Reader/test",
                    ready_file=Path("/tmp/unused-ready.json"),
                    output=Path("/tmp/unused-output.json"),
                )
            )


class ArrivalGateTests(unittest.TestCase):
    @staticmethod
    def summary(
        scenarios: dict[str, int],
        *,
        wall_clock_rate: float,
        dropped: int = 0,
    ) -> dict[str, object]:
        total = sum(scenarios.values())
        metrics: dict[str, object] = {
            "iterations": {
                "count": total,
                "rate": wall_clock_rate,
            },
            "dropped_iterations": {"count": dropped},
        }
        for scenario, count in scenarios.items():
            metrics[f"iterations{{scenario:{scenario}}}"] = {
                "count": count
            }
        return {"metrics": metrics}

    def test_slow_final_request_does_not_reduce_achieved_rate_gate(self) -> None:
        self.assertEqual(check_arrival_gate.duration_seconds("5m"), 300)
        result = check_arrival_gate.evaluate(
            self.summary({"workload": 301}, wall_clock_rate=9.74),
            profile="map-data-mixed",
            rate=10,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=True,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["scenarios"][0]["completedIterations"], 301)
        self.assertAlmostEqual(
            result["totals"][
                "achievedIterationsPerSecondOverConfiguredDuration"
            ],
            301 / 30,
        )
        self.assertEqual(
            result["totals"]["k6WallClockIterationsPerSecond"],
            9.74,
        )

    def test_incomplete_scheduled_count_still_fails(self) -> None:
        result = check_arrival_gate.evaluate(
            self.summary({"workload": 296}, wall_clock_rate=10),
            profile="map-data-mixed",
            rate=10,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=False,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["scenarios"][0]["passed"])

    def test_dropped_or_unattributed_iterations_still_fail(self) -> None:
        dropped = check_arrival_gate.evaluate(
            self.summary(
                {"workload": 301},
                wall_clock_rate=9.74,
                dropped=1,
            ),
            profile="map-data-mixed",
            rate=10,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=False,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )
        mismatched_summary = self.summary(
            {"workload": 301},
            wall_clock_rate=9.74,
        )
        mismatched_summary["metrics"]["iterations"]["count"] = 302
        mismatched = check_arrival_gate.evaluate(
            mismatched_summary,
            profile="map-data-mixed",
            rate=10,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=False,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )

        self.assertFalse(dropped["passed"])
        self.assertEqual(dropped["droppedIterations"], 1)
        self.assertFalse(mismatched["passed"])
        self.assertFalse(mismatched["scenarioCountsEqualOverall"])

    def test_live_viewer_scenarios_are_gated_independently(self) -> None:
        failed = check_arrival_gate.evaluate(
            self.summary(
                {
                    "playerPolling": 3001,
                    "markerPolling": 296,
                },
                wall_clock_rate=100,
            ),
            profile="live-viewers",
            rate=1,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=True,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )
        passed = check_arrival_gate.evaluate(
            self.summary(
                {
                    "playerPolling": 3001,
                    "markerPolling": 301,
                },
                wall_clock_rate=100,
            ),
            profile="live-viewers",
            rate=1,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=True,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )

        self.assertFalse(failed["passed"])
        self.assertFalse(failed["scenarios"][1]["passed"])
        self.assertTrue(passed["passed"])
        self.assertEqual(
            [scenario["scenario"] for scenario in passed["scenarios"]],
            ["playerPolling", "markerPolling"],
        )
        self.assertEqual(passed["scenarioCompletedIterations"], 3302)
        self.assertTrue(passed["scenarioCountsEqualOverall"])

    def test_accepts_nested_values_from_older_summary_shape(self) -> None:
        summary = self.summary({"workload": 301}, wall_clock_rate=9.74)
        summary["metrics"] = {
            name: {"values": values}
            for name, values in summary["metrics"].items()
        }

        result = check_arrival_gate.evaluate(
            summary,
            profile="map-data-mixed",
            rate=10,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=False,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )

        self.assertTrue(result["passed"])


class DeliveryCacheProbeTests(unittest.TestCase):
    def test_records_cold_warm_and_revalidated_delivery(self) -> None:
        request_counts: dict[str, int] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                request_counts[path] = request_counts.get(path, 0) + 1
                etag = f'"{hashlib.sha256(path.encode()).hexdigest()}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    body = b""
                else:
                    self.send_response(200)
                    body = path.encode()
                self.send_header("ETag", etag)
                self.send_header(
                    "Cache-Control",
                    (
                        "private,no-store,no-transform"
                        if path.endswith("players.json")
                        else "public,max-age=60,must-revalidate,no-transform"
                    ),
                )
                self.send_header(
                    "CF-Cache-Status",
                    (
                        "DYNAMIC"
                        if path.endswith("players.json")
                        else "MISS" if request_counts[path] == 1 else "HIT"
                    ),
                )
                if request_counts[path] > 1 and not path.endswith("players.json"):
                    self.send_header("Age", "1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = root / "manifest.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "hotTile": "/maps/world/tiles/0/x0z0.prbm",
                            "settings": ["/maps/world/settings.json"],
                            "markers": [],
                            "players": ["/maps/world/live/players.json"],
                        }
                    ),
                    encoding="utf-8",
                )
                result = probe_delivery_cache.execute(
                    argparse.Namespace(
                        base_url=f"http://127.0.0.1:{server.server_port}",
                        manifest=manifest,
                        probe_id="unit-test",
                        accept_encoding="zstd",
                        user_agent="BlueMap-Cache-Probe/test",
                        require_cloudflare_cache=True,
                        timeout=5,
                    )
                )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["targets"][0]["cold"]["cfCacheStatus"], "MISS")
        self.assertEqual(result["targets"][0]["warm"]["cfCacheStatus"], "HIT")
        self.assertEqual(result["targets"][0]["revalidated"]["status"], 304)

    def test_rejects_any_cached_player_response(self) -> None:
        def response(
            *,
            status: int = 200,
            body_sha: str = "body",
            transferred_bytes: int = 4,
            cache_status: str | None = "DYNAMIC",
            age: str | None = None,
            cache_control: str = "public,max-age=60",
        ) -> dict[str, object]:
            return {
                "status": status,
                "durationMilliseconds": 1.0,
                "transferredBytes": transferred_bytes,
                "transferredSha256": body_sha,
                "age": age,
                "cfCacheStatus": cache_status,
                "etag": '"stable"',
                "lastModified": None,
                "cacheControl": cache_control,
                "contentEncoding": "zstd",
                "contentLength": str(transferred_bytes),
            }

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "hotTile": "/maps/world/tiles/0/x0z0.prbm",
                        "settings": [],
                        "markers": [],
                        "players": ["/maps/world/live/players.json"],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                probe_delivery_cache,
                "request",
                side_effect=[
                    response(),
                    response(),
                    response(status=304, body_sha="empty", transferred_bytes=0),
                    response(cache_control="private,no-store"),
                    response(
                        cache_status="HIT",
                        cache_control="private,no-store",
                    ),
                    response(
                        status=304,
                        body_sha="empty",
                        transferred_bytes=0,
                        age="0",
                        cache_control="private,no-store",
                    ),
                ],
            ):
                result = probe_delivery_cache.execute(
                    argparse.Namespace(
                        base_url="http://example.invalid",
                        manifest=manifest,
                        probe_id="unit-test",
                        accept_encoding="zstd",
                        user_agent="BlueMap-Cache-Probe/test",
                        require_cloudflare_cache=False,
                        timeout=5,
                    )
                )

        self.assertFalse(result["passed"])
        self.assertIn(
            "players: CF-Cache-Status was HIT for warm",
            result["errors"],
        )
        self.assertIn(
            "players: Age was present for revalidated",
            result["errors"],
        )


if __name__ == "__main__":
    unittest.main()
