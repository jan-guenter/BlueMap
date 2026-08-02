from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


K6_IMAGE = (
    "grafana/k6:1.3.0@"
    "sha256:3ddc8b1a33a2c3d8edc6e99b6a762ae36cba08788463458f5e6a7703e14eb77d"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPOSITORY_ROOT / "benchmarks/web-throughput/throughput.js"
sys.path.insert(0, str(SCRIPT.parent))
import run_benchmark as benchmark

PATH = "/maps/world/tiles/0/x0z0"


def zstd_compress(value: bytes) -> bytes:
    return subprocess.run(
        ["zstd", "--compress", "--stdout", "--quiet", "-3"],
        input=value,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


def start_server(stored: bytes, *, chunked: bool):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.path != PATH:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Encoding", "zstd")
            self.send_header("Content-Type", "application/octet-stream")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(stored)))
            self.end_headers()
            if chunked:
                midpoint = max(1, len(stored) // 2)
                for part in (stored[:midpoint], stored[midpoint:]):
                    if part:
                        self.wfile.write(f"{len(part):x}\r\n".encode("ascii"))
                        self.wfile.write(part + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(stored)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@unittest.skipUnless(
    os.environ.get("RUN_K6_INTEGRATION") == "1",
    "set RUN_K6_INTEGRATION=1 for the pinned k6 container proof",
)
class K6ZstdIntegrationTest(unittest.TestCase):
    def test_fixed_and_chunked_zstd_are_decoded_and_accounted(self):
        decoded = (b"BlueMap zstd integration proof\n" * 4096)
        stored = zstd_compress(decoded)
        for variant, chunked in (("upstream", False), ("upstream-php", True)):
            with self.subTest(variant=variant):
                server, thread = start_server(stored, chunked=chunked)
                try:
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        root.chmod(0o777)
                        (root / "paths.txt").write_text(PATH + "\n", encoding="utf-8")
                        (root / "expectations.json").write_text(
                            json.dumps(
                                {
                                    "formatVersion": 2,
                                    "paths": {
                                        PATH: {
                                            "storedRepresentationLength": len(stored),
                                            "decodedContentLength": len(decoded),
                                            "contentType": "application/octet-stream",
                                            "targets": {
                                                variant: {
                                                    "declaredContentLength": (
                                                        None if chunked else len(stored)
                                                    ),
                                                    "etag": None,
                                                    "lastModified": None,
                                                }
                                            },
                                        }
                                    },
                                }
                            ),
                            encoding="utf-8",
                        )
                        summary = root / "summary.json"
                        k6_binary = os.environ.get("K6_BINARY")
                        if k6_binary:
                            command = [
                                k6_binary, "run", "--quiet", "--summary-export",
                                str(summary), str(SCRIPT),
                            ]
                            environment = {
                                **os.environ,
                                "BASE_URL": f"http://127.0.0.1:{server.server_port}",
                                "PATH_FILE": str(root / "paths.txt"),
                                "EXPECTATIONS_FILE": str(root / "expectations.json"),
                                "VARIANT": variant,
                                "ACCEPT_ENCODING": "zstd",
                                "REQUIRED_CONTENT_ENCODING": "zstd",
                                "VUS": "12",
                                "DURATION": "1s",
                                "K6_NO_USAGE_REPORT": "true",
                            }
                            subprocess.run(
                                command, env=environment, check=True, timeout=180
                            )
                        else:
                            command = [
                                "docker", "run", "--rm", "--network", "host",
                                "-v", f"{SCRIPT}:/work/throughput.js:ro",
                                "-v", f"{root}:/evidence",
                                "-e", f"BASE_URL=http://127.0.0.1:{server.server_port}",
                                "-e", "PATH_FILE=/evidence/paths.txt",
                                "-e", "EXPECTATIONS_FILE=/evidence/expectations.json",
                                "-e", f"VARIANT={variant}",
                                "-e", "ACCEPT_ENCODING=zstd",
                                "-e", "REQUIRED_CONTENT_ENCODING=zstd",
                                "-e", "VUS=12", "-e", "DURATION=1s",
                                "-e", "K6_NO_USAGE_REPORT=true",
                                K6_IMAGE,
                                "run", "--quiet", "--summary-export",
                                "/evidence/summary.json", "/work/throughput.js",
                            ]
                            subprocess.run(command, check=True, timeout=180)
                        evidence = json.loads(summary.read_text(encoding="utf-8"))
                        metrics = evidence["metrics"]
                        def values(name: str):
                            metric = metrics[name]
                            return metric.get("values", metric)

                        requests = int(values("http_reqs")["count"])
                        stored_count = int(
                            values("benchmark_stored_representation_bytes")["count"]
                        )
                        self.assertGreater(requests, 0)
                        self.assertEqual(requests * len(stored), stored_count)
                        parsed = benchmark.extract_metrics(
                            evidence,
                            expected_path_count=1,
                            expected_stored_bytes_per_iteration=len(stored),
                        )
                        self.assertEqual(requests, parsed["requests"])
                        self.assertEqual(stored_count, parsed["storedRepresentationBytes"])
                        for metric in (
                            "benchmark_errors",
                            "benchmark_content_length_errors",
                            "benchmark_body_length_errors",
                        ):
                            self.assertEqual(0, values(metric)["count"])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
