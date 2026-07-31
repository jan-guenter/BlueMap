from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
TOOLS = BENCHMARK_ROOT / "tools"
ANALYZER_PATH = BENCHMARK_ROOT / "controller" / "formal" / "analyze.py"
ANALYZER_SPEC = importlib.util.spec_from_file_location(
    "bluemap_direct_transport_analyze", ANALYZER_PATH
)
assert ANALYZER_SPEC is not None and ANALYZER_SPEC.loader is not None
analyze = importlib.util.module_from_spec(ANALYZER_SPEC)
sys.modules[ANALYZER_SPEC.name] = analyze
ANALYZER_SPEC.loader.exec_module(analyze)


class SshL4TraefikModeTests(unittest.TestCase):
    def test_runner_exposes_two_fail_closed_runpod_modes(self) -> None:
        runner_path = TOOLS / "run_origin_case.sh"
        runner = runner_path.read_text(encoding="utf-8")
        help_result = subprocess.run(
            ["bash", str(runner_path), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--traffic-mode MODE", help_result.stdout)
        self.assertIn('TRAFFIC_MODE="cloudflare-https"', runner)
        self.assertIn(
            '"${TRAFFIC_BASE_URL%/}" == "https://$TRAFFIC_HOST"', runner
        )
        self.assertIn(
            '"${TRAFFIC_BASE_URL%/}" == "http://$TRAFFIC_HOST"', runner
        )
        self.assertIn(
            'die "cloudflare-https traffic requires --require-edge-bypass"',
            runner,
        )
        self.assertIn(
            'die "ssh-l4-traefik traffic forbids --require-edge-bypass"',
            runner,
        )
        self.assertIn('exec-traefik-forward "$@"', runner)
        self.assertIn('loadgen_k6_exec "${phase_command[@]}"', runner)
        self.assertIn('-e "ACCEPT_ENCODING=$ACCEPT_ENCODING"', runner)
        self.assertIn('-e "STORED_ENCODING=$STORED_ENCODING"', runner)

        for key, value in (
            ("listenHost", '"127.0.0.1"'),
            ("listenPort", "18080"),
            ("targetHost", '"rke2-traefik.kube-system.svc.cluster.local"'),
            ("targetPort", "80"),
        ):
            self.assertIn(f"{key}: {value}", runner)

    def test_k6_uses_fixed_host_mapping_and_rejects_edge_headers(self) -> None:
        script = (BENCHMARK_ROOT / "k6" / "bluemap.js").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '"bluemap-test.guenter.cloud:80": "127.0.0.1:18080"',
            script,
        )
        self.assertIn(
            'BASE_URL !== "http://bluemap-test.guenter.cloud"',
            script,
        )
        self.assertIn("bluemap_prohibited_edge_header", script)
        self.assertIn('commonThresholds.bluemap_prohibited_edge_header = ["rate==0"]', script)
        for header in ("cf-ray", "cf-cache-status", "cf-mitigated"):
            self.assertIn(f'"{header}"', script)
        self.assertIn(
            '"SSH L4 Traefik response has no Cloudflare headers"',
            script,
        )
        self.assertIn('"Accept-Encoding": ACCEPT_ENCODING', script)

    def test_direct_k6_disables_redirects_in_every_options_branch(self) -> None:
        script = (BENCHMARK_ROOT / "k6" / "bluemap.js").read_text(
            encoding="utf-8"
        )
        build_options = script[
            script.index("function buildOptions()") : script.index(
                "function browserMixedIteration()"
            )
        ]
        network_options = build_options[
            build_options.index("const networkOptions =") : build_options.index(
                "if (REQUIRE_EDGE_BYPASS)"
            )
        ]

        self.assertIn('TRAFFIC_MODE === "ssh-l4-traefik"', network_options)
        self.assertIn("maxRedirects: 0", network_options)
        self.assertIn(
            '"bluemap-test.guenter.cloud:80": "127.0.0.1:18080"',
            network_options,
        )
        self.assertIn(": {};", network_options)
        self.assertEqual(script.count("maxRedirects: 0"), 1)
        self.assertEqual(
            build_options.count("...networkOptions"),
            2,
            "live-viewer and ordinary workload options must both retain the "
            "direct no-redirect control",
        )

    def test_redirect_status_cannot_pass_the_direct_phase_gate(self) -> None:
        # With maxRedirects=0, k6 returns the first 3xx response to recordStatus.
        # A redirect can have otherwise-clean direct transport headers, but its
        # unexpected-status rate must still make the combined phase gate fail.
        summary = {
            "metrics": {
                "bluemap_unexpected_status": {"value": 1},
                "http_req_failed{traffic:workload}": {"value": 0},
                "bluemap_prohibited_edge_header": {
                    "value": 0,
                    "passes": 0,
                    "fails": 1,
                },
            }
        }
        entry = {
            "entryId": "redirect-must-fail",
            "profile": "static",
            "contractMode": "enhanced",
        }

        proof = analyze.transport_phase_proof(
            summary,
            entry,
            "ssh-l4-traefik",
            {},
        )
        self.assertIs(proof["passed"], True)
        with self.assertRaisesRegex(
            analyze.AnalysisFailure,
            "status/failure metrics violate the gate",
        ):
            analyze.validate_status_metrics(
                summary,
                entry,
                "measurement",
                "ssh-l4-traefik",
                {},
            )

    def test_direct_enhanced_mode_proves_stored_compression_survives(self) -> None:
        script = (BENCHMARK_ROOT / "k6" / "bluemap.js").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'const STORED_ENCODING = __ENV.STORED_ENCODING || "zstd"',
            script,
        )
        self.assertIn("bluemap_stored_content_encoding_violation", script)
        self.assertIn(
            "commonThresholds.bluemap_stored_content_encoding_violation",
            script,
        )
        self.assertIn('TRAFFIC_MODE !== "ssh-l4-traefik"', script)
        self.assertIn('CONTRACT_MODE !== "enhanced"', script)
        self.assertIn("response.status !== 200", script)
        self.assertIn("manifest.tiles.includes(normalized)", script)
        self.assertIn(r"/\/tiles\/0\//.test(normalized)", script)
        self.assertIn("manifest.textures.includes(normalized)", script)
        self.assertIn("function storedCompressionProofApplicable()", script)
        self.assertNotIn('case "live-viewers":', script)
        self.assertIn(
            'responseHeader(response, "content-encoding")',
            script,
        )
        self.assertIn("actualEncoding !== expectedEncoding", script)
        self.assertIn(
            '"SSH L4 Traefik preserves stored Content-Encoding"',
            script,
        )

        # The proof is deliberately route-scoped; uncompressed data must not
        # be blanket-checked against STORED_ENCODING.
        for route_group in ("assets", "settings", "players", "markers"):
            self.assertNotIn(
                f"manifest.{route_group}.includes(normalized)",
                script,
            )

    def test_helper_has_one_fixed_reverse_forward(self) -> None:
        helper = (TOOLS / "runpod_loadgen.sh").read_text(encoding="utf-8")
        forward = (
            "127.0.0.1:18080:"
            "rke2-traefik.kube-system.svc.cluster.local:80"
        )

        self.assertEqual(helper.count(forward), 1)
        self.assertIn("-o ExitOnForwardFailure=yes", helper)
        self.assertIn("exec-traefik-forward)", helper)
        self.assertNotIn("--forward-listen", helper)
        self.assertNotIn("--forward-target", helper)

    def test_shell_scripts_parse(self) -> None:
        for script in ("run_origin_case.sh", "runpod_loadgen.sh"):
            with self.subTest(script=script):
                subprocess.run(
                    ["bash", "-n", str(TOOLS / script)],
                    check=True,
                    capture_output=True,
                    text=True,
                )


if __name__ == "__main__":
    unittest.main()
