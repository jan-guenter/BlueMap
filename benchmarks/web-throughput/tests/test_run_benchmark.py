from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_benchmark as benchmark


def setup_manifest(snapshot_id: str = "snapshot-1") -> dict:
    limits = {"cpu": "2 cores", "memory": "2 GiB"}
    return {
        "formatVersion": 1,
        "environment": "test host",
        "protocol": "HTTP/1.1",
        "database": {
            "snapshotId": snapshot_id,
            "aggregateConnectionCeiling": 12,
        },
        "resourceLimits": {
            variant: dict(limits) for variant in benchmark.VARIANTS
        },
        "targets": {
            variant: {"runtime": f"{variant}-runtime", "configuration": "config"}
            for variant in benchmark.VARIANTS
        },
    }


def measurement(variant: str, repetition: int, **overrides) -> dict:
    value = {
        "phase": "measurement",
        "variant": variant,
        "repetition": repetition,
        "orderPosition": 1,
        "status": "completed",
        "requests": 100,
        "requestsPerSecond": 10.0,
        "mibPerSecond": 1.0,
        "p95Milliseconds": 20.0,
        "httpFailureRate": 0.0,
        "errors": 0,
        "warmupExitCode": 0,
        "k6ExitCode": 0,
    }
    value.update(overrides)
    return value


def start_test_server(body: bytes) -> tuple[ThreadingHTTPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/maps/world/settings.json":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Encoding", "zstd")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class BenchmarkHelpersTest(unittest.TestCase):

    def test_parse_paths_accepts_comments_and_freezes_order(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "paths.txt"
            source.write_text(
                "# comment\n\n/maps/world/settings.json\n/maps/world/tiles/0/x1z2\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [
                    "/maps/world/settings.json",
                    "/maps/world/tiles/0/x1z2",
                ],
                benchmark.parse_paths(source),
            )

    def test_parse_paths_rejects_non_map_query_and_duplicate(self):
        invalid_values = [
            "/index.html\n",
            "/maps/world/settings.json?fresh=1\n",
            "/maps/world/../private.json\n",
            "/maps/world/%2e%2e/private.json\n",
            "/maps/world/%2Fprivate.json\n",
            "/maps/world//settings.json\n",
            "/maps/world/settings.json\n/maps/world/settings.json\n",
        ]
        for value in invalid_values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "paths.txt"
                source.write_text(value, encoding="utf-8")
                with self.assertRaises(benchmark.BenchmarkError):
                    benchmark.parse_paths(source)

    def test_rotated_targets_cycles_order(self):
        targets = [
            benchmark.Target("a", "http://a", "a"),
            benchmark.Target("b", "http://b", "b"),
            benchmark.Target("c", "http://c", "c"),
        ]
        self.assertEqual(["a", "b", "c"], [t.name for t in benchmark.rotated_targets(targets, 1)])
        self.assertEqual(["b", "c", "a"], [t.name for t in benchmark.rotated_targets(targets, 2)])
        self.assertEqual(["c", "a", "b"], [t.name for t in benchmark.rotated_targets(targets, 3)])

    def test_extract_metrics_reads_k6_summary(self):
        summary = {
            "metrics": {
                "http_reqs": {"values": {"count": 1200, "rate": 100.0}},
                "data_received": {
                    "values": {"count": 12 * 1024 * 1024, "rate": 1024 * 1024}
                },
                "http_req_duration": {"values": {"p(95)": 42.5}},
                "http_req_failed": {"values": {"rate": 0.0}},
                "benchmark_errors": {"values": {"count": 0}},
            }
        }
        self.assertEqual(
            {
                "requests": 1200,
                "requestsPerSecond": 100.0,
                "mibPerSecond": 1.0,
                "p95Milliseconds": 42.5,
                "httpFailureRate": 0.0,
                "errors": 0,
            },
            benchmark.extract_metrics(summary),
        )

    def test_extract_metrics_rejects_missing_or_invalid_values(self):
        valid = {
            "metrics": {
                "http_reqs": {"values": {"count": 1, "rate": 1.0}},
                "data_received": {"values": {"rate": 1.0}},
                "http_req_duration": {"values": {"p(95)": 1.0}},
                "http_req_failed": {"values": {"rate": 0.0}},
                "benchmark_errors": {"values": {"count": 0}},
            }
        }
        invalid = []
        missing_errors = json.loads(json.dumps(valid))
        del missing_errors["metrics"]["benchmark_errors"]
        invalid.append(missing_errors)
        zero_requests = json.loads(json.dumps(valid))
        zero_requests["metrics"]["http_reqs"]["values"]["count"] = 0
        invalid.append(zero_requests)
        nonfinite_rate = json.loads(json.dumps(valid))
        nonfinite_rate["metrics"]["http_reqs"]["values"]["rate"] = math.nan
        invalid.append(nonfinite_rate)
        bad_failure_rate = json.loads(json.dumps(valid))
        bad_failure_rate["metrics"]["http_req_failed"]["values"]["rate"] = 1.1
        invalid.append(bad_failure_rate)
        for summary in invalid:
            with self.subTest(summary=summary), self.assertRaises(
                benchmark.BenchmarkError
            ):
                benchmark.extract_metrics(summary)

    def test_run_k6_preserves_raw_summary_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_k6 = root / "k6"
            fake_k6.write_text(
                """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

summary_path = Path(sys.argv[sys.argv.index("--summary-export") + 1])
summary_path.write_text(json.dumps({
    "metrics": {
        "http_reqs": {"values": {"count": 120, "rate": 12.0}},
        "data_received": {"values": {"count": 10485760, "rate": 1048576}},
        "http_req_duration": {"values": {"p(95)": 25.0}},
        "http_req_failed": {"values": {"rate": 0.0}},
        "benchmark_errors": {"values": {"count": 0}}
    }
}))
print("fake k6 output")
""",
                encoding="utf-8",
            )
            fake_k6.chmod(0o755)
            paths = root / "paths.txt"
            paths.write_text("/maps/world/settings.json\n", encoding="utf-8")
            summary = root / "summary.json"
            log = root / "run.log"
            exit_code, metrics = benchmark.run_k6(
                k6_binary=str(fake_k6),
                script=Path(__file__),
                target=benchmark.Target("upstream", "http://example.test", "id"),
                path_file=paths,
                vus=1,
                duration="1s",
                accept_encoding="zstd",
                required_content_encoding="zstd",
                summary_path=summary,
                log_path=log,
            )
            self.assertEqual(0, exit_code)
            self.assertEqual(12.0, metrics["requestsPerSecond"])
            self.assertTrue(summary.is_file())
            self.assertIn("fake k6 output", log.read_text(encoding="utf-8"))

    def test_summary_retains_failed_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runs = [measurement("upstream", 1, httpFailureRate=0.1, errors=1)]
            self.assertFalse(benchmark.summarize_measurements(output, runs, 1))
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["valid"])
            self.assertEqual(1, summary["variants"][0]["failedRuns"])
            self.assertNotIn("medianRequestsPerSecond", summary["variants"][0])
            self.assertIn("No aggregate metrics", (output / "SUMMARY.md").read_text())

    def test_summary_aggregates_only_a_complete_valid_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runs = [
                measurement(variant, repetition, requestsPerSecond=10.0 + repetition)
                for repetition in (1, 2)
                for variant in benchmark.VARIANTS
            ]
            self.assertTrue(benchmark.summarize_measurements(output, runs, 2))
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["valid"])
            self.assertEqual(11.5, summary["variants"][0]["medianRequestsPerSecond"])

    def test_validate_url_rejects_credentials_and_query(self):
        for value in (
            "http://user:secret@example.test",
            "http://example.test/?x=1",
            "http://example.test/base",
            "http://example.test\n",
        ):
            with self.subTest(value=value), self.assertRaises(benchmark.BenchmarkError):
                benchmark.validate_url(value, "target")

    def test_setup_manifest_is_strict_and_bound_to_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.json"
            path.write_text(json.dumps(setup_manifest()), encoding="utf-8")
            parsed, raw = benchmark.load_setup_manifest(path, "snapshot-1")
            self.assertEqual("snapshot-1", parsed["database"]["snapshotId"])
            self.assertEqual(path.read_bytes(), raw)
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.load_setup_manifest(path, "different-snapshot")

            unequal = setup_manifest()
            unequal["resourceLimits"]["new-java"]["cpu"] = "4 cores"
            path.write_text(json.dumps(unequal), encoding="utf-8")
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.load_setup_manifest(path, "snapshot-1")

    def test_preflight_accepts_only_byte_identical_targets(self):
        servers_and_threads = [start_test_server(b"same-raw-body") for _ in range(3)]
        try:
            targets = [
                benchmark.Target(
                    name,
                    f"http://127.0.0.1:{server.server_address[1]}",
                    name,
                )
                for name, (server, _) in zip(
                    ("upstream", "upstream-php", "new-java"),
                    servers_and_threads,
                    strict=True,
                )
            ]
            evidence = benchmark.preflight(
                targets,
                ["/maps/world/settings.json"],
                "zstd",
                "zstd",
                2,
            )
            path_evidence = evidence["paths"]["/maps/world/settings.json"]
            self.assertEqual(3, len(path_evidence))
            self.assertEqual(
                1,
                len({entry["sha256"] for entry in path_evidence.values()}),
            )
        finally:
            for server, thread in servers_and_threads:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_preflight_rejects_different_raw_body(self):
        servers_and_threads = [
            start_test_server(body)
            for body in (b"reference", b"reference", b"different")
        ]
        try:
            targets = [
                benchmark.Target(
                    name,
                    f"http://127.0.0.1:{server.server_address[1]}",
                    name,
                )
                for name, (server, _) in zip(
                    ("upstream", "upstream-php", "new-java"),
                    servers_and_threads,
                    strict=True,
                )
            ]
            with tempfile.TemporaryDirectory() as directory:
                evidence_path = Path(directory) / "preflight.json"
                with self.assertRaises(benchmark.BenchmarkError):
                    benchmark.preflight(
                        targets,
                        ["/maps/world/settings.json"],
                        "zstd",
                        "zstd",
                        2,
                        evidence_path=evidence_path,
                    )
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                self.assertEqual("failed", evidence["status"])
                self.assertFalse(evidence["valid"])
                failed = evidence["paths"]["/maps/world/settings.json"]["new-java"]
                self.assertFalse(failed["valid"])
                self.assertIn("body differs", failed["error"])
        finally:
            for server, thread in servers_and_threads:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_main_preserves_terminal_evidence_on_preflight_failure(self):
        servers_and_threads = [
            start_test_server(body)
            for body in (b"reference", b"reference", b"different")
        ]
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fake_k6 = root / "k6"
                fake_k6.write_text(
                    "#!/bin/sh\n[ \"$1\" = version ] && echo 'k6 v1.0.0' && exit 0\nexit 99\n",
                    encoding="utf-8",
                )
                fake_k6.chmod(0o755)
                paths = root / "paths.txt"
                paths.write_text("/maps/world/settings.json\n", encoding="utf-8")
                setup = root / "setup.json"
                setup.write_text(json.dumps(setup_manifest()), encoding="utf-8")
                output = root / "results"
                urls = [
                    f"http://127.0.0.1:{server.server_address[1]}"
                    for server, _ in servers_and_threads
                ]
                result = benchmark.main(
                    [
                        "--upstream-url",
                        urls[0],
                        "--upstream-php-url",
                        urls[1],
                        "--new-java-url",
                        urls[2],
                        "--upstream-id",
                        "upstream-id",
                        "--new-java-id",
                        "new-java-id",
                        "--dataset-id",
                        "snapshot-1",
                        "--setup-manifest",
                        str(setup),
                        "--paths",
                        str(paths),
                        "--output",
                        str(output),
                        "--k6",
                        str(fake_k6),
                    ]
                )
                self.assertEqual(2, result)
                terminal = json.loads((output / "terminal.json").read_text())
                metadata = json.loads((output / "metadata.json").read_text())
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual("failed", terminal["status"])
                self.assertFalse(metadata["valid"])
                self.assertFalse(summary["valid"])
                self.assertTrue((output / "setup-manifest.json").is_file())
        finally:
            for server, thread in servers_and_threads:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
