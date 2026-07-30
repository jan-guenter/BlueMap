from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

BENCHMARK_ROOT = Path(__file__).parents[1]
TOOLS_DIR = BENCHMARK_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import run_guarded_slow_reader as guarded

EXPERIMENT_ID = "java-new-postgresql"
DEPLOYMENT_NAME = "bluemap-perf-java-new-postgresql"
REPLICASET_NAME = f"{DEPLOYMENT_NAME}-7d9f6d8c5b"
POD_NAME = f"{REPLICASET_NAME}-abcde"
NAMESPACE = "minecraft"


def safety_labels() -> dict[str, str]:
    return {
        guarded.PART_OF_LABEL: guarded.PART_OF_VALUE,
        guarded.EXPERIMENT_LABEL: EXPERIMENT_ID,
        "app.kubernetes.io/name": "bluemap-web",
        "app.kubernetes.io/instance": DEPLOYMENT_NAME,
    }


def resources() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    labels = safety_labels()
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": DEPLOYMENT_NAME,
            "namespace": NAMESPACE,
            "uid": "deployment-uid",
            "labels": labels,
        },
        "spec": {
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "bluemap-web",
                    "app.kubernetes.io/instance": DEPLOYMENT_NAME,
                }
            }
        },
    }
    replicaset = {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {
            "name": REPLICASET_NAME,
            "namespace": NAMESPACE,
            "uid": "replicaset-uid",
            "labels": labels,
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": DEPLOYMENT_NAME,
                    "uid": "deployment-uid",
                    "controller": True,
                }
            ],
        },
        "spec": {
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "bluemap-web",
                    "app.kubernetes.io/instance": DEPLOYMENT_NAME,
                }
            }
        },
    }
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": POD_NAME,
            "namespace": NAMESPACE,
            "uid": "pod-uid",
            "labels": labels,
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": REPLICASET_NAME,
                    "uid": "replicaset-uid",
                    "controller": True,
                }
            ],
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    return deployment, replicaset, pod


def validate(
    deployment: dict[str, Any],
    replicaset: dict[str, Any],
    pod: dict[str, Any],
) -> guarded.VerifiedTarget:
    return guarded.validate_target(
        deployment,
        replicaset,
        pod,
        namespace=NAMESPACE,
        deployment_name=DEPLOYMENT_NAME,
        pod_name=POD_NAME,
        experiment_id=EXPERIMENT_ID,
    )


class TargetValidationTests(unittest.TestCase):
    def test_accepts_exact_labeled_owned_selected_ready_target(self) -> None:
        target = validate(*resources())

        self.assertEqual(target.deployment_uid, "deployment-uid")
        self.assertEqual(target.replicaset_name, REPLICASET_NAME)
        self.assertEqual(target.pod_uid, "pod-uid")

    def test_explicitly_rejects_protected_and_nonbenchmark_names(self) -> None:
        for name in (
            "minecraft",
            "minecraft-data",
            "minecraft-maintenance-holder",
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(guarded.SafetyError, "explicitly rejects"),
            ):
                guarded.require_exact_benchmark_name(name, "--pod")

        for name in ("web", "pod/bluemap-perf-web", "bluemap-perf-*"):
            with self.subTest(name=name), self.assertRaises(guarded.SafetyError):
                guarded.require_exact_benchmark_name(name, "--pod")

    def test_requires_exact_nonempty_experiment_labels_on_both_resources(
        self,
    ) -> None:
        for resource_index, kind in ((0, "Deployment"), (2, "Pod")):
            with self.subTest(kind=kind):
                values = list(resources())
                values[resource_index]["metadata"]["labels"][
                    guarded.EXPERIMENT_LABEL
                ] = "another-experiment"
                with self.assertRaisesRegex(guarded.SafetyError, "must have exact"):
                    validate(*values)

        with self.assertRaises(guarded.SafetyError):
            guarded.require_experiment_id("")

    def test_requires_exact_part_of_labels_on_both_resources(self) -> None:
        for resource_index, kind in ((0, "Deployment"), (2, "Pod")):
            with self.subTest(kind=kind):
                values = list(resources())
                del values[resource_index]["metadata"]["labels"][guarded.PART_OF_LABEL]
                with self.assertRaisesRegex(guarded.SafetyError, guarded.PART_OF_LABEL):
                    validate(*values)

    def test_rejects_wrong_or_changed_controller_uids(self) -> None:
        deployment, replicaset, pod = resources()
        pod["metadata"]["ownerReferences"][0]["uid"] = "replacement-rs-uid"
        with self.assertRaisesRegex(guarded.SafetyError, "ReplicaSet UID"):
            validate(deployment, replicaset, pod)

        deployment, replicaset, pod = resources()
        replicaset["metadata"]["ownerReferences"][0]["uid"] = (
            "replacement-deployment-uid"
        )
        with self.assertRaisesRegex(guarded.SafetyError, "exact named Deployment"):
            validate(deployment, replicaset, pod)

    def test_requires_exact_resource_kinds_and_api_versions(self) -> None:
        deployment, replicaset, pod = resources()
        deployment["apiVersion"] = "extensions/v1beta1"
        with self.assertRaisesRegex(guarded.SafetyError, "apps/v1"):
            validate(deployment, replicaset, pod)

        deployment, replicaset, pod = resources()
        pod["kind"] = "Service"
        with self.assertRaisesRegex(guarded.SafetyError, "v1 Pod"):
            validate(deployment, replicaset, pod)

    def test_requires_deployment_and_replicaset_to_select_pod(self) -> None:
        deployment, replicaset, pod = resources()
        deployment["spec"]["selector"]["matchLabels"]["app.kubernetes.io/instance"] = (
            "another-release"
        )
        with self.assertRaisesRegex(guarded.SafetyError, "Deployment selector"):
            validate(deployment, replicaset, pod)

        deployment, replicaset, pod = resources()
        replicaset["spec"]["selector"] = {
            "matchExpressions": [
                {
                    "key": "app.kubernetes.io/name",
                    "operator": "NotIn",
                    "values": ["bluemap-web"],
                }
            ]
        }
        with self.assertRaisesRegex(guarded.SafetyError, "ReplicaSet selector"):
            validate(deployment, replicaset, pod)

    def test_rejects_unready_or_terminating_pod(self) -> None:
        deployment, replicaset, pod = resources()
        pod["status"]["conditions"][0]["status"] = "False"
        with self.assertRaisesRegex(guarded.SafetyError, "not Ready"):
            validate(deployment, replicaset, pod)

        deployment, replicaset, pod = resources()
        pod["metadata"]["deletionTimestamp"] = "2026-07-30T00:00:00Z"
        with self.assertRaisesRegex(guarded.SafetyError, "already terminating"):
            validate(deployment, replicaset, pod)

    def test_reverification_rejects_any_identity_change(self) -> None:
        target = validate(*resources())
        replacement = guarded.VerifiedTarget(
            **{**target.__dict__, "pod_uid": "replacement-pod-uid"}
        )
        with self.assertRaisesRegex(guarded.SafetyError, "identity changed"):
            guarded.same_target(target, replacement)


class UIDPreconditionedDeletionTests(unittest.TestCase):
    def test_delete_uses_exact_raw_path_and_pod_uid_precondition(self) -> None:
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def runner(
            command: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"kind":"Pod","metadata":{"name":"test"}}',
                stderr="",
            )

        client = guarded.Kubectl(Path("/safe/kubeconfig"), NAMESPACE, runner)
        target = validate(*resources())
        client.delete_verified_pod(target, 30)

        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertIn(
            f"--raw=/api/v1/namespaces/{NAMESPACE}/pods/{POD_NAME}",
            command,
        )
        self.assertNotIn("minecraft-data", " ".join(command))
        options = json.loads(kwargs["input"])
        self.assertEqual(options["preconditions"], {"uid": "pod-uid"})
        self.assertEqual(options["gracePeriodSeconds"], 30)
        self.assertFalse(
            any(argument in {"--all", "--selector", "-l"} for argument in command)
        )


class StaticProcedureTests(unittest.TestCase):
    def test_readme_uses_only_the_guarded_destructive_runner(self) -> None:
        readme = (BENCHMARK_ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## Graceful-drain slow-reader check", maxsplit=1)[
            1
        ].split("For public delivery tests", maxsplit=1)[0]

        self.assertIn("run_guarded_slow_reader.py", section)
        self.assertIn("--confirm-delete-pod", section)
        self.assertNotIn("kubectl", section)
        self.assertNotIn("delete pod", section)

    def test_large_object_path_cannot_inject_query_or_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            for path in ("//other-host/path", "/maps/world/tile?unsafe=1"):
                with self.subTest(path=path):
                    manifest.write_text(
                        json.dumps({"largeObject": path}), encoding="utf-8"
                    )
                    with self.assertRaises(guarded.SafetyError):
                        guarded.load_large_object(manifest)

    def test_rejects_unbounded_ports_timeouts_and_encoding_headers(self) -> None:
        valid = {
            "remote_port": 8100,
            "grace_period_seconds": 30,
            "rollout_timeout_seconds": 120,
            "bytes_per_second": 1024,
            "minimum_object_bytes": 1024,
            "request_timeout_seconds": 90.0,
            "ready_timeout_seconds": 30.0,
            "initial_delay_seconds": 2.0,
            "accept_encoding": "zstd",
        }
        for field, value in (
            ("remote_port", 65536),
            ("request_timeout_seconds", float("nan")),
            ("initial_delay_seconds", float("inf")),
            ("accept_encoding", "zstd\r\nX-Unsafe: true"),
        ):
            with self.subTest(field=field):
                arguments = argparse.Namespace(**{**valid, field: value})
                with self.assertRaises(guarded.SafetyError):
                    guarded.validate_numeric_args(arguments)


class BenchmarkValuesTests(unittest.TestCase):
    def test_java_candidates_use_the_same_container_relative_heap(self) -> None:
        kubernetes = BENCHMARK_ROOT / "kubernetes"
        for filename in (
            "java-postgresql-values.yaml",
            "java-optimized-postgresql-values.yaml",
        ):
            with self.subTest(filename=filename):
                values = (kubernetes / filename).read_text(encoding="utf-8")
                self.assertIn("name: JAVA_TOOL_OPTIONS", values)
                self.assertIn(
                    'value: "-XX:MaxRAMPercentage=70.0"',
                    values,
                )

    def test_candidates_use_implicit_app_version_and_external_secrets(self) -> None:
        kubernetes = BENCHMARK_ROOT / "kubernetes"
        candidates = {
            "java-optimized-postgresql-values.yaml": (
                "bluemap-perf-java-new-postgresql",
                "java-new-postgresql",
                "ghcr.io/jan-guenter/bluemap-web",
            ),
            "rust-postgresql-values.yaml": (
                "bluemap-perf-rust-postgresql",
                "rust-postgresql",
                "ghcr.io/jan-guenter/bluemap-web-rust",
            ),
        }
        for filename, (name, experiment_id, image) in candidates.items():
            with self.subTest(filename=filename):
                values = (kubernetes / filename).read_text(encoding="utf-8")
                self.assertIn(f"fullnameOverride: {name}", values)
                self.assertIn(f"repository: {image}", values)
                self.assertIn('tag: ""', values)
                self.assertGreaterEqual(
                    values.count(
                        f"bluemap.guenter.cloud/experiment-id: {experiment_id}"
                    ),
                    2,
                )
                self.assertIn("existingSecret: bluemap-perf-postgres", values)
                self.assertIn("bluemap-perf-postgres-ca", values)
                self.assertIn('cpu: "1"', values)
                self.assertIn("memory: 1Gi", values)
                self.assertNotRegex(values, r"(?m)^\s+(?:username|password):\s+\S")

    def test_horizontal_overlays_and_matrix_agree_on_three_replicas(self) -> None:
        kubernetes = BENCHMARK_ROOT / "kubernetes"
        java_overlay = (
            kubernetes / "java-optimized-postgresql-r3-values.yaml"
        ).read_text(encoding="utf-8")
        rust_overlay = (kubernetes / "rust-postgresql-r3-values.yaml").read_text(
            encoding="utf-8"
        )
        matrix = json.loads(
            (BENCHMARK_ROOT / "matrix.example.json").read_text(encoding="utf-8")
        )

        self.assertIn("replicaCount: 3", java_overlay)
        self.assertIn("maxConnections: 4", java_overlay)
        self.assertIn("replicaCount: 3", rust_overlay)
        self.assertIn("maxConnections: 4", rust_overlay)
        self.assertIn("maxInFlightRequests: 8", rust_overlay)

        variants = {variant["id"]: variant for variant in matrix["variants"]}
        self.assertEqual(variants["java-new-postgresql-r3"]["replicaCount"], 3)
        self.assertEqual(variants["rust-postgresql-r3"]["replicaCount"], 3)
        horizontal = next(
            case
            for case in matrix["cases"]
            if case["id"] == "map-mixed-horizontal-r40"
        )
        self.assertEqual(
            horizontal["variants"],
            [
                "java-new-postgresql",
                "rust-postgresql",
                "java-new-postgresql-r3",
                "rust-postgresql-r3",
            ],
        )

    def test_helm_values_regression_check_is_executable(self) -> None:
        script = BENCHMARK_ROOT / "kubernetes" / "test-helm-values.sh"
        self.assertTrue(script.stat().st_mode & 0o111)
        self.assertIn("--app-version", script.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
