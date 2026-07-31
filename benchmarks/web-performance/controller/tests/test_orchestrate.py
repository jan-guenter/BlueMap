from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from support import (  # noqa: E402
    RUN_ID,
    analyze,
    orchestrate,
    runpod_identity,
    schedule_entry,
    write_json,
)


def option(command: list[str], flag: str) -> str:
    index = command.index(flag)
    return command[index + 1]


class RunPodOrchestratorTests(unittest.TestCase):
    def test_allowlist_contains_only_six_disposable_deployments(self) -> None:
        orchestrate.validate_target_constants()
        self.assertEqual(len(orchestrate.TARGETS), 6)
        self.assertEqual(len(orchestrate.FORMAL_DEPLOYMENTS), 6)
        self.assertNotIn("minecraft", orchestrate.FORMAL_DEPLOYMENTS)
        for deployment in orchestrate.FORMAL_DEPLOYMENTS:
            command = orchestrate.scale_command(
                deployment,
                0,
                kubeconfig=Path("/tmp/controller-test-kubeconfig"),
            )
            self.assertIn(f"deployment/{deployment}", command)
            self.assertNotIn("deployment/minecraft", command)
        with self.assertRaises(orchestrate.SafetyError):
            orchestrate.scale_command(
                "minecraft",
                0,
                kubeconfig=Path("/tmp/controller-test-kubeconfig"),
            )

    def test_load_runpod_identity_binds_run_id_and_refuses_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_path = root / "identity.json"
            write_json(identity_path, runpod_identity())
            loaded = orchestrate.load_runpod_identity(identity_path, RUN_ID)
            self.assertEqual(loaded, runpod_identity())

            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "run ID",
            ):
                orchestrate.load_runpod_identity(identity_path, "another-run")

            symlink = root / "identity-link.json"
            symlink.symlink_to(identity_path)
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "non-symlink",
            ):
                orchestrate.load_runpod_identity(symlink, RUN_ID)

    def test_runpod_controls_require_private_owned_key_and_public_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_path = root / "identity.json"
            key_path = root / "id_ed25519"
            write_json(identity_path, runpod_identity())
            key_path.write_text("private-test-key\n", encoding="utf-8")
            key_path.chmod(0o600)
            args = argparse.Namespace(
                load_generator_backend="runpod-ssh",
                load_generator_identity=identity_path,
                load_generator_identity_key=key_path,
                formal_run_id=RUN_ID,
                traffic_base_url=orchestrate.DEFAULT_TRAFFIC_BASE_URL,
                traffic_service=orchestrate.TRAFFIC_SERVICE,
                traffic_service_port=orchestrate.TRAFFIC_SERVICE_PORT,
                require_edge_bypass=True,
            )
            self.assertEqual(
                orchestrate.validate_runpod_controls(args),
                runpod_identity(),
            )

            non_exact = runpod_identity()
            non_exact["runpod"]["vcpuCount"] = 4
            write_json(identity_path, non_exact)
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "exact cpu5c/8-vCPU/500/100",
            ):
                orchestrate.validate_runpod_controls(args)
            write_json(identity_path, runpod_identity())

            key_path.chmod(0o640)
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "group or others",
            ):
                orchestrate.validate_runpod_controls(args)
            key_path.chmod(0o600)

            with patch.object(
                orchestrate.os,
                "getuid",
                return_value=os.getuid() + 1,
            ):
                with self.assertRaisesRegex(
                    orchestrate.SafetyError,
                    "owned by the controller user",
                ):
                    orchestrate.validate_runpod_controls(args)

            args.traffic_base_url = "http://127.0.0.1:8100"
            with self.assertRaisesRegex(
                orchestrate.SafetyError,
                "Traffic base URL",
            ):
                orchestrate.validate_runpod_controls(args)

    def test_execution_identity_is_canonical_and_never_archives_key_material(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_path = root / "identity.json"
            key_path = root / "id_ed25519"
            runner = root / "run_origin_case.sh"
            benchmark_python = root / "python"
            kubeconfig = root / "kubeconfig"
            write_json(identity_path, runpod_identity())
            key_path.write_text("never-archive-this-secret\n", encoding="utf-8")
            key_path.chmod(0o600)
            runner.write_text("#!/bin/sh\n", encoding="utf-8")
            benchmark_python.write_bytes(b"python-test-binary")
            kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
            args = argparse.Namespace(
                load_generator_backend="runpod-ssh",
                load_generator_identity=identity_path,
                load_generator_identity_key=key_path,
                formal_run_id=RUN_ID,
                traffic_base_url=orchestrate.DEFAULT_TRAFFIC_BASE_URL,
                traffic_service=orchestrate.TRAFFIC_SERVICE,
                traffic_service_port=orchestrate.TRAFFIC_SERVICE_PORT,
                require_edge_bypass=True,
                runner=runner,
                benchmark_python=benchmark_python,
                kubeconfig=kubeconfig,
                no_prometheus=False,
                prometheus_url=orchestrate.DEFAULT_PROMETHEUS_URL,
                transition_timeout_seconds=300,
                metrics_timeout_seconds=180,
                poll_interval_seconds=2.0,
            )
            identity = orchestrate.execution_identity(args)
            self.assertEqual(
                identity["loadGeneratorIdentitySha256"],
                analyze.canonical_sha256(identity["loadGeneratorIdentity"]),
            )
            self.assertEqual(identity, analyze.validate_execution_identity(identity))
            serialized = json.dumps(identity, sort_keys=True)
            self.assertNotIn("never-archive-this-secret", serialized)
            self.assertNotIn(str(key_path), serialized)
            self.assertNotIn("loadgenPod", serialized)

            tampered = copy.deepcopy(identity)
            tampered["loadGeneratorIdentity"]["runpod"]["machineId"] = "changed"
            with self.assertRaisesRegex(
                analyze.AnalysisFailure,
                "identity digest differs",
            ):
                analyze.validate_execution_identity(tampered)

    def test_runner_command_uses_runpod_public_traffic_and_direct_origin(
        self,
    ) -> None:
        entry = schedule_entry()
        target = orchestrate.TARGETS[entry["variantId"]]
        web_pods = [
            f"{target.deployment}-resolved-pod-{index}"
            for index in range(1, target.replica_count + 1)
        ]
        options = orchestrate.RunnerOptions(
            runner=Path("/opt/bluemap/benchmarks/web-performance/tools/run_origin_case.sh"),
            matrix=Path("/frozen/matrix.json"),
            schedule=Path("/frozen/schedule.json"),
            manifest=Path("/frozen/manifest.json"),
            artifact_root=Path("/artifacts/results"),
            benchmark_python=Path("/opt/venv/bin/python"),
            kubeconfig=Path("/opt/controller/kubeconfig"),
            prometheus_url=orchestrate.DEFAULT_PROMETHEUS_URL,
            load_generator_identity=Path("/identity/identity.json"),
            load_generator_identity_key=Path("/credentials/id_ed25519"),
            traffic_base_url=orchestrate.DEFAULT_TRAFFIC_BASE_URL,
            traffic_service=orchestrate.TRAFFIC_SERVICE,
            traffic_service_port=orchestrate.TRAFFIC_SERVICE_PORT,
            formal_run_id=RUN_ID,
            require_edge_bypass=True,
        )
        command = orchestrate.build_runner_command(
            entry,
            target,
            web_pods,
            options,
        )
        self.assertEqual(option(command, "--load-generator-backend"), "runpod-ssh")
        self.assertEqual(
            option(command, "--traffic-base-url"),
            "https://bluemap-test.guenter.cloud",
        )
        self.assertEqual(
            option(command, "--origin-base-url"),
            (
                f"http://{target.service}.minecraft.svc.cluster.local:"
                f"{target.port}"
            ),
        )
        self.assertEqual(
            option(command, "--prometheus-url"),
            orchestrate.DEFAULT_PROMETHEUS_URL,
        )
        self.assertEqual(option(command, "--traffic-service"), "bluemap-perf-public")
        self.assertEqual(option(command, "--traffic-service-port"), "8100")
        self.assertEqual(option(command, "--formal-run-id"), RUN_ID)
        self.assertIn("--require-edge-bypass", command)
        self.assertEqual(
            [
                command[index + 1]
                for index, value in enumerate(command)
                if value == "--web-pod"
            ],
            web_pods,
        )
        joined = "\n".join(command)
        self.assertNotIn("--loadgen-pod", joined)
        self.assertNotIn("bluemap-perf-loadgen", joined)
        for protected in orchestrate.PROTECTED_RESOURCES:
            self.assertNotIn(protected, joined)

    def test_static_target_validation_has_no_kubernetes_loadgen_resource(
        self,
    ) -> None:
        target = orchestrate.TARGETS["java-new-postgresql"]
        calls: list[tuple[str, str]] = []

        class FakeKube:
            @staticmethod
            def service(name: str) -> dict[str, object]:
                calls.append(("service", name))
                return {
                    "kind": "Service",
                    "metadata": {
                        "name": name,
                        "namespace": orchestrate.NAMESPACE,
                    },
                }

            @staticmethod
            def configmap(name: str) -> dict[str, object]:
                calls.append(("configmap", name))
                return {
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": name,
                        "namespace": orchestrate.NAMESPACE,
                    },
                }

            @staticmethod
            def pod(name: str) -> dict[str, object]:
                raise AssertionError(f"unexpected Kubernetes Pod lookup: {name}")

        orchestrate.validate_static_target_resources(FakeKube(), target)
        self.assertEqual(
            calls,
            [
                ("service", target.service),
                ("service", orchestrate.TRAFFIC_SERVICE),
                *(("configmap", name) for name in target.configmaps),
            ],
        )
        self.assertFalse(
            any("loadgen" in name for _kind, name in calls),
            calls,
        )


if __name__ == "__main__":
    unittest.main()
