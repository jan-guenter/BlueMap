from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_benchmark as benchmark


def setup_manifest(snapshot_id: str = "snapshot-1") -> dict:
    limits = {"cpu": "4 logical CPUs", "memory": "8 GiB"}
    return {
        "formatVersion": benchmark.FORMAT_VERSION,
        "environment": "test RunPod host",
        "protocol": "HTTP/1.1 direct origin",
        "directOrigin": True,
        "runpod": {"region": "EU-NL-1", "topology": "five isolated Pods"},
        "database": {
            "snapshotId": snapshot_id,
            "engine": "MariaDB",
            "version": "mariadb@sha256:test",
            "aggregateConnectionCeiling": 12,
            "tls": {
                "required": True,
                "verified": True,
                "serverName": "mariadb.test",
                "caSha256": "a" * 64,
            },
        },
        "resourceLimits": {
            variant: dict(limits) for variant in benchmark.VARIANTS
        },
        "targets": {
            variant: {
                "runtime": f"{variant}-runtime",
                "configuration": "config-sha256",
                "runtimeIdentity": f"{variant}-identity",
                "runpodPodId": f"{variant}-pod",
                "imageDigest": "sha256:" + "b" * 64,
                "processIdentitySha256": "c" * 64,
                "runtimeProbeSha256": "d" * 64,
                "configurationSha256": "e" * 64,
                "identityHeader": "X-BlueMap-Benchmark-Runtime",
                "uploadBitsPerSecond": 1_000_000_000,
            }
            for variant in benchmark.VARIANTS
        },
        "loadGenerator": {
            "runtime": "loadgen@sha256:test",
            "configuration": "fixed-vus",
            "hardware": {
                "podId": "pod-1",
                "podType": "cpu5c",
                "cpuModel": "test cpu",
                "logicalCpuCount": 8,
                "memoryBytes": 16 * 1024 * 1024 * 1024,
                "downloadBitsPerSecond": 1_000_000_000,
            },
            "admission": {
                "maximumCpuUtilizationPercent": 90,
                "maximumMemoryUtilizationPercent": 90,
                "minimumSamples": 1,
                "minimumFreeDiskBytes": 1,
            },
        },
    }


def k6_summary(requests: int = 1) -> dict:
    metrics = {
        "http_reqs": {"values": {"count": requests, "rate": float(requests)}},
        "data_received": {"values": {"count": 1048576, "rate": 1048576.0}},
        "http_req_duration": {
            "values": {"med": 10.0, "p(95)": 20.0, "p(99)": 30.0}
        },
        "http_req_failed": {"values": {"rate": 0.0}},
        "iterations": {"values": {"count": 1}},
        "checks": {"values": {"rate": 1.0}},
    }
    for name in (
        "benchmark_errors",
        "benchmark_http_errors",
        "benchmark_transport_errors",
        "benchmark_proxy_header_errors",
        "benchmark_encoding_errors",
        "benchmark_content_type_errors",
        "benchmark_content_length_errors",
        "benchmark_body_length_errors",
        "benchmark_identity_errors",
        "benchmark_cache_validator_errors",
        "benchmark_dropped_iterations",
    ):
        metrics[name] = {"values": {"count": 0}}
    metrics["benchmark_observed_responses"] = {"values": {"count": requests}}
    metrics["benchmark_stored_representation_bytes"] = {
        "values": {"count": requests * 1024, "rate": float(requests * 1024)}
    }
    return {"metrics": metrics}


def measurement(variant: str, repetition: int, order_position: int, **overrides) -> dict:
    value = {
        "phase": "measurement",
        "variant": variant,
        "profileId": "profile-1",
        "repetition": repetition,
        "orderPosition": order_position,
        "status": "completed",
        "requests": 100,
        "networkReceivedBytes": 1024,
        "storedRepresentationBytes": 1024,
        "iterations": 10,
        "requestsPerSecond": 10.0,
        "networkMibPerSecond": 1.1,
        "storedMibPerSecond": 1.0,
        "p50Milliseconds": 10.0,
        "p95Milliseconds": 20.0,
        "p99Milliseconds": 30.0,
        "httpFailureRate": 0.0,
        "checkFailureRate": 0.0,
        "httpFailures": 0,
        "errors": 0,
        "httpErrors": 0,
        "transportErrors": 0,
        "proxyHeaderErrors": 0,
        "encodingErrors": 0,
        "contentTypeErrors": 0,
        "contentLengthErrors": 0,
        "bodyLengthErrors": 0,
        "identityErrors": 0,
        "cacheValidatorErrors": 0,
        "droppedIterations": 0,
        "k6DroppedIterations": 0,
        "observedResponses": 100,
        "summaryEvidenceValid": True,
        "summaryEvidenceSha256": "f" * 64,
        "summaryEvidenceBytes": 1024,
        "loadGeneratorEvidenceValid": True,
        "loadGeneratorSaturated": False,
        "loadGeneratorSampleCount": 108,
        "loadGeneratorMaximumCpuPercent": 20.0,
        "loadGeneratorMaximumMemoryPercent": 10.0,
        "loadGeneratorP95NetworkReceiveBytesPerSecond": 1000.0,
        "loadGeneratorP95NetworkTransmitBytesPerSecond": 100.0,
        "loadGeneratorTimingEvidenceValid": True,
        "loadGeneratorExpectedPhaseDurationSeconds": 120.0,
        "loadGeneratorCoveredSeconds": 120.0,
        "loadGeneratorCoverageFraction": 1.0,
        "loadGeneratorMaxIntervalSeconds": 1.0,
        "loadGeneratorStartEdgeLagSeconds": 0.0,
        "loadGeneratorEndEdgeLagSeconds": 0.0,
        "loadGeneratorDownloadBitsPerSecond": 1_000_000_000,
        "targetUploadBitsPerSecond": 1_000_000_000,
        "networkLinkCapBitsPerSecond": 1_000_000_000,
        "networkLinkAdmissionFraction": 0.70,
        "networkLinkAdmissionBytesPerSecond": 87_500_000.0,
        "networkLinkHeadroomValid": True,
        "loadGeneratorDiskEvidenceValid": True,
        "loadGeneratorDiskFreeBytesBefore": 10 * 1024**3,
        "loadGeneratorDiskFreeBytesAfter": 10 * 1024**3,
        "warmupExitCode": 0,
        "k6ExitCode": 0,
    }
    value.update(overrides)
    return value


def sampler_with_receive_rates(
    rates: list[int],
    *,
    expected_duration: float = 30.0,
    timestamps: list[float] | None = None,
) -> benchmark.ProcessResourceSampler:
    admission = benchmark.LoadGeneratorAdmission(
        1,
        1000,
        90.0,
        90.0,
        1,
        1,
        8_000,
    )
    sampler = benchmark.ProcessResourceSampler(
        1,
        admission,
        expected_phase_duration_seconds=expected_duration,
        target_upload_bits_per_second=16_000,
    )
    if timestamps is None:
        timestamps = [float(index) for index in range(len(rates) + 1)]
    receive_total = 0
    sampler._samples = []
    for index, monotonic_seconds in enumerate(timestamps):
        if index > 0:
            receive_total += rates[index - 1]
        sampler._samples.append(
            {
                "monotonicSeconds": monotonic_seconds,
                "cpuTicks": 0,
                "rssBytes": 100,
                "networkReceiveBytes": receive_total,
                "networkTransmitBytes": index,
            }
        )
    sampler._phase_start_monotonic_seconds = timestamps[0]
    sampler._phase_end_monotonic_seconds = timestamps[-1]
    sampler._capture_start_monotonic_seconds = timestamps[0]
    sampler._capture_stop_monotonic_seconds = timestamps[-1]
    sampler._sampler_thread_stopped = True
    return sampler


def start_test_server(
    body: bytes,
    identity: str,
    *,
    cloudflare: bool = False,
    honor_conditionals: bool = True,
    include_content_length: bool = True,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/maps/world/settings.json":
                self.send_error(404)
                return
            conditional = self.headers.get("If-None-Match") or self.headers.get(
                "If-Modified-Since"
            )
            if conditional and honor_conditionals:
                self.send_response(304)
                self.send_header("ETag", '"fixture-etag"')
                self.send_header("Last-Modified", "Wed, 01 Jan 2025 00:00:00 GMT")
                self.send_header("X-BlueMap-Benchmark-Runtime", identity)
                if cloudflare:
                    self.send_header("CF-Ray", "forbidden")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Encoding", "zstd")
            self.send_header("Content-Type", "application/json")
            if include_content_length:
                self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"fixture-etag"')
            self.send_header("Last-Modified", "Wed, 01 Jan 2025 00:00:00 GMT")
            self.send_header("X-BlueMap-Benchmark-Runtime", identity)
            if cloudflare:
                self.send_header("CF-Ray", "forbidden")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def zstd_compress(value: bytes) -> bytes:
    result = subprocess.run(
        ["zstd", "--compress", "--stdout", "--quiet", "-3"],
        input=value,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def targets_for_servers(servers_and_threads) -> list[benchmark.Target]:
    return [
        benchmark.Target(
            name,
            f"http://127.0.0.1:{server.server_address[1]}",
            name,
            f"{name}-identity",
            "X-BlueMap-Benchmark-Runtime",
        )
        for name, (server, _) in zip(
            benchmark.VARIANTS, servers_and_threads, strict=True
        )
    ]


def stop_servers(servers_and_threads) -> None:
    for server, thread in servers_and_threads:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class BenchmarkHelpersTest(unittest.TestCase):
    def test_timed_javascript_rejects_the_preflight_proxy_header_set(self):
        source = (Path(__file__).resolve().parents[1] / "throughput.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("hasRejectedProxyHeader(response.headers)", source)
        self.assertIn('normalizedName.startsWith("cf-")', source)
        self.assertIn('normalizedName === "server"', source)
        self.assertIn('includes("cloudflare")', source)
        for name in benchmark.PROXY_RESPONSE_HEADERS:
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', source)
        self.assertIn('if (vus !== 12)', source)
        self.assertIn('"User-Agent": "BlueMap-Throughput/2"', source)
        self.assertNotIn("(${variant})", source)

    def test_main_rejects_nonapproved_vus_before_external_work(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            result = benchmark.main(
                [
                    "--upstream-url", "http://upstream.test",
                    "--upstream-php-url", "http://php.test",
                    "--new-java-url", "http://java.test",
                    "--upstream-id", "upstream-id",
                    "--new-java-id", "java-id",
                    "--dataset-id", "dataset-id",
                    "--setup-manifest", str(Path(directory) / "missing-setup.json"),
                    "--paths", str(Path(directory) / "missing-paths.txt"),
                    "--output", str(output),
                    "--vus", "13",
                ]
            )
            self.assertEqual(2, result)
            terminal = json.loads((output / "terminal.json").read_text())
            self.assertIn("exactly 12", terminal["error"])

    def test_parse_paths_accepts_comments_and_freezes_order(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "paths.txt"
            source.write_text(
                "# comment\n\n/maps/world/settings.json\n/maps/world/tiles/0/x1z2\n",
                encoding="utf-8",
            )
            self.assertEqual(
                ["/maps/world/settings.json", "/maps/world/tiles/0/x1z2"],
                benchmark.parse_paths(source),
            )

    def test_parse_paths_rejects_unsafe_or_duplicate_paths(self):
        for value in (
            "/index.html\n",
            "/maps/world/settings.json?fresh=1\n",
            "/maps/world/../private.json\n",
            "/maps/world/%2e%2e/private.json\n",
            "/maps/world//settings.json\n",
            "/maps/world/settings.json\n/maps/world/settings.json\n",
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "paths.txt"
                source.write_text(value, encoding="utf-8")
                with self.assertRaises(benchmark.BenchmarkError):
                    benchmark.parse_paths(source)

    def test_randomized_rotated_five_block_schedule_is_complete(self):
        targets = [benchmark.Target(name, f"http://{name}", name) for name in benchmark.VARIANTS]
        first = benchmark.create_schedule(targets, 5, "fixed-seed")
        second = benchmark.create_schedule(targets, 5, "fixed-seed")
        self.assertEqual(first, second)
        self.assertEqual(5, len(first))
        for block in first:
            self.assertEqual(set(benchmark.VARIANTS), set(block["order"]))
        self.assertNotEqual(first[0]["order"], first[1]["order"])

    def test_extract_metrics_reads_all_required_k6_evidence(self):
        metrics = benchmark.extract_metrics(k6_summary(1200))
        self.assertEqual(1200, metrics["requests"])
        self.assertEqual(1.0, metrics["networkMibPerSecond"])
        self.assertGreater(metrics["storedMibPerSecond"], 0)
        self.assertEqual(10.0, metrics["p50Milliseconds"])
        self.assertEqual(30.0, metrics["p99Milliseconds"])
        self.assertEqual(0, metrics["transportErrors"])
        self.assertEqual(0, metrics["droppedIterations"])

    def test_extract_metrics_accepts_exact_k6_v130_flat_summary(self):
        summary = k6_summary()
        for name, metric in list(summary["metrics"].items()):
            summary["metrics"][name] = dict(metric["values"])
        for name in ("http_req_failed", "checks"):
            rate = summary["metrics"][name].pop("rate")
            summary["metrics"][name].update(
                {"value": rate, "passes": 1 if rate == 1 else 0, "fails": 0}
            )
        metrics = benchmark.extract_metrics(
            summary,
            expected_path_count=1,
            expected_stored_bytes_per_iteration=1024,
        )
        self.assertEqual(1, metrics["requests"])
        self.assertEqual(1024, metrics["storedRepresentationBytes"])

    def test_extract_metrics_rejects_partial_profile_accounting(self):
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.extract_metrics(
                k6_summary(2),
                expected_path_count=1,
                expected_stored_bytes_per_iteration=1024,
            )
        mismatched = k6_summary()
        mismatched["metrics"]["benchmark_stored_representation_bytes"]["values"][
            "count"
        ] = 1023
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.extract_metrics(
                mismatched,
                expected_path_count=1,
                expected_stored_bytes_per_iteration=1024,
            )

    def test_extract_metrics_rejects_missing_malformed_or_nonfinite_evidence(self):
        invalid = []
        missing = k6_summary()
        del missing["metrics"]["benchmark_transport_errors"]
        invalid.append(missing)
        no_p99 = k6_summary()
        del no_p99["metrics"]["http_req_duration"]["values"]["p(99)"]
        invalid.append(no_p99)
        nonfinite = k6_summary()
        nonfinite["metrics"]["http_reqs"]["values"]["rate"] = math.nan
        invalid.append(nonfinite)
        incomplete_validation = k6_summary(2)
        incomplete_validation["metrics"]["benchmark_observed_responses"]["values"][
            "count"
        ] = 1
        invalid.append(incomplete_validation)
        for summary in invalid:
            with self.subTest(summary=summary), self.assertRaises(benchmark.BenchmarkError):
                benchmark.extract_metrics(summary)

    def test_process_sampler_fails_closed_on_incomplete_or_saturated_evidence(self):
        sampler = sampler_with_receive_rates([])
        incomplete = sampler._summarize()
        self.assertFalse(incomplete["valid"])
        sampler = sampler_with_receive_rates([10] * 30)
        for index, sample in enumerate(sampler._samples):
            sample["rssBytes"] = 900
            sample["cpuTicks"] = index * 100
        sampler.admission = benchmark.LoadGeneratorAdmission(
            1, 1000, 50.0, 50.0, 1, 1, 8_000
        )
        saturated = sampler._summarize()
        self.assertTrue(saturated["saturated"])
        self.assertFalse(saturated["valid"])
        self.assertEqual(10.0, saturated["p95NetworkReceiveBytesPerSecond"])

    def test_process_sampler_recomputes_nearest_rank_p95_and_link_headroom(self):
        sampler = sampler_with_receive_rates([10] * 28 + [650, 900])
        telemetry = sampler._summarize()
        self.assertTrue(telemetry["timingEvidenceValid"])
        self.assertEqual(30, telemetry["sampleCount"])
        self.assertEqual(27, telemetry["minimumPhaseSampleCount"])
        self.assertEqual(1.0, telemetry["coverageFraction"])
        self.assertEqual(650.0, telemetry["p95NetworkReceiveBytesPerSecond"])
        self.assertEqual(8_000, telemetry["networkLinkCapBitsPerSecond"])
        self.assertEqual(700.0, telemetry["networkLinkAdmissionBytesPerSecond"])
        self.assertTrue(telemetry["networkLinkHeadroomValid"])
        self.assertTrue(telemetry["valid"])
        self.assertGreater(telemetry["clockTicksPerSecond"], 0)
        self.assertEqual(31, len(telemetry["rawSamples"]))
        self.assertEqual(30, len(telemetry["samples"]))
        self.assertEqual(0.0, telemetry["samples"][0]["startMonotonicSeconds"])
        self.assertEqual(1.0, telemetry["samples"][0]["endMonotonicSeconds"])

        exceeded = sampler_with_receive_rates([10] * 28 + [701, 900])._summarize()
        self.assertEqual(701.0, exceeded["p95NetworkReceiveBytesPerSecond"])
        self.assertFalse(exceeded["networkLinkHeadroomValid"])
        self.assertFalse(exceeded["valid"])

    def test_process_sampler_requires_phase_coverage_fresh_edges_and_no_gaps(self):
        four_intervals = sampler_with_receive_rates([10] * 4)
        four_intervals._phase_end_monotonic_seconds = 30.0
        four_intervals._capture_stop_monotonic_seconds = 30.0
        sparse = four_intervals._summarize()
        self.assertFalse(sparse["timingEvidenceValid"])
        self.assertEqual(27, sparse["minimumPhaseSampleCount"])

        timestamps = [0.0, 3.0] + [float(value) for value in range(4, 33)]
        gap = sampler_with_receive_rates([10] * 30, timestamps=timestamps)._summarize()
        self.assertEqual(3.0, gap["maxIntervalSeconds"])
        self.assertFalse(gap["timingEvidenceValid"])

        stale = sampler_with_receive_rates(
            [10] * 30,
            timestamps=[float(value) for value in range(3, 34)],
        )
        stale._phase_start_monotonic_seconds = 0.0
        stale_evidence = stale._summarize()
        self.assertGreater(stale_evidence["startEdgeLagSeconds"], 2.0)
        self.assertFalse(stale_evidence["timingEvidenceValid"])

        regressed = sampler_with_receive_rates([10] * 30)
        regressed._samples[10]["networkReceiveBytes"] = 0
        regression_evidence = regressed._summarize()
        self.assertFalse(regression_evidence["timingEvidenceValid"])
        self.assertTrue(regression_evidence["evidenceErrors"])

    def test_run_k6_preserves_summary_log_and_loadgen_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_k6 = root / "k6"
            fake_k6.write_text(
                """#!/usr/bin/env python3
import json, sys, time
from pathlib import Path
summary = Path(sys.argv[sys.argv.index('--summary-export') + 1])
summary.write_text(json.dumps(%s))
time.sleep(0.2)
print('fake k6 output')
""" % repr(k6_summary()),
                encoding="utf-8",
            )
            fake_k6.chmod(0o755)
            paths = root / "paths.txt"
            paths.write_text("/maps/world/settings.json\n", encoding="utf-8")
            expectations = root / "expectations.json"
            expectations.write_text(
                json.dumps(
                    {
                        "formatVersion": benchmark.FORMAT_VERSION,
                        "paths": {
                            "/maps/world/settings.json": {
                                "storedRepresentationLength": 1024
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            summary = root / "summary.json"
            log = root / "run.log"
            telemetry = root / "telemetry.json"
            admission = benchmark.LoadGeneratorAdmission(
                1, 1024**3, 99.0, 99.0, 1, 1, 10_000_000_000
            )
            exit_code, metrics = benchmark.run_k6(
                k6_binary=str(fake_k6),
                script=Path(__file__),
                target=benchmark.Target(
                    "upstream",
                    "http://example.test",
                    "id",
                    upload_bits_per_second=10_000_000_000,
                ),
                path_file=paths,
                vus=benchmark.APPROVED_VUS,
                duration="100ms",
                accept_encoding="zstd",
                required_content_encoding="zstd",
                summary_path=summary,
                log_path=log,
                expectations_path=expectations,
                telemetry_path=telemetry,
                load_generator_admission=admission,
                sampler_interval_seconds=0.01,
            )
            self.assertEqual(0, exit_code)
            self.assertTrue(metrics["summaryEvidenceValid"])
            self.assertEqual(1, metrics["observedResponses"])
            self.assertTrue(metrics["loadGeneratorEvidenceValid"])
            self.assertTrue(metrics["loadGeneratorTimingEvidenceValid"])
            self.assertTrue(metrics["networkLinkHeadroomValid"])
            self.assertTrue(metrics["loadGeneratorDiskEvidenceValid"])
            self.assertRegex(metrics["summaryEvidenceSha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(metrics["logSha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(metrics["telemetrySha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(telemetry.is_file())
            self.assertIn("fake k6 output", log.read_text(encoding="utf-8"))

    def test_run_evidence_rejects_link_or_timing_admission_failure(self):
        valid = measurement("upstream", 1, 1)
        self.assertTrue(benchmark.run_evidence_is_valid(valid))
        warmup = measurement(
            "upstream",
            1,
            1,
            phase="warmup",
            loadGeneratorExpectedPhaseDurationSeconds=30.0,
            loadGeneratorCoveredSeconds=30.0,
            loadGeneratorSampleCount=27,
        )
        self.assertTrue(benchmark.run_evidence_is_valid(warmup))
        for overrides in (
            {"networkLinkHeadroomValid": False},
            {"loadGeneratorTimingEvidenceValid": False},
            {"loadGeneratorSampleCount": 4},
            {"loadGeneratorExpectedPhaseDurationSeconds": 30.0},
            {"loadGeneratorMaxIntervalSeconds": 2.01},
            {"loadGeneratorStartEdgeLagSeconds": 2.01},
            {"loadGeneratorP95NetworkReceiveBytesPerSecond": 87_500_001.0},
        ):
            with self.subTest(overrides=overrides):
                self.assertFalse(
                    benchmark.run_evidence_is_valid(
                        measurement("upstream", 1, 1, **overrides)
                    )
                )

    def test_failed_block_retains_rows_and_suppresses_aggregates(self):
        targets = [benchmark.Target(name, f"http://{name}", name) for name in benchmark.VARIANTS]
        schedule = benchmark.create_schedule(targets, 1, "seed")
        runs = [
            measurement(variant, 1, position, transportErrors=1, errors=1)
            if position == 1
            else measurement(variant, 1, position)
            for position, variant in enumerate(schedule[0]["order"], start=1)
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.assertFalse(
                benchmark.summarize_measurements(output, runs, 1, schedule, "profile-1")
            )
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(1, summary["failedBlocks"])
            self.assertNotIn("medianRequestsPerSecond", summary["variants"][0])

    def test_summary_requires_complete_five_block_schedule_and_one_profile(self):
        targets = [benchmark.Target(name, f"http://{name}", name) for name in benchmark.VARIANTS]
        schedule = benchmark.create_schedule(targets, 5, "seed")
        runs = [
            measurement(variant, block["block"], position)
            for block in schedule
            for position, variant in enumerate(block["order"], start=1)
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.assertTrue(
                benchmark.summarize_measurements(output, runs, 5, schedule, "profile-1")
            )
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(0, summary["failedBlocks"])
            self.assertEqual(5, len(summary["blocks"]))
        runs[0]["profileId"] = "different-profile"
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(
                benchmark.summarize_measurements(Path(directory), runs, 5, schedule, "profile-1")
            )

    def test_setup_manifest_is_strict_and_bound_to_dataset_tls_and_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.json"
            path.write_text(json.dumps(setup_manifest()), encoding="utf-8")
            parsed, raw = benchmark.load_setup_manifest(path, "snapshot-1")
            self.assertEqual("EU-NL-1", parsed["runpod"]["region"])
            self.assertEqual(path.read_bytes(), raw)
            no_header = setup_manifest()
            for target in no_header["targets"].values():
                target["identityHeader"] = None
            path.write_text(json.dumps(no_header), encoding="utf-8")
            benchmark.load_setup_manifest(path, "snapshot-1")

            mixed_header = setup_manifest()
            mixed_header["targets"]["upstream"]["identityHeader"] = None
            path.write_text(json.dumps(mixed_header), encoding="utf-8")
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.load_setup_manifest(path, "snapshot-1")
            for mutate in (
                lambda value: value["database"].__setitem__("snapshotId", "wrong"),
                lambda value: value["database"].__setitem__(
                    "aggregateConnectionCeiling", 11
                ),
                lambda value: value["database"]["tls"].__setitem__("verified", False),
                lambda value: value["loadGenerator"]["hardware"].__setitem__("logicalCpuCount", 0),
                lambda value: value["loadGenerator"]["hardware"].__setitem__(
                    "downloadBitsPerSecond", True
                ),
                lambda value: value["targets"]["new-java"].__setitem__(
                    "uploadBitsPerSecond", 0
                ),
                lambda value: value["resourceLimits"]["new-java"].__setitem__("cpu", "8 CPUs"),
            ):
                invalid = setup_manifest()
                mutate(invalid)
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(benchmark.BenchmarkError):
                    benchmark.load_setup_manifest(path, "snapshot-1")

    def test_preflight_proves_bytes_headers_identity_and_conditionals(self):
        decoded = b"same-decoded-body"
        stored = zstd_compress(decoded)
        servers = [
            start_test_server(
                stored,
                f"{name}-identity",
                include_content_length=(name != "upstream-php"),
            )
            for name in benchmark.VARIANTS
        ]
        try:
            evidence = benchmark.preflight(
                targets_for_servers(servers),
                ["/maps/world/settings.json"],
                "zstd",
                "zstd",
                2,
            )
            self.assertTrue(evidence["directOriginValidated"])
            entries = evidence["paths"]["/maps/world/settings.json"]
            self.assertEqual(
                1,
                len(
                    {
                        entry["storedRepresentationSha256"]
                        for entry in entries.values()
                    }
                ),
            )
            self.assertEqual(
                len(stored), entries["new-java"]["storedRepresentationLength"]
            )
            self.assertEqual(len(decoded), entries["new-java"]["decodedContentLength"])
            self.assertIsNone(entries["upstream-php"]["declaredContentLength"])
            self.assertEqual(len(stored), entries["new-java"]["declaredContentLength"])
            self.assertTrue(entries["new-java"]["conditional"]["etag"]["valid"])
            self.assertTrue(entries["new-java"]["conditional"]["lastModified"]["valid"])
            out_of_band_targets = [
                benchmark.Target(
                    target.name,
                    target.url,
                    target.artifact_id,
                    target.runtime_identity,
                    None,
                )
                for target in targets_for_servers(servers)
            ]
            self.assertTrue(
                benchmark.preflight(
                    out_of_band_targets,
                    ["/maps/world/settings.json"],
                    "zstd",
                    "zstd",
                    2,
                )["valid"]
            )
        finally:
            stop_servers(servers)

    def test_preflight_rejects_byte_difference_cloudflare_and_bad_304(self):
        same = zstd_compress(b"same")
        different = zstd_compress(b"different")
        cases = [
            [
                start_test_server(body, f"{name}-identity")
                for name, body in zip(benchmark.VARIANTS, (same, same, different), strict=True)
            ],
            [
                start_test_server(same, f"{name}-identity", cloudflare=(name == "new-java"))
                for name in benchmark.VARIANTS
            ],
            [
                start_test_server(same, f"{name}-identity", honor_conditionals=(name != "new-java"))
                for name in benchmark.VARIANTS
            ],
            [
                start_test_server(
                    same,
                    "wrong-identity" if name == "new-java" else f"{name}-identity",
                )
                for name in benchmark.VARIANTS
            ],
            [
                start_test_server(
                    b"not-a-zstd-frame",
                    f"{name}-identity",
                )
                for name in benchmark.VARIANTS
            ],
        ]
        for servers in cases:
            try:
                with self.assertRaises(benchmark.BenchmarkError):
                    benchmark.preflight(
                        targets_for_servers(servers),
                        ["/maps/world/settings.json"],
                        "zstd",
                        "zstd",
                        2,
                    )
            finally:
                stop_servers(servers)

    def test_main_preserves_terminal_evidence_on_preflight_failure(self):
        same = zstd_compress(b"same")
        different = zstd_compress(b"different")
        servers = [
            start_test_server(body, f"{name}-identity")
            for name, body in zip(benchmark.VARIANTS, (same, same, different), strict=True)
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
                    f"http://127.0.0.1:{server.server_address[1]}" for server, _ in servers
                ]
                result = benchmark.main(
                    [
                        "--upstream-url", urls[0],
                        "--upstream-php-url", urls[1],
                        "--new-java-url", urls[2],
                        "--upstream-id", "upstream-id",
                        "--new-java-id", "new-java-id",
                        "--dataset-id", "snapshot-1",
                        "--setup-manifest", str(setup),
                        "--paths", str(paths),
                        "--output", str(output),
                        "--k6", str(fake_k6),
                        "--repetitions", "5",
                    ]
                )
                self.assertEqual(2, result)
                terminal = json.loads((output / "terminal.json").read_text())
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual("failed", terminal["status"])
                self.assertFalse(summary["valid"])
                self.assertTrue((output / "schedule.json").is_file())
        finally:
            stop_servers(servers)


if __name__ == "__main__":
    unittest.main()
