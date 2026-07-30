from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

BENCHMARK_ROOT = Path(__file__).parents[1]
TOOLS_DIR = BENCHMARK_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import capture_prometheus
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
        queries = capture_prometheus.build_queries("minecraft", targets)
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
                        output=output,
                        timeout=5,
                    )
                )
                bundle = json.loads(output.read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        self.assertGreaterEqual(len(requests), 10)
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


if __name__ == "__main__":
    unittest.main()
