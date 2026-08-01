from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
HELPER = BENCHMARK_ROOT / "tools" / "runpod_loadgen.sh"
RUNNER = BENCHMARK_ROOT / "tools" / "run_origin_case.sh"
CONTROLLER_TESTS = BENCHMARK_ROOT / "controller" / "tests"
if str(CONTROLLER_TESTS) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_TESTS))

from support import (  # noqa: E402
    analyze,
    orchestrate,
    runpod_identity,
    runpod_runtime_identity,
)


FAKE_SSH = r"""#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >>"$FAKE_SSH_LOG"
is_lane=false
lane_port=""
for argument in "$@"; do
    [[ "$argument" == "-N" ]] && is_lane=true
    if [[ "$argument" =~ ^127\.0\.0\.1:(1808[1-8]): ]]; then
        lane_port="${BASH_REMATCH[1]}"
    fi
done
last="${!#}"
write_session_receipt() {
    local eof_observed="$1"
    local command_status="$2"
    local recorded_at
    recorded_at="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
    [[ "$last" =~ BLUEMAP_PHASE_SESSION_ID=([a-f0-9]{64}) ]] || exit 96
    session_id="${BASH_REMATCH[1]}"
    [[ "$last" =~ BLUEMAP_PHASE_SESSION_OUTPUT=([^[:space:]]+) ]] || exit 95
    session_output="${BASH_REMATCH[1]}"
    jq -n \
        --arg sessionId "$session_id" \
        --arg sessionOutput "$session_output" \
        --arg recordedAt "$recorded_at" \
        --argjson eofObserved "$eof_observed" \
        --argjson commandStatus "$command_status" \
        --argjson processGroupId "$$" \
        '{
            kind: "runpod-command-session",
            formatVersion: 2,
            sessionId: $sessionId,
            sessionOutput: $sessionOutput,
            activeLock: "/tmp/bluemap-runpod-active-phase.lock",
            startedAt: $recordedAt,
            completedAt: $recordedAt,
            telemetry: {
                resourceOutput: env.FAKE_REMOTE_RESOURCE_OUTPUT,
                readyBeforeWorkload: true,
                readyAt: $recordedAt,
                workloadReleasedAt: $recordedAt,
                samplerExitStatus: 0
            },
            lease: {
                required: true,
                eofObserved: $eofObserved,
                protocolViolation: false,
                observedAt: (if $eofObserved
                    then $recordedAt else null end)
            },
            termination: {
                requested: $eofObserved,
                termSignal: (if $eofObserved then "TERM" else null end),
                killEscalated: false,
                commandExitStatus: $commandStatus,
                processGroupId: $processGroupId,
                processGroupEmpty: true,
                watcherReaped: true,
                samplerReaped: true
            },
            passed: true
        }' >"$FAKE_REMOTE_SESSION"
}
if [[ "$is_lane" == true ]]; then
    [[ -n "$lane_port" ]] || exit 98
    printf '%s\n' "$$" >"${FAKE_LANE_PID_PREFIX}.${lane_port}"
    : >"${FAKE_READY_PREFIX}.${lane_port}"
    if [[ "${FAKE_FAIL_LANE:-}" == "${lane_port#1808}" ]]; then
        sleep 0.5
        exit 42
    fi
    trap 'exit 143' TERM INT
    while :; do sleep 0.05; done
elif [[ "$last" == *"bluemap-runpod-identity"* ]]; then
    exec cat "$FAKE_LIVE_IDENTITY"
elif [[ "$last" == *"curl"* ]]; then
    [[ "$last" =~ :((1808[1-8]))/ ]] || exit 97
    marker="${FAKE_READY_PREFIX}.${BASH_REMATCH[1]}"
    for _ in {1..100}; do
        [[ -e "$marker" ]] && break
        sleep 0.01
    done
    [[ -e "$marker" ]] || exit 1
    printf '200'
elif [[ "$*" == *"for path do"* ]]; then
    [[ "${FAKE_STALE_OUTPUT:-false}" != true ]]
elif [[ "$last" == *"termination.processGroupId"* ]]; then
    cat "$FAKE_REMOTE_SESSION"
elif [[ "$last" == *"sourceSha256"* && "$last" == *"sha256sum"* ]]; then
    jq -nc \
        --arg sha256 "$FAKE_RUNPOD_RESOURCE_SHA256" \
        --arg sourceSha256 "$FAKE_RUNPOD_SOURCE_SHA256" \
        '{sha256: $sha256, count: 2, sourceSha256: $sourceSha256}'
elif [[ "$last" == *"test-command"* ]]; then
    if [[ "${FAKE_FAIL_LANE:-}" == "2" ||
        "${FAKE_HANG_COMMAND:-false}" == true ]]; then
        [[ -z "${FAKE_COMMAND_PID:-}" ]] || printf '%s\n' "$$" >"$FAKE_COMMAND_PID"
        IFS= read -r handshake || exit 93
        [[ "$handshake" =~ ^bluemap-phase-lease-v1:[a-f0-9]{64}$ ]] || exit 92
        if IFS= read -r _; then
            exit 94
        fi
        write_session_receipt true 143
        [[ -z "${FAKE_COMMAND_EOF:-}" ]] || : >"$FAKE_COMMAND_EOF"
        exit 143
    fi
    write_session_receipt false "${FAKE_COMMAND_STATUS:-0}"
    exit "${FAKE_COMMAND_STATUS:-0}"
else
    exit 0
fi
"""

FAKE_SCP = r"""#!/usr/bin/env bash
set -Eeuo pipefail
source_file="${@: -2:1}"
cp -- "$source_file" "$FAKE_EVIDENCE"
"""

FAKE_CONTROLLER_SAMPLER = r"""#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import time

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
parser.add_argument("--ready-file", required=True)
parser.add_argument("--stop-file", required=True)
parser.add_argument("--source-sha256", required=True)
parser.add_argument("--expected-remote-address", required=True)
parser.add_argument("--expected-remote-port", required=True, type=int)
parser.add_argument("--interval-seconds")
parser.add_argument("--lane", action="append", default=[])
args = parser.parse_args()

lanes = []
for value in args.lane:
    lane_id, raw_pid = value.split("=", 1)
    lanes.append({
        "id": lane_id,
        "process": {
            "pid": int(raw_pid),
            "startTimeTicks": 1,
            "userTicks": 0,
            "systemTicks": 0,
        },
        "socket": {
            "remote": {
                "address": args.expected_remote_address,
                "port": args.expected_remote_port,
            }
        },
    })
sample = {
    "formatVersion": 2,
    "kind": "controller-ssh-transport-sample",
    "sourceSha256": args.source_sha256,
    "lanes": lanes,
}
with open(args.output, "w", encoding="utf-8") as output:
    output.write(json.dumps(sample) + "\n")
    output.write(json.dumps(sample) + "\n")
    output.flush()
    os.fsync(output.fileno())
ready = {
    "formatVersion": 1,
    "kind": "controller-ssh-transport-ready",
    "sampleCount": 1,
    "sourceSha256": args.source_sha256,
}
temporary = args.ready_file + ".tmp"
with open(temporary, "w", encoding="utf-8") as destination:
    json.dump(ready, destination)
os.replace(temporary, args.ready_file)
while not os.path.exists(args.stop_file):
    time.sleep(0.01)
"""

FAKE_RUNPOD_SAMPLER_SOURCE = """#!/usr/bin/env bash
exit 0
"""


class RunPodHelperBehaviorTests(unittest.TestCase):
    def runner_validation(
        self, root: Path, evidence: dict[str, object], helper_status: int
    ) -> subprocess.CompletedProcess[str]:
        source = RUNNER.read_text(encoding="utf-8")
        function_start = source.index("validate_ssh_l4_transport_evidence() {")
        function_end = source.index("\n}\n\nrun_k6_phase()", function_start)
        function = source[function_start:function_end]
        marker = '--arg expectedTransportOutput "$expected_transport_output" \\\n'
        program_start = function.index("        '\n", function.index(marker)) + 10
        program_end = function.rindex("\n        ' \"$artifact\"")
        program = function[program_start:program_end]
        artifact = root / "runner-transport.json"
        artifact.write_text(json.dumps(evidence), encoding="utf-8")
        session = evidence["commandSession"]
        return subprocess.run(
            [
                "jq",
                "-e",
                "--argjson",
                "helperStatus",
                str(helper_status),
                "--arg",
                "expectedTransportOutput",
                "/artifacts/case/phase/ssh-l4-transport.json",
                "--arg",
                "expectedRemoteAddress",
                "203.0.113.10",
                "--argjson",
                "expectedRemotePort",
                "22",
                "--arg",
                "expectedImageDigest",
                "sha256:" + "a" * 64,
                program,
                str(artifact),
            ],
            capture_output=True,
            check=False,
            text=True,
        )

    def assert_runner_accepts(
        self, root: Path, evidence: dict[str, object], helper_status: int
    ) -> None:
        result = self.runner_validation(root, evidence, helper_status)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_helper(
        self,
        root: Path,
        *,
        command_status: int = 0,
        fail_lane: int | None = None,
        hang_command: bool = False,
        fast_deadline: bool = False,
        stale_output: bool = False,
    ) -> tuple[
        subprocess.CompletedProcess[str], dict[str, object] | None, list[int], str
    ]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        ssh = fake_bin / "ssh"
        scp = fake_bin / "scp"
        ssh.write_text(FAKE_SSH, encoding="utf-8")
        scp.write_text(FAKE_SCP, encoding="utf-8")
        ssh.chmod(0o755)
        scp.chmod(0o755)

        helper_dir = root / "helper"
        helper_dir.mkdir()
        helper_path = helper_dir / "runpod_loadgen.sh"
        helper_source = HELPER.read_text(encoding="utf-8")
        if fast_deadline:
            helper_source = helper_source.replace(
                "phase_timeout_seconds + 60",
                "phase_timeout_seconds + 0",
            )
        helper_path.write_text(helper_source, encoding="utf-8")
        helper_path.chmod(0o755)
        controller_sampler = helper_dir / "sample_ssh_transport.py"
        controller_sampler.write_text(
            FAKE_CONTROLLER_SAMPLER, encoding="utf-8"
        )
        controller_sampler.chmod(0o755)
        runpod_sampler = helper_dir / "runpod-sample-resources.sh"
        runpod_sampler.write_text(
            FAKE_RUNPOD_SAMPLER_SOURCE, encoding="utf-8"
        )
        runpod_sampler.chmod(0o755)

        identity = root / "identity.json"
        live_identity = root / "live-identity.json"
        key = root / "id_ed25519"
        evidence = root / "transport.json"
        ssh_log = root / "ssh.log"
        lane_pid_prefix = root / "lane-pid"
        remote_session = root / "remote-session.json"
        identity.write_text(
            json.dumps(runpod_identity("formal-helper-behavior")),
            encoding="utf-8",
        )
        live_identity.write_text(
            json.dumps(runpod_runtime_identity("formal-helper-behavior")),
            encoding="utf-8",
        )
        key.write_text("test-private-key\n", encoding="utf-8")
        key.chmod(0o600)
        ssh_log.touch()

        resource_output = (
            "/artifacts/case/phase/load-generator-resources.ndjson"
        )
        runpod_source_sha256 = hashlib.sha256(
            FAKE_RUNPOD_SAMPLER_SOURCE.encode("utf-8")
        ).hexdigest()
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_LIVE_IDENTITY": str(live_identity),
            "FAKE_EVIDENCE": str(evidence),
            "FAKE_SSH_LOG": str(ssh_log),
            "FAKE_LANE_PID_PREFIX": str(lane_pid_prefix),
            "FAKE_READY_PREFIX": str(root / "lane-ready"),
            "FAKE_REMOTE_SESSION": str(remote_session),
            "FAKE_COMMAND_STATUS": str(command_status),
            "FAKE_DATE_COUNTER": str(root / "date-counter"),
            "FAKE_COMMAND_PID": str(root / "command.pid"),
            "FAKE_COMMAND_EOF": str(root / "command.eof"),
            "FAKE_REMOTE_RESOURCE_OUTPUT": resource_output,
            "FAKE_RUNPOD_SOURCE_SHA256": runpod_source_sha256,
            "FAKE_RUNPOD_RESOURCE_SHA256": "b" * 64,
        }
        if fail_lane is not None:
            environment["FAKE_FAIL_LANE"] = str(fail_lane)
        if hang_command:
            environment["FAKE_HANG_COMMAND"] = "true"
        if stale_output:
            environment["FAKE_STALE_OUTPUT"] = "true"
        result = subprocess.run(
            [
                "bash",
                str(helper_path),
                "--identity",
                str(identity),
                "--identity-key",
                str(key),
                "exec-traefik-forward",
                "--transport-output",
                "/artifacts/case/phase/ssh-l4-transport.json",
                "--",
                "env",
                f"BLUEMAP_PHASE_TIMEOUT_SECONDS={1 if fast_deadline else 5}",
                "test-command",
                "--resource-output",
                resource_output,
            ],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=15,
        )
        payload = (
            json.loads(evidence.read_text(encoding="utf-8"))
            if evidence.is_file()
            else None
        )
        pids = [
            int(Path(f"{lane_pid_prefix}.{port}").read_text(encoding="utf-8"))
            for port in range(18081, 18089)
            if Path(f"{lane_pid_prefix}.{port}").is_file()
        ]
        return result, payload, pids, ssh_log.read_text(encoding="utf-8")

    def assert_processes_gone(self, pids: list[int]) -> None:
        for pid in pids:
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"transport child {pid} survived helper cleanup")

    def test_healthy_lanes_preserve_nonzero_command_status_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, evidence, pids, _ = self.run_helper(root, command_status=17)
            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assert_runner_accepts(root, evidence, 17)
            started = analyze.timestamp_epoch(evidence["startedAt"], "startedAt")
            finished = analyze.timestamp_epoch(evidence["finishedAt"], "finishedAt")
            with patch.object(
                analyze,
                "validate_transport_telemetry_artifacts",
                return_value={"capturePassed": True},
            ):
                analyzed = analyze.validate_ssh_l4_transport_artifact(
                    root / "transport.json",
                    "measurement",
                    (started - 1, finished + 1),
                    17,
                    "/artifacts/case/phase/ssh-l4-transport.json",
                )
            self.assertTrue(analyzed["passed"])

        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertIs(evidence["passed"], True)
        self.assertEqual(evidence["commandExitStatus"], 17)
        self.assertIsNone(evidence["failure"])
        self.assertTrue(evidence["commandSession"]["required"])
        self.assertTrue(evidence["commandSession"]["confirmed"])
        self.assertEqual(
            evidence["commandSession"]["leaseCloseReason"],
            "after-command-exit",
        )
        self.assertFalse(
            evidence["commandSession"]["receipt"]["lease"]["eofObserved"]
        )
        self.assertTrue(
            evidence["commandSession"]["receipt"]["termination"][
                "processGroupEmpty"
            ]
        )
        self.assertEqual(evidence["topology"], analyze.SSH_L4_TRAEFIK_TUNNEL)
        self.assertEqual(evidence["topology"], orchestrate.SSH_L4_TRAEFIK_TUNNEL)
        self.assertEqual(
            [lane["listenPort"] for lane in evidence["lanes"]],
            list(range(18081, 18089)),
        )
        self.assertTrue(all(lane["preProbe"]["passed"] for lane in evidence["lanes"]))
        self.assertTrue(all(lane["postProbe"]["passed"] for lane in evidence["lanes"]))
        self.assert_processes_gone(pids)

    def test_lane_loss_terminates_command_and_returns_reserved_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, evidence, pids, _ = self.run_helper(root, fail_lane=2)
            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assert_runner_accepts(root, evidence, 86)

        self.assertEqual(result.returncode, 86, result.stderr)
        self.assertIs(evidence["passed"], False)
        self.assertTrue(evidence["commandTerminatedForLaneFailure"])
        self.assertEqual(evidence["failure"], "lane-2-exited-during-command")
        self.assertTrue(evidence["commandSession"]["confirmed"])
        self.assertEqual(
            evidence["commandSession"]["leaseCloseReason"], "lane-failure"
        )
        self.assertTrue(
            evidence["commandSession"]["receipt"]["lease"]["eofObserved"]
        )
        self.assertTrue(
            evidence["commandSession"]["receipt"]["termination"][
                "processGroupEmpty"
            ]
        )
        self.assertTrue(evidence["lanes"][1]["exitedEarly"])
        self.assertEqual(evidence["lanes"][1]["exitStatus"], 42)
        self.assert_processes_gone(pids)

    def test_unconfirmed_remote_session_is_structural_but_requires_fatal_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, evidence, pids, _ = self.run_helper(root)
            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assertEqual(result.returncode, 0, result.stderr)
            unconfirmed = copy.deepcopy(evidence)
            unconfirmed["commandSession"]["confirmed"] = False
            unconfirmed["commandSession"]["receipt"] = None
            unconfirmed["failure"] = "command-session-unconfirmed"
            unconfirmed["passed"] = False
            self.assert_runner_accepts(root, unconfirmed, 86)
            fatal_gate = subprocess.run(
                [
                    "jq",
                    "-e",
                    (
                        ".commandSession.required == true and "
                        ".commandSession.confirmed != true"
                    ),
                ],
                input=json.dumps(unconfirmed),
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(fatal_gate.returncode, 0)
            self.assertIn(
                "remote process-group termination is unconfirmed",
                RUNNER.read_text(encoding="utf-8"),
            )
            self.assert_processes_gone(pids)

    def test_stale_output_is_rejected_before_tunnels_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, evidence, pids, ssh_log = self.run_helper(
                root, stale_output=True
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("output already exists", result.stderr)
        self.assertIsNone(evidence)
        self.assertEqual(pids, [])
        self.assertNotIn(" -N ", f" {ssh_log} ")

    def test_helper_deadline_closes_lease_and_confirms_remote_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, evidence, pids, _ = self.run_helper(
                root,
                hang_command=True,
                fast_deadline=True,
            )
            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assert_runner_accepts(root, evidence, 86)

        self.assertEqual(result.returncode, 86, result.stderr)
        self.assertEqual(evidence["failure"], "command-session-helper-deadline")
        self.assertEqual(
            evidence["commandSession"]["leaseCloseReason"], "helper-deadline"
        )
        self.assertTrue(evidence["commandSession"]["confirmed"])
        self.assertTrue(
            evidence["commandSession"]["receipt"]["lease"]["eofObserved"]
        )
        self.assert_processes_gone(pids)

    def test_runner_rejects_malformed_failed_transport_lifecycles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, evidence, pids, _ = self.run_helper(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(evidence)
            assert evidence is not None
            mutations: list[dict[str, object]] = []

            inconsistent_status = copy.deepcopy(evidence)
            inconsistent_status["failure"] = "synthetic-failure"
            inconsistent_status["passed"] = False
            inconsistent_status["lanes"][0]["preProbe"]["httpStatus"] = 500
            mutations.append(inconsistent_status)

            unattempted_timestamp = copy.deepcopy(evidence)
            unattempted_timestamp["failure"] = "synthetic-failure"
            unattempted_timestamp["passed"] = False
            unattempted_timestamp["lanes"][0]["preProbe"]["attempted"] = False
            unattempted_timestamp["lanes"][0]["preProbe"]["passed"] = False
            mutations.append(unattempted_timestamp)

            probe_without_start = copy.deepcopy(evidence)
            probe_without_start["failure"] = "synthetic-failure"
            probe_without_start["passed"] = False
            probe_without_start["lanes"][0]["started"] = False
            probe_without_start["lanes"][0]["startedAt"] = None
            mutations.append(probe_without_start)

            reversed_outer = copy.deepcopy(evidence)
            reversed_outer["failure"] = "synthetic-failure"
            reversed_outer["passed"] = False
            reversed_outer["startedAt"], reversed_outer["finishedAt"] = (
                reversed_outer["finishedAt"],
                reversed_outer["startedAt"],
            )
            mutations.append(reversed_outer)

            cancelled_but_claimed_passed = copy.deepcopy(evidence)
            cancelled_but_claimed_passed["commandSession"]["receipt"][
                "lease"
            ].update(
                {
                    "eofObserved": True,
                    "observedAt": cancelled_but_claimed_passed["commandSession"][
                        "receipt"
                    ]["startedAt"],
                }
            )
            cancelled_but_claimed_passed["commandSession"]["receipt"][
                "termination"
            ].update(
                {
                    "requested": True,
                    "termSignal": "TERM",
                }
            )
            mutations.append(cancelled_but_claimed_passed)

            unterminated_lane = copy.deepcopy(evidence)
            unterminated_lane["failure"] = "synthetic-failure"
            unterminated_lane["passed"] = False
            unterminated_lane["lanes"][0].update(
                {
                    "stoppedByHelper": False,
                    "exitedEarly": False,
                    "exitStatus": None,
                }
            )
            mutations.append(unterminated_lane)

            for index, mutation in enumerate(mutations):
                with self.subTest(index=index):
                    helper_status = (
                        0 if mutation is cancelled_but_claimed_passed else 86
                    )
                    validation = self.runner_validation(
                        root, mutation, helper_status
                    )
                    self.assertNotEqual(validation.returncode, 0)
            self.assert_processes_gone(pids)

    def test_helper_sigkill_terminates_all_supervised_ssh_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            ssh = fake_bin / "ssh"
            scp = fake_bin / "scp"
            ssh.write_text(FAKE_SSH, encoding="utf-8")
            scp.write_text(FAKE_SCP, encoding="utf-8")
            ssh.chmod(0o755)
            scp.chmod(0o755)
            helper_dir = root / "helper"
            helper_dir.mkdir()
            helper_path = helper_dir / "runpod_loadgen.sh"
            helper_path.write_text(
                HELPER.read_text(encoding="utf-8"), encoding="utf-8"
            )
            helper_path.chmod(0o755)
            controller_sampler = helper_dir / "sample_ssh_transport.py"
            controller_sampler.write_text(
                FAKE_CONTROLLER_SAMPLER, encoding="utf-8"
            )
            controller_sampler.chmod(0o755)
            runpod_sampler = helper_dir / "runpod-sample-resources.sh"
            runpod_sampler.write_text(
                FAKE_RUNPOD_SAMPLER_SOURCE, encoding="utf-8"
            )
            runpod_sampler.chmod(0o755)

            identity = root / "identity.json"
            live_identity = root / "live-identity.json"
            key = root / "id_ed25519"
            ssh_log = root / "ssh.log"
            lane_pid_prefix = root / "lane-pid"
            command_pid_path = root / "command.pid"
            command_eof = root / "command.eof"
            resource_output = (
                "/artifacts/case/phase/load-generator-resources.ndjson"
            )
            identity.write_text(
                json.dumps(runpod_identity("formal-helper-behavior")),
                encoding="utf-8",
            )
            live_identity.write_text(
                json.dumps(runpod_runtime_identity("formal-helper-behavior")),
                encoding="utf-8",
            )
            key.write_text("test-private-key\n", encoding="utf-8")
            key.chmod(0o600)
            ssh_log.touch()
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "FAKE_LIVE_IDENTITY": str(live_identity),
                "FAKE_EVIDENCE": str(root / "transport.json"),
                "FAKE_SSH_LOG": str(ssh_log),
                "FAKE_LANE_PID_PREFIX": str(lane_pid_prefix),
                "FAKE_READY_PREFIX": str(root / "lane-ready"),
                "FAKE_REMOTE_SESSION": str(root / "remote-session.json"),
                "FAKE_COMMAND_STATUS": "0",
                "FAKE_HANG_COMMAND": "true",
                "FAKE_COMMAND_PID": str(command_pid_path),
                "FAKE_COMMAND_EOF": str(command_eof),
                "FAKE_REMOTE_RESOURCE_OUTPUT": resource_output,
                "FAKE_RUNPOD_SOURCE_SHA256": hashlib.sha256(
                    FAKE_RUNPOD_SAMPLER_SOURCE.encode("utf-8")
                ).hexdigest(),
                "FAKE_RUNPOD_RESOURCE_SHA256": "b" * 64,
            }
            process = subprocess.Popen(
                [
                    "bash",
                    str(helper_path),
                    "--identity",
                    str(identity),
                    "--identity-key",
                    str(key),
                    "exec-traefik-forward",
                    "--transport-output",
                    "/artifacts/case/phase/ssh-l4-transport.json",
                    "--",
                    "env",
                    "BLUEMAP_PHASE_TIMEOUT_SECONDS=5",
                    "test-command",
                    "--resource-output",
                    resource_output,
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(300):
                if command_pid_path.is_file():
                    break
                time.sleep(0.02)
            self.assertTrue(command_pid_path.is_file())
            lease_targets = {
                path.resolve(strict=False)
                for path in Path(f"/proc/{process.pid}/fd").iterdir()
                if path.is_symlink() and path.resolve(strict=False).name == "stdin"
            }
            self.assertEqual(len(lease_targets), 1)
            monitor_pid: int | None = None
            for _ in range(300):
                for proc in Path("/proc").iterdir():
                    if not proc.name.isdigit():
                        continue
                    try:
                        stat_tail = (proc / "stat").read_text().rsplit(") ", 1)[1]
                        parent_pid = int(stat_tail.split()[1])
                        command_name = (proc / "comm").read_text().strip()
                    except (FileNotFoundError, IndexError, ValueError):
                        continue
                    if parent_pid == process.pid and command_name == "sleep":
                        monitor_pid = int(proc.name)
                        break
                if monitor_pid is not None:
                    break
                time.sleep(0.005)
            self.assertIsNotNone(monitor_pid)
            assert monitor_pid is not None
            monitor_targets = {
                path.resolve(strict=False)
                for path in Path(f"/proc/{monitor_pid}/fd").iterdir()
                if path.is_symlink()
            }
            self.assertTrue(lease_targets.isdisjoint(monitor_targets))
            os.kill(process.pid, 9)
            self.assertEqual(process.wait(timeout=5), -9)
            lane_pids = [
                int(Path(f"{lane_pid_prefix}.{port}").read_text(encoding="utf-8"))
                for port in range(18081, 18089)
            ]
            process.communicate(timeout=5)
            command_pid = int(command_pid_path.read_text(encoding="utf-8"))
            self.assert_processes_gone([*lane_pids, command_pid])


if __name__ == "__main__":
    unittest.main()
