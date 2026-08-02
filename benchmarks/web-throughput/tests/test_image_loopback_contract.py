from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
IMAGES = ROOT / "benchmarks" / "web-throughput" / "images"
BOOTSTRAP = IMAGES / "common" / "bootstrap.sh"


class ImageLoopbackContractTest(unittest.TestCase):
    def validate_webserver_config(self, text: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "webserver.conf"
            config.write_text(text, encoding="utf-8")
            return subprocess.run(
                [
                    "sh",
                    "-c",
                    '. "$1"; bootstrap_validate_java_webserver_config "$2"',
                    "validate-webserver-config",
                    str(BOOTSTRAP),
                    str(config),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    def test_shared_validator_accepts_only_one_explicit_loopback_binding(self) -> None:
        for ip_setting in ('ip: "127.0.0.1"', "ip = 127.0.0.1"):
            with self.subTest(ip_setting=ip_setting):
                result = self.validate_webserver_config(
                    f"enabled: true\n{ip_setting}\nport: 8100\nlog: {{}}\n"
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_shared_validator_rejects_missing_non_loopback_or_duplicate_binding(self) -> None:
        invalid_configs = {
            "missing": "enabled: true\nport: 8100\nlog: {}\n",
            "ipv4-wildcard": 'ip: "0.0.0.0"\nport: 8100\nlog: {}\n',
            "ipv6-wildcard": 'ip: "::"\nport: 8100\nlog: {}\n',
            "hostname": 'ip: "localhost"\nport: 8100\nlog: {}\n',
            "duplicate": (
                'ip: "127.0.0.1"\nip: "127.0.0.1"\nport: 8100\nlog: {}\n'
            ),
        }
        for name, config in invalid_configs.items():
            with self.subTest(name=name):
                result = self.validate_webserver_config(config)
                self.assertEqual(result.returncode, 78)
                self.assertIn("exactly one active ip: 127.0.0.1", result.stderr)

    def test_php_and_candidate_images_publish_no_http_listener(self) -> None:
        nginx = (IMAGES / "php" / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("listen 127.0.0.1:8100;", nginx)
        self.assertNotIn("listen 8100;", nginx)
        for role in ("upstream", "php", "java"):
            with self.subTest(role=role):
                dockerfile = (IMAGES / role / "Dockerfile").read_text(encoding="utf-8")
                self.assertIn("EXPOSE 22\n", dockerfile)
                self.assertNotIn("EXPOSE 22 8100", dockerfile)
                self.assertNotIn("iptables", dockerfile)

    def test_setup_example_freezes_ssh_lane_transport(self) -> None:
        manifest = json.loads(
            (ROOT / "benchmarks" / "web-throughput" / "setup.example.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["transport"]["originBindAddress"], "127.0.0.1")
        self.assertEqual(manifest["transport"]["originPort"], 8100)
        self.assertEqual(manifest["transport"]["laneCountPerTarget"], 12)
        self.assertTrue(manifest["transport"]["sshHostKeysPinned"])
        self.assertFalse(manifest["transport"]["candidatePublicHttp"])


if __name__ == "__main__":
    unittest.main()
