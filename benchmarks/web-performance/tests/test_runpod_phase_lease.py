from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
RUN_PHASE = BENCHMARK_ROOT / "runpod" / "run-phase.sh"


FAKE_SAMPLER = """#!/usr/bin/env bash
set -Eeuo pipefail
output="$1"
stop_file="$2"
: >"$output"
if [[ "${FAKE_SAMPLER_FAIL:-false}" == true ]]; then
    exit 42
fi
while [[ ! -e "$stop_file" ]]; do
    sleep 0.05
done
"""


class RunPodPhaseLeaseTests(unittest.TestCase):
    def prepare(self, root: Path) -> tuple[Path, dict[str, str], Path, Path]:
        artifacts = root / "artifacts"
        artifacts.mkdir()
        fake_bin = root / "bin"
        fake_bin.mkdir()
        sampler = fake_bin / "bluemap-runpod-sample-resources"
        sampler.write_text(FAKE_SAMPLER, encoding="utf-8")
        sampler.chmod(0o755)

        # Execute the production script with only its fixed filesystem roots
        # relocated into the isolated test directory. The short escalation
        # loop keeps the stubborn-child case fast while preserving the exact
        # TERM -> poll -> KILL algorithm.
        script = RUN_PHASE.read_text(encoding="utf-8")
        script = script.replace(
            "/tmp/bluemap-runpod-active-phase.lock",
            str(root / "active-phase.lock"),
        )
        script = script.replace(
            "mktemp -d /tmp/bluemap-runpod-phase.XXXXXXXX",
            f"mktemp -d {root}/runtime.XXXXXXXX",
        )
        script = script.replace("/artifacts", str(artifacts))
        script = script.replace("for _ in {1..100}; do", "for _ in {1..5}; do")
        relocated = root / "run-phase.sh"
        relocated.write_text(script, encoding="utf-8")
        relocated.chmod(0o755)

        resource_output = artifacts / "case" / "resources.ndjson"
        session_output = artifacts / "case" / "command-session.json"
        resource_output.parent.mkdir()
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "BLUEMAP_PHASE_TIMEOUT_SECONDS": "30",
            "BLUEMAP_PHASE_SESSION_ID": "a" * 64,
            "BLUEMAP_PHASE_SESSION_OUTPUT": str(session_output),
        }
        return relocated, environment, resource_output, session_output

    def start(
        self,
        script: Path,
        environment: dict[str, str],
        resource_output: Path,
        command: list[str],
    ) -> tuple[subprocess.Popen[str], int]:
        read_fd, write_fd = os.pipe()
        process = subprocess.Popen(
            [
                "bash",
                str(script),
                "--resource-output",
                str(resource_output),
                "--",
                *command,
            ],
            env=environment,
            stdin=read_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(read_fd)
        os.write(
            write_fd,
            (
                "bluemap-phase-lease-v1:"
                f"{environment['BLUEMAP_PHASE_SESSION_ID']}\n"
            ).encode(),
        )
        return process, write_fd

    def test_normal_completion_reaps_watcher_and_preserves_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, resources, receipt_path = self.prepare(root)
            process, lease = self.start(
                script,
                environment,
                resources,
                ["bash", "-ceu", "exit 17"],
            )
            status = process.wait(timeout=10)
            os.close(lease)
            _, stderr = process.communicate(timeout=1)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

            self.assertEqual(status, 17, stderr)
            self.assertTrue(receipt["passed"])
            self.assertFalse(receipt["lease"]["eofObserved"])
            self.assertFalse(receipt["termination"]["requested"])
            self.assertEqual(receipt["termination"]["commandExitStatus"], 17)
            self.assertTrue(receipt["termination"]["processGroupEmpty"])
            self.assertTrue(receipt["termination"]["watcherReaped"])
            self.assertTrue(receipt["termination"]["samplerReaped"])
            self.assertFalse((root / "active-phase.lock").exists())

    def test_lease_eof_kills_stubborn_process_group_and_confirms_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, resources, receipt_path = self.prepare(root)
            child_pid_path = root / "stubborn-child.pid"
            process, lease = self.start(
                script,
                environment,
                resources,
                [
                    "bash",
                    "-ceu",
                    'trap "" TERM; sleep 300 & echo "$!" >"$1"; wait',
                    "bash",
                    str(child_pid_path),
                ],
            )
            for _ in range(100):
                if child_pid_path.is_file():
                    break
                time.sleep(0.02)
            self.assertTrue(child_pid_path.is_file())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))

            os.close(lease)
            status = process.wait(timeout=10)
            _, stderr = process.communicate(timeout=1)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

            self.assertNotEqual(status, 0, stderr)
            self.assertTrue(receipt["passed"])
            self.assertTrue(receipt["lease"]["eofObserved"])
            self.assertTrue(receipt["termination"]["requested"])
            self.assertEqual(receipt["termination"]["termSignal"], "TERM")
            self.assertTrue(receipt["termination"]["killEscalated"])
            self.assertTrue(receipt["termination"]["processGroupEmpty"])
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self.assertFalse((root / "active-phase.lock").exists())

    def test_existing_active_phase_lock_refuses_a_fresh_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, resources, receipt_path = self.prepare(root)
            (root / "active-phase.lock").mkdir()
            result = subprocess.run(
                [
                    "bash",
                    str(script),
                    "--resource-output",
                    str(resources),
                    "--",
                    "bash",
                    "-ceu",
                    "exit 0",
                ],
                env=environment,
                input="",
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("active-phase lock", result.stderr)
            self.assertFalse(receipt_path.exists())

    def test_immediate_eof_is_safe_across_repeated_group_launches(self) -> None:
        for attempt in range(10):
            with self.subTest(attempt=attempt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                script, environment, resources, receipt_path = self.prepare(root)
                side_effect = root / "must-not-run"
                result = subprocess.run(
                    [
                        "bash",
                        str(script),
                        "--resource-output",
                        str(resources),
                        "--",
                        "bash",
                        "-ceu",
                        ': >"$1"; sleep 300',
                        "bash",
                        str(side_effect),
                    ],
                    env=environment,
                    input=(
                        "bluemap-phase-lease-v1:"
                        f"{environment['BLUEMAP_PHASE_SESSION_ID']}\n"
                    ),
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertFalse(receipt_path.exists())
                self.assertTrue((root / "active-phase.lock").is_dir())
                self.assertFalse(side_effect.exists())

    def test_protocol_byte_is_fail_closed_and_retains_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, resources, receipt_path = self.prepare(root)
            process, lease = self.start(
                script,
                environment,
                resources,
                ["bash", "-ceu", "sleep 300"],
            )
            os.write(lease, b"x")
            os.close(lease)
            status = process.wait(timeout=10)
            _, stderr = process.communicate(timeout=1)

            self.assertNotEqual(status, 0, stderr)
            self.assertFalse(receipt_path.exists())
            self.assertTrue((root / "active-phase.lock").is_dir())

    def test_unexpected_watcher_death_kills_workload_and_retains_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, resources, receipt_path = self.prepare(root)
            side_effect = root / "must-not-run"
            process, lease = self.start(
                script,
                environment,
                resources,
                [
                    "bash",
                    "-ceu",
                    'sleep 2; : >"$1"',
                    "bash",
                    str(side_effect),
                ],
            )

            watcher_pid_path: Path | None = None
            for _ in range(200):
                candidates = list(root.glob("runtime.*/lease-watcher-pid"))
                live_markers = list(root.glob("runtime.*/lease-watcher-live"))
                if candidates and live_markers:
                    watcher_pid_path = candidates[0]
                    break
                time.sleep(0.01)
            self.assertIsNotNone(watcher_pid_path)
            watcher_pid = int(watcher_pid_path.read_text(encoding="utf-8"))
            os.kill(watcher_pid, signal.SIGKILL)
            os.close(lease)

            status = process.wait(timeout=10)
            _, stderr = process.communicate(timeout=1)
            self.assertEqual(status, 125, stderr)
            self.assertIn("lease watcher exited unexpectedly", stderr)
            self.assertFalse(side_effect.exists())
            self.assertFalse(receipt_path.exists())
            self.assertTrue((root / "active-phase.lock").is_dir())

    def test_sampler_failure_is_fail_closed_and_retains_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, resources, receipt_path = self.prepare(root)
            environment["FAKE_SAMPLER_FAIL"] = "true"
            process, lease = self.start(
                script,
                environment,
                resources,
                ["bash", "-ceu", "sleep 0.2"],
            )
            status = process.wait(timeout=10)
            os.close(lease)
            _, stderr = process.communicate(timeout=1)

            self.assertEqual(status, 125, stderr)
            self.assertIn("resource sampler exited", stderr)
            self.assertFalse(receipt_path.exists())
            self.assertTrue((root / "active-phase.lock").is_dir())

    def test_receipt_publication_failure_retains_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, resources, receipt_path = self.prepare(root)
            source = script.read_text(encoding="utf-8").replace(
                'mv -- "$session_temp" "$session_output"',
                'false # injected receipt-publication failure',
            )
            script.write_text(source, encoding="utf-8")
            process, lease = self.start(
                script,
                environment,
                resources,
                ["bash", "-ceu", "exit 0"],
            )
            status = process.wait(timeout=10)
            os.close(lease)
            process.communicate(timeout=1)

            self.assertNotEqual(status, 0)
            self.assertFalse(receipt_path.exists())
            self.assertTrue((root / "active-phase.lock").is_dir())

    def test_term_during_command_reaps_group_and_retains_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, resources, receipt_path = self.prepare(root)
            child_pid_path = root / "child.pid"
            process, lease = self.start(
                script,
                environment,
                resources,
                [
                    "bash",
                    "-ceu",
                    'sleep 300 & echo "$!" >"$1"; wait',
                    "bash",
                    str(child_pid_path),
                ],
            )
            for _ in range(100):
                if child_pid_path.is_file():
                    break
                time.sleep(0.02)
            self.assertTrue(child_pid_path.is_file())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            process.terminate()
            status = process.wait(timeout=10)
            os.close(lease)
            process.communicate(timeout=1)

            self.assertEqual(status, 143)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self.assertFalse(receipt_path.exists())
            self.assertTrue((root / "active-phase.lock").is_dir())

    def test_term_racing_eof_cannot_interrupt_group_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, resources, receipt_path = self.prepare(root)
            child_pid_path = root / "stubborn-race-child.pid"
            process, lease = self.start(
                script,
                environment,
                resources,
                [
                    "bash",
                    "-ceu",
                    'trap "" TERM; sleep 300 & echo "$!" >"$1"; wait',
                    "bash",
                    str(child_pid_path),
                ],
            )
            for _ in range(100):
                if child_pid_path.is_file():
                    break
                time.sleep(0.02)
            self.assertTrue(child_pid_path.is_file())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            os.close(lease)
            for _ in range(100):
                if list(root.glob("runtime.*/termination-requested")):
                    break
                time.sleep(0.01)
            self.assertTrue(list(root.glob("runtime.*/termination-requested")))
            process.terminate()
            status = process.wait(timeout=10)
            process.communicate(timeout=1)

            self.assertEqual(status, 143)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self.assertFalse(receipt_path.exists())
            self.assertTrue((root / "active-phase.lock").is_dir())


if __name__ == "__main__":
    unittest.main()
