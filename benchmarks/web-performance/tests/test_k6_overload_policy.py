from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
K6_SCRIPT = BENCHMARK_ROOT / "k6" / "bluemap.js"
RUNNER = BENCHMARK_ROOT / "tools" / "run_origin_case.sh"


def extract_shell_function(source: str, name: str) -> str:
    lines = source.splitlines()
    start = lines.index(f"{name}() {{")
    for end in range(start + 1, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[start : end + 1])
    raise AssertionError(f"Could not find the end of {name}")


class K6OverloadPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = K6_SCRIPT.read_text(encoding="utf-8")
        cls.runner = RUNNER.read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_overload_signature_is_exact_and_fail_closed(self) -> None:
        helper_source = self.script[
            self.script.index("function responseHeader") : self.script.index(
                "function recordStatus"
            )
        ]
        valid_headers = {
            "X-BlueMap-Overload": "capacity",
            "Retry-After": "7",
            "Content-Type": "application/problem+json",
            "Cache-Control": 'private, extension="a,b", NO-STORE',
        }
        cases = [
            {"status": 503, "headers": valid_headers},
            {
                "status": 503,
                "headers": {**valid_headers, "Cf-Ray": "edge-contamination"},
                "error": "HTTP status 503",
                "timings": {"duration": 1, "waiting": 1},
            },
            {"status": 503, "headers": {}},
            {
                "status": 503,
                "headers": {**valid_headers, "X-BlueMap-Overload": "Capacity"},
            },
            {
                "status": 503,
                "headers": {**valid_headers, "Retry-After": "0"},
            },
            {
                "status": 503,
                "headers": {**valid_headers, "Retry-After": "01"},
            },
            {
                "status": 503,
                "headers": {**valid_headers, "Retry-After": "1.5"},
            },
            {
                "status": 503,
                "headers": {
                    **valid_headers,
                    "Content-Type": "application/problem+json; charset=utf-8",
                },
            },
            {
                "status": 503,
                "headers": {**valid_headers, "Cache-Control": "x-no-store"},
            },
            {
                "status": 503,
                "headers": {**valid_headers, "Cache-Control": "no-store=true"},
            },
            {
                "status": 503,
                "headers": {
                    **valid_headers,
                    "Cache-Control": 'no-store, extension="unterminated',
                },
            },
            {"status": 200, "headers": valid_headers},
            {"status": 200, "headers": {"Cache-Control": "no-store"}},
            {
                "status": 0,
                "headers": {},
                "timings": {"duration": 0, "waiting": 0},
            },
        ]
        node_program = f"""
{helper_source}
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map((response) => ({{
  overload: classifyOverloadResponse(response),
  transport: responseHasTransportError(response),
}}))));
"""
        result = subprocess.run(
            ["node", "-e", node_program, json.dumps(cases)],
            check=True,
            capture_output=True,
            text=True,
        )
        classified = json.loads(result.stdout)

        self.assertEqual(
            classified[0]["overload"],
            {"signaled": True, "valid": True, "malformed": False},
        )
        # Edge headers and an HTTP-status error string do not change the
        # exclusive response classification; their independent gates still fail.
        self.assertEqual(classified[1]["overload"], classified[0]["overload"])
        self.assertFalse(classified[1]["transport"])
        for entry in classified[2:12]:
            self.assertEqual(
                entry["overload"],
                {"signaled": True, "valid": False, "malformed": True},
            )
        self.assertEqual(
            classified[12]["overload"],
            {"signaled": False, "valid": False, "malformed": False},
        )
        self.assertTrue(classified[13]["transport"])

    def test_metrics_form_an_explicit_exclusive_partition(self) -> None:
        for metric in (
            "bluemap_workload_requests",
            "bluemap_available_responses",
            "bluemap_overload_responses",
            "bluemap_malformed_overload_responses",
            "bluemap_transport_errors",
            "bluemap_unexpected_responses",
        ):
            self.assertIn(metric, self.script)

        self.assertIn('"http_reqs{traffic:workload}": ["count>0"]', self.script)
        self.assertIn("availableResponses.add(successful ? 1 : 0, tags)", self.script)
        self.assertIn(
            "overloadResponses.add(classifiedOverload ? 1 : 0, tags)",
            self.script,
        )
        self.assertIn("transportErrors.add(failedTransport ? 1 : 0, tags)", self.script)
        self.assertIn("bluemap_available_duration", self.script)
        self.assertIn("bluemap_available_ttfb", self.script)
        self.assertNotIn("http_req_failed{traffic:workload}", self.script)

    def test_allow_explicit_is_opt_in_and_forbid_remains_strict(self) -> None:
        self.assertIn(
            'const OVERLOAD_POLICY = __ENV.OVERLOAD_POLICY || "forbid"',
            self.script,
        )
        self.assertIn('["forbid", "allow-explicit"]', self.script)
        self.assertIn(
            'commonThresholds.bluemap_overload_responses = ["count==0"]',
            self.script,
        )
        self.assertIn(
            'commonThresholds.bluemap_available_responses = ["count>0"]',
            self.script,
        )
        self.assertIn(
            'if (ENFORCE_LATENCY_GATES && OVERLOAD_POLICY === "forbid")',
            self.script,
        )

    def test_runner_pins_policy_and_uses_post_summary_gates(self) -> None:
        help_result = subprocess.run(
            ["bash", str(RUNNER), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("--overload-policy forbid|allow-explicit", help_result.stdout)
        self.assertIn('-e "OVERLOAD_POLICY=$OVERLOAD_POLICY"', self.runner)
        self.assertIn("and .overloadPolicy == $overloadPolicy", self.runner)
        self.assertIn("overloadPolicy: $overloadPolicy", self.runner)
        self.assertIn("validate_response_policy_gate", self.runner)
        self.assertIn('"$local_dir/response-policy-gate.json"', self.runner)

    @unittest.skipUnless(shutil.which("jq"), "jq is required")
    def test_response_policy_gate_checks_partition_and_policy(self) -> None:
        function = extract_shell_function(
            self.runner, "validate_response_policy_gate"
        )

        def summary(
            *,
            workload: int,
            http_workload: int,
            available: int,
            overload: int,
            iterations: int | None = None,
            malformed: int = 0,
            transport: int = 0,
            unexpected: int = 0,
        ) -> dict[str, object]:
            def counter(count: int) -> dict[str, object]:
                return {"values": {"count": count, "rate": count / 10}}

            return {
                "metrics": {
                    "http_reqs{traffic:workload}": counter(http_workload),
                    "iterations": counter(
                        workload if iterations is None else iterations
                    ),
                    "bluemap_workload_requests": counter(workload),
                    "bluemap_available_responses": counter(available),
                    "bluemap_overload_responses": counter(overload),
                    "bluemap_malformed_overload_responses": counter(malformed),
                    "bluemap_transport_errors": counter(transport),
                    "bluemap_unexpected_responses": counter(unexpected),
                }
            }

        missing_rate = summary(
            workload=10,
            http_workload=10,
            available=10,
            overload=0,
        )
        del missing_rate["metrics"]["bluemap_overload_responses"]["values"][
            "rate"
        ]
        cases = [
            (
                "forbid",
                summary(workload=10, http_workload=10, available=10, overload=0),
                True,
            ),
            (
                "forbid",
                summary(workload=10, http_workload=10, available=9, overload=1),
                False,
            ),
            (
                "allow-explicit",
                summary(workload=10, http_workload=10, available=0, overload=10),
                True,
            ),
            (
                "allow-explicit",
                summary(
                    workload=10,
                    http_workload=10,
                    available=9,
                    overload=0,
                    malformed=1,
                ),
                False,
            ),
            (
                "allow-explicit",
                summary(workload=10, http_workload=9, available=10, overload=0),
                False,
            ),
            (
                "allow-explicit",
                summary(
                    workload=10,
                    http_workload=10,
                    iterations=9,
                    available=10,
                    overload=0,
                ),
                False,
            ),
            (
                "allow-explicit",
                summary(workload=0, http_workload=0, available=0, overload=0),
                False,
            ),
            ("allow-explicit", missing_rate, False),
        ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (policy, content, expected) in enumerate(cases):
                source = root / f"summary-{index}.json"
                destination = root / f"gate-{index}.json"
                source.write_text(json.dumps(content), encoding="utf-8")
                command = (
                    f"{function}\n"
                    f"OVERLOAD_POLICY={policy!r}\n"
                    f"validate_response_policy_gate {str(source)!r} "
                    f"{str(destination)!r}"
                )
                result = subprocess.run(["bash", "-c", command], check=False)
                self.assertEqual(result.returncode == 0, expected)
                artifact = json.loads(destination.read_text(encoding="utf-8"))
                self.assertEqual(artifact["passed"], expected)
                if content["metrics"]["bluemap_workload_requests"]["values"][
                    "count"
                ] == 0:
                    self.assertTrue(artifact["partitionValid"])
                    self.assertFalse(artifact["requestIterationIdentityValid"])

    @unittest.skipUnless(shutil.which("jq"), "jq is required")
    def test_zero_available_latency_is_explicitly_inapplicable(self) -> None:
        function = extract_shell_function(self.runner, "validate_latency_gate")
        summary = {
            "metrics": {
                "bluemap_available_responses": {
                    "values": {"count": 0, "rate": 0}
                }
            }
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "summary.json"
            source.write_text(json.dumps(summary), encoding="utf-8")
            for policy in ("allow-explicit", "forbid"):
                destination = root / f"latency-{policy}.json"
                command = (
                    f"{function}\n"
                    "EFFECTIVE_LATENCY_P95_MS=500\n"
                    "EFFECTIVE_LATENCY_P99_MS=1000\n"
                    f"OVERLOAD_POLICY={policy!r}\n"
                    f"validate_latency_gate {str(source)!r} {str(destination)!r}"
                )
                subprocess.run(["bash", "-c", command], check=True)
                artifact = json.loads(destination.read_text(encoding="utf-8"))
                self.assertFalse(artifact["applicable"])
                self.assertIsNone(artifact["passed"])
                self.assertIsNone(artifact["observedP95Milliseconds"])
                self.assertIsNone(artifact["observedP99Milliseconds"])


if __name__ == "__main__":
    unittest.main()
