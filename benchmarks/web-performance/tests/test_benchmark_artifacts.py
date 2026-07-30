from __future__ import annotations

# ruff: noqa: E402

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import yaml

BENCHMARK_ROOT = Path(__file__).parents[1]
TOOLS_DIR = BENCHMARK_ROOT / "tools"
KUBERNETES_DIR = BENCHMARK_ROOT / "kubernetes"
sys.path.insert(0, str(TOOLS_DIR))

import capture_prometheus
import check_arrival_gate
import configmap_references
import generate_schedule
import probe_delivery_cache
import runtime_identity
import sanitize_kubernetes_resource
import sanitize_configmap
import slow_reader


def persistent_volume_claim_references(value: object) -> list[dict[str, object]]:
    references: list[dict[str, object]] = []
    if isinstance(value, dict):
        claim = value.get("persistentVolumeClaim")
        if isinstance(claim, dict):
            references.append(claim)
        for child in value.values():
            references.extend(persistent_volume_claim_references(child))
    elif isinstance(value, list):
        for child in value:
            references.extend(persistent_volume_claim_references(child))
    return references


class KubernetesSnapshotTests(unittest.TestCase):
    def test_redacts_literal_credentials_but_preserves_references_and_digests(
        self,
    ) -> None:
        resource = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "bluemap-perf-web-abc",
                "namespace": "minecraft",
                "uid": "pod-uid",
                "annotations": {"example.invalid/password": "must-not-appear"},
            },
            "spec": {
                "containers": [
                    {
                        "name": "web",
                        "image": "example.invalid/web:test",
                        "args": [
                            "--database=password=literal-secret",
                            "https://user:literal-secret@example.invalid/",
                            "--token",
                            "second-literal-secret",
                        ],
                        "env": [
                            {"name": "PGPASSWORD", "value": "literal-secret"},
                            {"name": "PUBLIC_MODE", "value": "benchmark"},
                            {
                                "name": "DATABASE_PASSWORD",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": "bluemap-perf-database",
                                        "key": "password",
                                    }
                                },
                            },
                        ],
                    }
                ]
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": "web",
                        "image": "example.invalid/web:test",
                        "imageID": "example.invalid/web@sha256:0123",
                        "ready": True,
                        "restartCount": 2,
                    }
                ]
            },
        }

        result = sanitize_kubernetes_resource.snapshot(
            resource,
            "2026-07-30T00:00:00.000Z",
        )

        pod = result["resource"]
        self.assertNotIn("annotations", pod["metadata"])
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["env"][0]["value"], "<redacted>")
        self.assertEqual(container["env"][1]["value"], "benchmark")
        self.assertEqual(
            container["env"][2]["valueFrom"]["secretKeyRef"]["name"],
            "bluemap-perf-database",
        )
        self.assertNotIn("literal-secret", " ".join(container["args"]))
        self.assertNotIn("second-literal-secret", " ".join(container["args"]))
        self.assertEqual(
            pod["status"]["containerStatuses"][0]["imageID"],
            "example.invalid/web@sha256:0123",
        )
        self.assertEqual(
            pod["status"]["containerStatuses"][0]["restartCount"],
            2,
        )

    def test_refuses_secret_resources(self) -> None:
        with self.assertRaisesRegex(ValueError, "Refusing"):
            sanitize_kubernetes_resource.snapshot(
                {"apiVersion": "v1", "kind": "Secret", "metadata": {}},
                "2026-07-30T00:00:00.000Z",
            )


class ConfigMapSnapshotTests(unittest.TestCase):
    def test_preserves_non_secret_config_and_redacts_credentials(self) -> None:
        result = sanitize_configmap.snapshot(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "bluemap-perf-web", "namespace": "minecraft"},
                "data": {
                    "webserver.conf": (
                        "port = 8100\n"
                        "password = literal-secret\n"
                        "url = postgresql://user:literal-secret@db/bluemap\n"
                    ),
                    "client-secret": "literal-secret",
                },
            },
            "2026-07-30T00:00:00.000Z",
        )

        data = result["resource"]["data"]
        self.assertIn("port = 8100", data["webserver.conf"])
        self.assertNotIn("literal-secret", json.dumps(data))
        self.assertEqual(data["client-secret"], "<redacted>")

    def test_refuses_private_keys_and_binary_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "private-key"):
            sanitize_configmap.snapshot(
                {
                    "kind": "ConfigMap",
                    "metadata": {},
                    "data": {"tls.key": "-----BEGIN PRIVATE KEY-----\nvalue"},
                },
                "2026-07-30T00:00:00.000Z",
            )

        with self.assertRaisesRegex(ValueError, "binaryData"):
            sanitize_configmap.snapshot(
                {
                    "kind": "ConfigMap",
                    "metadata": {},
                    "binaryData": {"blob": "AA=="},
                },
                "2026-07-30T00:00:00.000Z",
            )


class RuntimeIdentityTests(unittest.TestCase):
    def test_resolves_every_pod_container_kind_to_an_immutable_digest(
        self,
    ) -> None:
        pod = {
            "kind": "Pod",
            "spec": {
                "containers": [{"name": "web"}],
                "initContainers": [{"name": "driver"}],
                "ephemeralContainers": [{"name": "debug"}],
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": "web",
                        "imageID": "containerd://sha256:" + "1" * 64,
                    }
                ],
                "initContainerStatuses": [
                    {
                        "name": "driver",
                        "imageID": "example.invalid/driver@sha256:" + "2" * 64,
                    }
                ],
                "ephemeralContainerStatuses": [
                    {
                        "name": "debug",
                        "imageID": "docker-pullable://debug@sha256:" + "3" * 64,
                    }
                ],
            },
        }

        self.assertEqual(
            runtime_identity.pod_images(pod),
            [
                {
                    "kind": "container",
                    "name": "web",
                    "digest": "sha256:" + "1" * 64,
                },
                {
                    "kind": "ephemeralContainer",
                    "name": "debug",
                    "digest": "sha256:" + "3" * 64,
                },
                {
                    "kind": "initContainer",
                    "name": "driver",
                    "digest": "sha256:" + "2" * 64,
                },
            ],
        )

    def test_rejects_missing_status_and_non_immutable_image_ids(self) -> None:
        pod = {
            "kind": "Pod",
            "spec": {"containers": [{"name": "web"}]},
            "status": {"containerStatuses": []},
        }
        with self.assertRaisesRegex(ValueError, "do not exactly match"):
            runtime_identity.pod_images(pod)

        pod["status"]["containerStatuses"] = [
            {"name": "web", "imageID": "example.invalid/web:latest"}
        ]
        with self.assertRaisesRegex(ValueError, "not an immutable"):
            runtime_identity.pod_images(pod)

    def test_sanitized_configuration_identity_is_stable_and_secret_free(
        self,
    ) -> None:
        def configmaps(secret: str, public_value: str, reverse: bool) -> dict:
            items = [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "bluemap-perf-b"},
                    "data": {
                        "storage.conf": (
                            f"password = {secret}\npublic = {public_value}\n"
                        )
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "bluemap-perf-a"},
                    "data": {"web.conf": "port = 8100\n"},
                },
            ]
            return {"kind": "List", "items": list(reversed(items)) if reverse else items}

        first = runtime_identity.config_identity_from_snapshots(
            runtime_identity.sanitize_configmaps(configmaps("one", "same", False))
        )
        second = runtime_identity.config_identity_from_snapshots(
            runtime_identity.sanitize_configmaps(configmaps("two", "same", True))
        )
        changed = runtime_identity.config_identity_from_snapshots(
            runtime_identity.sanitize_configmaps(configmaps("two", "changed", True))
        )

        self.assertEqual(first, second)
        self.assertNotEqual(
            first["sanitizedConfigSha256"],
            changed["sanitizedConfigSha256"],
        )
        self.assertNotIn("one", json.dumps(first))
        self.assertNotIn("two", json.dumps(second))
        self.assertEqual(
            [item["name"] for item in first["configMaps"]],
            ["bluemap-perf-a", "bluemap-perf-b"],
        )

    def test_expected_identity_rejects_placeholders_and_unsorted_images(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "40-character"):
            runtime_identity.validate_git_revision(
                "REPLACE_WITH_40_CHARACTER_BENCHMARK_GIT_REVISION",
                "revision",
            )
        with self.assertRaisesRegex(ValueError, "all-zero"):
            runtime_identity.validate_digest(
                "sha256:" + "0" * 64,
                "image",
                prefix=True,
            )
        with self.assertRaisesRegex(ValueError, "sorted"):
            runtime_identity.validate_expected_images(
                [
                    {
                        "kind": "initContainer",
                        "name": "driver",
                        "digest": "sha256:" + "1" * 64,
                    },
                    {
                        "kind": "container",
                        "name": "web",
                        "digest": "sha256:" + "2" * 64,
                    },
                ]
            )


class PrometheusCaptureTests(unittest.TestCase):
    def test_inspects_cluster_service_url_without_credentials(self) -> None:
        result = capture_prometheus.inspect_url(
            "http://rancher-monitoring-prometheus." "cattle-monitoring-system.svc:9090"
        )

        self.assertEqual(
            result["clusterService"],
            {
                "service": "rancher-monitoring-prometheus",
                "namespace": "cattle-monitoring-system",
                "port": 9090,
                "path": "",
                },
            )

    def test_derives_standard_pod_configmap_references(self) -> None:
        resource = {
            "kind": "Deployment",
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [
                            {"configMap": {"name": "bluemap-perf-direct"}},
                            {
                                "projected": {
                                    "sources": [
                                        {
                                            "configMap": {
                                                "name": "bluemap-perf-projected"
                                            }
                                        },
                                        {"configMap": {"name": "kube-root-ca.crt"}},
                                    ]
                                }
                            },
                        ],
                        "initContainers": [
                            {
                                "envFrom": [
                                    {
                                        "configMapRef": {
                                            "name": "bluemap-perf-init-env"
                                        }
                                    }
                                ]
                            }
                        ],
                        "containers": [
                            {
                                "env": [
                                    {
                                        "valueFrom": {
                                            "configMapKeyRef": {
                                                "name": "bluemap-perf-key"
                                            }
                                        }
                                    }
                                ]
                            }
                        ],
                    }
                }
            },
        }

        self.assertEqual(
            configmap_references.references(resource),
            [
                "bluemap-perf-direct",
                "bluemap-perf-init-env",
                "bluemap-perf-key",
                "bluemap-perf-projected",
            ],
        )

        with self.assertRaisesRegex(ValueError, "must not contain credentials"):
            capture_prometheus.inspect_url("https://benchmark:secret@example.invalid")

    def test_queries_are_anchored_to_exact_selected_pods(self) -> None:
        targets = capture_prometheus.parse_pod_targets(
            [
                "loadgen=bluemap-perf-loadgen",
                "web=bluemap-perf-web.a",
                "database=bluemap-perf-postgres-0",
            ]
        )
        queries = capture_prometheus.build_queries(
            "minecraft",
            targets,
            ["contabo1", "contabo2"],
        )
        cpu_query = next(
            query["query"]
            for query in queries
            if query["name"] == "container_cpu_cores"
        )
        database_query = next(
            query["query"]
            for query in queries
            if query["name"] == "postgres_connections"
        )

        self.assertIn(
            r'pod=~"^(?:bluemap-perf-loadgen|'
            r'bluemap-perf-web\\.a|bluemap-perf-postgres-0)$"',
            cpu_query,
        )
        self.assertIn(
            r'pod=~"^(?:bluemap-perf-postgres-0)$"',
            database_query,
        )
        self.assertNotIn("bluemap-perf-web", database_query)
        node_cpu_query = next(
            query["query"]
            for query in queries
            if query["name"] == "node_non_target_container_cpu_cores"
        )
        node_disk_query = next(
            query["query"]
            for query in queries
            if query["name"] == "node_disk_read_bytes_rate"
        )
        self.assertIn(r'node=~"^(?:contabo1|contabo2)$"', node_cpu_query)
        self.assertIn(r'pod!~"^(?:bluemap-perf-loadgen|', node_cpu_query)
        self.assertIn(r'nodename=~"^(?:contabo1|contabo2)$"', node_disk_query)

    def test_captures_every_query_for_the_exact_requested_range(self) -> None:
        requests: list[dict[str, list[str]]] = []
        paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                paths.append(parsed.path)
                requests.append(parse_qs(parsed.query))
                body = json.dumps(
                    {
                        "status": "success",
                        "data": {"resultType": "matrix", "result": []},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "prometheus.json"
                phases = Path(directory) / "phases.ndjson"
                phases.write_text(
                    '{"timestamp":"2026-07-30T00:00:10Z","repetition":1,'
                    '"phase":"measurement","event":"start"}\n'
                    '{"timestamp":"2026-07-30T00:04:50Z","repetition":1,'
                    '"phase":"measurement","event":"end"}\n',
                    encoding="utf-8",
                )
                capture_prometheus.capture(
                    argparse.Namespace(
                        base_url=f"http://127.0.0.1:{server.server_port}",
                        source_url="http://prometheus.monitoring.svc:9090",
                        start=1_785_369_600,
                        end=1_785_369_900,
                        step=15,
                        namespace="minecraft",
                        pod=[
                            "loadgen=bluemap-perf-loadgen",
                            "web=bluemap-perf-web-abc",
                            "database=bluemap-perf-postgres-0",
                        ],
                        node=["contabo1"],
                        phase_events=phases,
                        max_non_target_node_cpu_range_cores=0.5,
                        max_non_target_node_cpu_mean_cores=2.0,
                        max_non_target_node_cpu_maximum_cores=3.0,
                        output=output,
                        timeout=5,
                    )
                )
                bundle = json.loads(output.read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        self.assertGreaterEqual(len(requests), 20)
        self.assertEqual(paths, ["/api/v1/query_range"] * len(requests))
        for request in requests:
            self.assertEqual(request["start"], ["1785369600"])
            self.assertEqual(request["end"], ["1785369900"])
            self.assertEqual(request["step"], ["15"])
        self.assertEqual(bundle["range"]["start"], 1_785_369_600)
        self.assertEqual(bundle["range"]["end"], 1_785_369_900)
        self.assertEqual(
            bundle["prometheus"]["baseUrl"],
            "http://prometheus.monitoring.svc:9090",
        )
        self.assertEqual(bundle["nodes"], ["contabo1"])
        self.assertFalse(bundle["nodeNoise"]["passed"])

    def test_node_noise_is_assessed_per_measurement_repetition(self) -> None:
        query_results = [
            {
                "name": "node_non_target_container_cpu_cores",
                "response": {
                    "data": {
                        "result": [
                            {
                                "metric": {"node": "contabo1"},
                                "values": [
                                    [10, "0.20"],
                                    [20, "0.25"],
                                    [110, "0.20"],
                                    [120, "1.10"],
                                    [210, "2.50"],
                                    [220, "2.60"],
                                ],
                            }
                        ]
                    }
                },
            }
        ]
        assessment = capture_prometheus.assess_node_noise(
            query_results,
            ["contabo1"],
            [
                {"repetition": 1, "start": 1, "end": 30},
                {"repetition": 2, "start": 100, "end": 130},
                {"repetition": 3, "start": 200, "end": 230},
            ],
            0.5,
            2.0,
            3.0,
        )

        self.assertEqual(assessment["noisyRepetitions"], [2, 3])
        self.assertFalse(assessment["passed"])


class ProtectedPvcManifestTests(unittest.TestCase):
    def test_snapshot_copy_is_the_only_protected_pvc_consumer(self) -> None:
        snapshot_path = KUBERNETES_DIR / "snapshot-copy.yaml"
        snapshot_documents = list(
            yaml.safe_load_all(snapshot_path.read_text(encoding="utf-8"))
        )
        protected_claims = [
            claim
            for document in snapshot_documents
            for claim in persistent_volume_claim_references(document)
            if claim.get("claimName") == "minecraft-data"
        ]
        self.assertEqual(
            protected_claims,
            [{"claimName": "minecraft-data", "readOnly": True}],
        )

        for path in sorted(KUBERNETES_DIR.glob("*.yaml")):
            if path == snapshot_path:
                continue
            with self.subTest(path=path.name):
                documents = yaml.safe_load_all(path.read_text(encoding="utf-8"))
                claims = [
                    claim
                    for document in documents
                    for claim in persistent_volume_claim_references(document)
                    if claim.get("claimName") == "minecraft-data"
                ]
                self.assertEqual(claims, [])

    def test_snapshot_source_is_read_only_without_pod_wide_ownership_changes(
        self,
    ) -> None:
        documents = list(
            yaml.safe_load_all(
                (KUBERNETES_DIR / "snapshot-copy.yaml").read_text(
                    encoding="utf-8"
                )
            )
        )
        job = next(
            document
            for document in documents
            if document.get("kind") == "Job"
            and document["metadata"]["name"] == "bluemap-perf-snapshot-copy"
        )
        pod_spec = job["spec"]["template"]["spec"]

        self.assertIs(pod_spec["automountServiceAccountToken"], False)
        self.assertNotIn("fsGroup", pod_spec.get("securityContext", {}))

        volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
        self.assertEqual(
            volumes["source"]["persistentVolumeClaim"],
            {"claimName": "minecraft-data", "readOnly": True},
        )
        self.assertEqual(
            volumes["destination"]["persistentVolumeClaim"],
            {"claimName": "bluemap-perf-snapshot"},
        )

        self.assertEqual(len(pod_spec.get("initContainers", [])), 0)
        self.assertEqual(len(pod_spec["containers"]), 1)
        container = pod_spec["containers"][0]
        self.assertEqual(container["name"], "copy")
        self.assertEqual(
            container["securityContext"],
            {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "readOnlyRootFilesystem": True,
                "runAsNonRoot": False,
                "runAsUser": 0,
                "runAsGroup": 0,
            },
        )

        mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        self.assertEqual(
            mounts["source"],
            {"name": "source", "mountPath": "/source", "readOnly": True},
        )
        self.assertEqual(
            mounts["destination"],
            {"name": "destination", "mountPath": "/snapshot"},
        )

    def test_tls_secret_preparation_is_bound_to_benchmark_names_and_kubeconfig(
        self,
    ) -> None:
        script = KUBERNETES_DIR / "prepare-postgres-tls.sh"
        source = script.read_text(encoding="utf-8")

        self.assertIn("BLUEMAP_BENCHMARK_KUBECONFIG", source)
        self.assertIn(
            'kubectl_command=(kubectl --kubeconfig "$kubeconfig_path")',
            source,
        )
        result = subprocess.run(
            ["bash", str(script)],
            env={
                "PATH": "/usr/bin:/bin",
                "SECRET_NAME": "minecraft-data",
                "BLUEMAP_BENCHMARK_KUBECONFIG": "/does/not/exist",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("beginning with bluemap-perf-", result.stderr)


class OriginRunnerStaticTests(unittest.TestCase):
    @staticmethod
    def resolved_matrix() -> dict:
        matrix = generate_schedule.load_json(BENCHMARK_ROOT / "matrix.example.json")
        matrix["benchmarkGitRevision"] = "1" * 40
        matrix["manifestSha256"] = "2" * 64
        for variant_index, variant in enumerate(matrix["variants"], start=1):
            for image_index, image in enumerate(variant["expectedImages"], start=1):
                digit = format((variant_index + image_index) % 15 + 1, "x")
                image["digest"] = "sha256:" + digit * 64
            variant["expectedSanitizedConfigSha256"] = (
                format(variant_index + 8, "x") * 64
            )
        return matrix

    def test_help_does_not_require_cluster_access(self) -> None:
        result = subprocess.run(
            ["bash", str(TOOLS_DIR / "run_origin_case.sh"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("bluemap-perf-loadgen", result.stdout)
        self.assertIn("--database-pod", result.stdout)
        self.assertIn("--prometheus-url", result.stdout)
        self.assertIn("--python", result.stdout)
        self.assertIn("--map-id", result.stdout)
        self.assertIn("--configmap", result.stdout)
        self.assertIn("--min-achieved-rate-ratio", result.stdout)
        self.assertIn("--trace-seed", result.stdout)
        self.assertIn("--latency-p95-ms", result.stdout)
        self.assertIn("--max-non-target-node-cpu-range-cores", result.stdout)
        self.assertIn("--schedule-entry", result.stdout)
        self.assertIn("--variant-id", result.stdout)
        self.assertIn("--implementation", result.stdout)
        self.assertIn("--storage-type", result.stdout)
        self.assertIn("--database-backend", result.stdout)
        runner = (TOOLS_DIR / "run_origin_case.sh").read_text(encoding="utf-8")
        self.assertIn("configmap_references.py", runner)
        self.assertIn("check_arrival_gate.py", runner)
        self.assertIn("DESIRED_WEB_REPLICA_COUNT", runner)
        self.assertIn("verify_formal_runtime_identity", runner)
        self.assertIn("runtime_identity.py", runner)
        self.assertNotIn("printf 'unknown'", runner)

    def test_documented_optimized_java_commands_target_the_optimized_release(
        self,
    ) -> None:
        readme = (BENCHMARK_ROOT / "README.md").read_text(encoding="utf-8")
        identity = readme.split(
            "First deploy each frozen candidate", maxsplit=1
        )[1].split("Repeat this for every variant", maxsplit=1)[0]
        example = readme.split(
            "Use a unique case ID and current, exact Pod names:", maxsplit=1
        )[1].split(
            "Repeat `--web-deployment`", maxsplit=1
        )[0]

        for section in (identity, example):
            self.assertIn("bluemap-perf-java-new-postgresql", section)
            self.assertNotIn("bluemap-perf-java-POD-SUFFIX", section)
            self.assertNotIn("bluemap-perf-java-config", section)
            self.assertNotIn("bluemap-perf-java-storage", section)

    def test_formal_schedule_is_seeded_balanced_and_tamper_evident(self) -> None:
        matrix_path = BENCHMARK_ROOT / "matrix.example.json"
        unresolved = generate_schedule.load_json(matrix_path)
        digest = generate_schedule.matrix_sha256(matrix_path)
        with self.assertRaisesRegex(ValueError, "benchmarkGitRevision"):
            generate_schedule.build_schedule(unresolved, digest)

        matrix = self.resolved_matrix()
        first = generate_schedule.build_schedule(matrix, digest)
        second = generate_schedule.build_schedule(matrix, digest)

        self.assertEqual(first, second)
        self.assertEqual(first["formatVersion"], 2)
        self.assertEqual(
            first["benchmarkGitRevision"],
            matrix["benchmarkGitRevision"],
        )
        generate_schedule.validate_schedule(matrix, digest, first)
        counts = {}
        for entry in first["entries"]:
            key = (
                entry["block"],
                entry["matrixCaseId"],
                entry["variantId"],
            )
            counts[key] = counts.get(key, 0) + 1
            variant = next(
                item
                for item in matrix["variants"]
                if item["id"] == entry["variantId"]
            )
            self.assertEqual(entry["implementation"], variant["implementation"])
            self.assertEqual(entry["storageType"], variant["storageType"])
            self.assertEqual(
                entry["databaseBackend"],
                variant["databaseBackend"],
            )
            self.assertEqual(entry["replicaCount"], variant["replicaCount"])
            self.assertEqual(
                entry["benchmarkGitRevision"],
                matrix["benchmarkGitRevision"],
            )
            self.assertEqual(entry["expectedImages"], variant["expectedImages"])
            self.assertEqual(
                entry["expectedSanitizedConfigSha256"],
                variant["expectedSanitizedConfigSha256"],
            )
        self.assertTrue(all(count == 1 for count in counts.values()))

        for case in matrix["cases"]:
            for variant_id in case["variants"]:
                positions = Counter(
                    entry["ordinalWithinCase"]
                    for entry in first["entries"]
                    if entry["matrixCaseId"] == case["id"]
                    and entry["variantId"] == variant_id
                )
                counts_by_position = [
                    positions.get(position, 0)
                    for position in range(1, len(case["variants"]) + 1)
                ]
                self.assertLessEqual(
                    max(counts_by_position) - min(counts_by_position),
                    1,
                )

        tampered = json.loads(json.dumps(first))
        tampered["entries"][0]["variantId"] = "tampered"
        with self.assertRaisesRegex(ValueError, "does not exactly match"):
            generate_schedule.validate_schedule(matrix, digest, tampered)

    def test_formal_identity_gate_precedes_correctness_and_load(self) -> None:
        runner = (TOOLS_DIR / "run_origin_case.sh").read_text(encoding="utf-8")
        identity_call = runner.index(
            "capture_snapshot_set before\n"
            "verify_formal_runtime_identity ||"
        )
        case_start = runner.index('CASE_START_EPOCH="$(date -u +%s)"')
        correctness = runner.index(
            'set_phase "$repetition" "correctness"',
        )

        self.assertLess(identity_call, case_start)
        self.assertLess(identity_call, correctness)
        self.assertIn("Formal runs require a clean tracked Git worktree", runner)
        self.assertIn(
            "Benchmark Git revision does not match the formal schedule",
            runner,
        )
        self.assertIn("expectedImages", runner)
        self.assertIn("expectedSanitizedConfigSha256", runner)

    def test_identity_schemas_are_versioned_and_reject_placeholders(self) -> None:
        matrix_schema = json.loads(
            (BENCHMARK_ROOT / "matrix.schema.json").read_text(encoding="utf-8")
        )
        schedule_schema = json.loads(
            (BENCHMARK_ROOT / "schedule.schema.json").read_text(encoding="utf-8")
        )

        self.assertEqual(matrix_schema["properties"]["formatVersion"]["const"], 2)
        self.assertEqual(schedule_schema["properties"]["formatVersion"]["const"], 2)
        self.assertIn(
            "benchmarkGitRevision",
            matrix_schema["required"],
        )
        self.assertIn(
            "expectedImages",
            matrix_schema["$defs"]["variant"]["required"],
        )
        self.assertNotRegex(
            "REPLACE_WITH_40_CHARACTER_BENCHMARK_GIT_REVISION",
            matrix_schema["$defs"]["gitRevision"]["pattern"],
        )

    def test_k6_workload_has_formal_arrival_and_exact_status_gates(self) -> None:
        script = (BENCHMARK_ROOT / "k6" / "bluemap.js").read_text(encoding="utf-8")

        self.assertIn('dropped_iterations: ["count==0"]', script)
        self.assertIn("MIN_ACHIEVED_RATE_RATIO", script)
        self.assertIn('case "missing-tile":', script)
        self.assertIn('request(manifest.missingTile, "tile-missing", [204])', script)
        self.assertIn("conditional-seed", script)
        self.assertIn('recordStatus(response, [304])', script)
        self.assertIn("playerPolling", script)
        self.assertIn("markerPolling", script)
        self.assertIn("exec.scenario.iterationInTest", script)
        self.assertIn("TRACE_SEED", script)
        self.assertIn("http_req_duration", script)
        self.assertIn('"iterations{scenario:workload}"', script)
        self.assertIn('"iterations{scenario:playerPolling}"', script)
        self.assertIn('"iterations{scenario:markerPolling}"', script)
        self.assertIn("minimumIterationCountThreshold", script)
        self.assertNotRegex(script, r"iterations\s*:\s*\[\s*`rate>=")
        self.assertNotIn("Math.random()", script)
        self.assertNotIn("data_received{traffic:workload}", script)
        self.assertNotIn("data_sent{traffic:workload}", script)

    def test_http_contract_rejects_each_alternate_representation(self) -> None:
        contract = (
            TOOLS_DIR / "check_http_contract.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'for offered_encoding in ("gzip", "deflate", "zstd", '
            '"identity", "lz4")',
            contract,
        )
        self.assertIn(
            'problem.get("code") == "bluemap_required_content_encoding"',
            contract,
        )
        self.assertIn(
            'get_content_type() == "application/problem+json"',
            contract,
        )
        self.assertIn(
            'head.headers.get("Last-Modified") == last_modified',
            contract,
        )
        self.assertIn(
            'not_modified.headers.get("Last-Modified") == last_modified',
            contract,
        )
        self.assertIn(
            'str(etag)[2:] if str(etag).startswith("W/") else f"W/{etag}"',
            contract,
        )

    def test_calibrated_matrix_and_load_defaults_match(self) -> None:
        matrix = generate_schedule.load_json(
            BENCHMARK_ROOT / "matrix.example.json"
        )
        self.assertEqual(matrix["controls"]["preAllocatedVUs"], 256)
        self.assertEqual(matrix["controls"]["maxVUs"], 512)

        cases = {case["id"]: case for case in matrix["cases"]}
        self.assertEqual(
            set(cases),
            {
                "map-mixed-r15",
                "map-mixed-horizontal-r40",
                "live-viewers-r15",
                "large-object-r1",
            },
        )
        self.assertEqual(
            (
                cases["map-mixed-r15"]["rate"],
                cases["map-mixed-r15"]["latencyP95Milliseconds"],
                cases["map-mixed-r15"]["latencyP99Milliseconds"],
            ),
            (15, 10000, 20000),
        )
        self.assertEqual(
            (
                cases["map-mixed-horizontal-r40"]["rate"],
                cases["map-mixed-horizontal-r40"][
                    "latencyP95Milliseconds"
                ],
                cases["map-mixed-horizontal-r40"][
                    "latencyP99Milliseconds"
                ],
            ),
            (40, 5000, 10000),
        )
        self.assertEqual(cases["live-viewers-r15"]["viewers"], 15)
        self.assertEqual(cases["large-object-r1"]["rate"], 1)

        runner = (TOOLS_DIR / "run_origin_case.sh").read_text(encoding="utf-8")
        script = (BENCHMARK_ROOT / "k6" / "bluemap.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('PRE_ALLOCATED_VUS="256"', runner)
        self.assertIn('__ENV.PRE_ALLOCATED_VUS || "256"', script)
        self.assertIn('MAX_NON_TARGET_NODE_CPU_RANGE_CORES="0.5"', runner)
        self.assertIn('MAX_NON_TARGET_NODE_CPU_MEAN_CORES="3.0"', runner)
        self.assertIn('MAX_NON_TARGET_NODE_CPU_MAXIMUM_CORES="4.0"', runner)

    def test_file_cases_do_not_require_a_database_target(self) -> None:
        runner = (TOOLS_DIR / "run_origin_case.sh").read_text(encoding="utf-8")

        self.assertNotIn("At least one --database-pod is required", runner)
        self.assertIn('json_array "${DATABASE_PODS[@]}"', runner)
        self.assertIn("sample_service_endpoints", runner)
        self.assertIn('if [[ "$phase" == */measurement ]]', runner)
        self.assertIn('$phase-end"', runner)
        self.assertIn(
            'sh "$REMOTE_ROOT/repetitions/$repetition_name"',
            runner,
        )
        self.assertIn('[[ -f "$log_file" ]]', runner)
        self.assertIn(
            'loadgen_copy_to "$local_file" "$remote_file"',
            runner,
        )
        self.assertIn('[[ "$actual_sha256" == "$expected_sha256" ]]', runner)

    def test_runner_validates_load_generator_before_every_exec_or_copy(
        self,
    ) -> None:
        runner = (TOOLS_DIR / "run_origin_case.sh").read_text(encoding="utf-8")
        validation = runner.split(
            "validate_load_generator_pod() {", maxsplit=1
        )[1].split("\n}", maxsplit=1)[0]
        exec_wrapper = runner.split("loadgen_exec() {", maxsplit=1)[1].split(
            "\n}", maxsplit=1
        )[0]
        copy_wrapper = runner.split(
            "loadgen_copy_to() {", maxsplit=1
        )[1].split("\n}", maxsplit=1)[0]

        for required in (
            ".metadata.ownerReferences // []",
            '"app.kubernetes.io/part-of"',
            '"bluemap.guenter.cloud/experiment-id"',
            ".spec.automountServiceAccountToken == false",
            ".spec.initContainers // []",
            ".spec.ephemeralContainers // []",
            ".spec.containers | length == 1",
            ".spec.volumes | length == 3",
            '.name == "benchmark"',
            '.name == "artifacts"',
            '.name == "tmp"',
            '.mountPath == "/artifacts"',
            '.emptyDir | type == "object"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, validation)

        self.assertLess(
            exec_wrapper.index("validate_load_generator_pod"),
            exec_wrapper.index('kube exec "pod/$LOADGEN_POD"'),
        )
        self.assertLess(
            copy_wrapper.index("validate_load_generator_pod"),
            copy_wrapper.index('kube cp "$local_file"'),
        )
        self.assertEqual(
            runner.count('kube exec "pod/$LOADGEN_POD"'),
            1,
        )
        self.assertEqual(runner.count('kube cp "$local_file"'), 1)

    def test_runner_closes_every_prometheus_jq_conditional(self) -> None:
        runner = (TOOLS_DIR / "run_origin_case.sh").read_text(encoding="utf-8")
        observability = runner.split("observability: {", maxsplit=1)[1].split(
            "formalSchedule:", maxsplit=1
        )[0]

        self.assertEqual(
            observability.count("if $prometheusEnabled"),
            observability.count("end"),
        )

    def test_imports_use_disposable_snapshot_and_live_fixtures(self) -> None:
        imports = (
            BENCHMARK_ROOT / "kubernetes" / "import-jobs.yaml"
        ).read_text(encoding="utf-8")
        snapshot = (
            BENCHMARK_ROOT / "kubernetes" / "snapshot-copy.yaml"
        ).read_text(encoding="utf-8")
        databases = (
            BENCHMARK_ROOT / "kubernetes" / "databases.yaml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("claimName: minecraft-data", imports)
        self.assertIn("claimName: bluemap-perf-snapshot", imports)
        self.assertIn("--players-fixture /fixtures/players.json", imports)
        self.assertIn("claimName: minecraft-data", snapshot)
        self.assertIn("readOnly: true", snapshot)
        self.assertIn("claimName: bluemap-perf-snapshot", snapshot)
        self.assertIn("storageClassName: longhorn-static", snapshot)
        self.assertIn("kind: User", databases)
        self.assertIn("name: bluemap_mtls", databases)
        self.assertIn("name: bluemap-perf-mariadb-mtls", databases)
        self.assertIn("x509: true", databases)
        self.assertIn("kind: Grant", databases)
        self.assertIn("- SELECT", databases)
        self.assertNotIn("kind: Secret", databases)

        file_values = (
            BENCHMARK_ROOT
            / "kubernetes"
            / "file-live-fixtures-values.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("root: /snapshot/bluemap/web/maps", file_values)
        self.assertIn("claimName: bluemap-perf-snapshot", file_values)
        self.assertIn(
            "/snapshot/bluemap/web/maps/world/live/players.json",
            file_values,
        )
        self.assertIn(
            "/snapshot/bluemap/web/maps/world/live/markers.json",
            file_values,
        )
        self.assertGreaterEqual(file_values.count("readOnly: true"), 3)
        self.assertNotIn("/data/web/maps", file_values)
        self.assertNotIn("claimName: minecraft-data", file_values)

    def test_documented_slow_reader_uses_guarded_verified_expectations(
        self,
    ) -> None:
        readme = (BENCHMARK_ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## Graceful-drain slow-reader check", maxsplit=1)[1]

        self.assertIn("run_guarded_slow_reader.py", section)
        self.assertIn("--confirm-delete-pod \"$WEB_POD\"", section)
        self.assertIn("expected response headers/hash/length", section)


class SlowReaderTests(unittest.TestCase):
    def test_reads_and_hashes_the_complete_transferred_representation(self) -> None:
        body = b"BlueMap large response" * 4096

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Encoding", "zstd")
                self.send_header("ETag", '"test"')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = slow_reader.execute(
                    argparse.Namespace(
                        url=f"http://127.0.0.1:{server.server_port}/large",
                        bytes_per_second=100_000_000,
                        chunk_size=4096,
                        initial_delay_seconds=0,
                        timeout_seconds=5,
                        expected_status=200,
                        expected_sha256=hashlib.sha256(body).hexdigest(),
                        expected_length=len(body),
                        accept_encoding="zstd",
                        user_agent="BlueMap-Slow-Reader/test",
                        ready_file=root / "ready.json",
                        output=root / "result.json",
                    )
                )
                ready = json.loads((root / "ready.json").read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        self.assertTrue(result["complete"])
        self.assertEqual(result["bytesRead"], len(body))
        self.assertEqual(ready["contentLength"], len(body))

    def test_requires_an_independent_length_or_hash_expectation(self) -> None:
        with self.assertRaisesRegex(ValueError, "is required"):
            slow_reader.execute(
                argparse.Namespace(
                    url="http://127.0.0.1:1/large",
                    bytes_per_second=1024,
                    chunk_size=1024,
                    initial_delay_seconds=0,
                    timeout_seconds=1,
                    expected_status=200,
                    expected_sha256=None,
                    expected_length=None,
                    accept_encoding="zstd",
                    user_agent="BlueMap-Slow-Reader/test",
                    ready_file=Path("/tmp/unused-ready.json"),
                    output=Path("/tmp/unused-output.json"),
                )
            )


class ArrivalGateTests(unittest.TestCase):
    @staticmethod
    def summary(
        scenarios: dict[str, int],
        *,
        wall_clock_rate: float,
        dropped: int = 0,
    ) -> dict[str, object]:
        total = sum(scenarios.values())
        metrics: dict[str, object] = {
            "iterations": {
                "count": total,
                "rate": wall_clock_rate,
            },
            "dropped_iterations": {"count": dropped},
        }
        for scenario, count in scenarios.items():
            metrics[f"iterations{{scenario:{scenario}}}"] = {
                "count": count
            }
        return {"metrics": metrics}

    def test_slow_final_request_does_not_reduce_achieved_rate_gate(self) -> None:
        self.assertEqual(check_arrival_gate.duration_seconds("5m"), 300)
        result = check_arrival_gate.evaluate(
            self.summary({"workload": 301}, wall_clock_rate=9.74),
            profile="map-data-mixed",
            rate=10,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=True,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["scenarios"][0]["completedIterations"], 301)
        self.assertAlmostEqual(
            result["totals"][
                "achievedIterationsPerSecondOverConfiguredDuration"
            ],
            301 / 30,
        )
        self.assertEqual(
            result["totals"]["k6WallClockIterationsPerSecond"],
            9.74,
        )

    def test_incomplete_scheduled_count_still_fails(self) -> None:
        result = check_arrival_gate.evaluate(
            self.summary({"workload": 296}, wall_clock_rate=10),
            profile="map-data-mixed",
            rate=10,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=False,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["scenarios"][0]["passed"])

    def test_dropped_or_unattributed_iterations_still_fail(self) -> None:
        dropped = check_arrival_gate.evaluate(
            self.summary(
                {"workload": 301},
                wall_clock_rate=9.74,
                dropped=1,
            ),
            profile="map-data-mixed",
            rate=10,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=False,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )
        mismatched_summary = self.summary(
            {"workload": 301},
            wall_clock_rate=9.74,
        )
        mismatched_summary["metrics"]["iterations"]["count"] = 302
        mismatched = check_arrival_gate.evaluate(
            mismatched_summary,
            profile="map-data-mixed",
            rate=10,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=False,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )

        self.assertFalse(dropped["passed"])
        self.assertEqual(dropped["droppedIterations"], 1)
        self.assertFalse(mismatched["passed"])
        self.assertFalse(mismatched["scenarioCountsEqualOverall"])

    def test_live_viewer_scenarios_are_gated_independently(self) -> None:
        failed = check_arrival_gate.evaluate(
            self.summary(
                {
                    "playerPolling": 3001,
                    "markerPolling": 296,
                },
                wall_clock_rate=100,
            ),
            profile="live-viewers",
            rate=1,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=True,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )
        passed = check_arrival_gate.evaluate(
            self.summary(
                {
                    "playerPolling": 3001,
                    "markerPolling": 301,
                },
                wall_clock_rate=100,
            ),
            profile="live-viewers",
            rate=1,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=True,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )

        self.assertFalse(failed["passed"])
        self.assertFalse(failed["scenarios"][1]["passed"])
        self.assertTrue(passed["passed"])
        self.assertEqual(
            [scenario["scenario"] for scenario in passed["scenarios"]],
            ["playerPolling", "markerPolling"],
        )
        self.assertEqual(passed["scenarioCompletedIterations"], 3302)
        self.assertTrue(passed["scenarioCountsEqualOverall"])

    def test_accepts_nested_values_from_older_summary_shape(self) -> None:
        summary = self.summary({"workload": 301}, wall_clock_rate=9.74)
        summary["metrics"] = {
            name: {"values": values}
            for name, values in summary["metrics"].items()
        }

        result = check_arrival_gate.evaluate(
            summary,
            profile="map-data-mixed",
            rate=10,
            viewers=100,
            marker_interval_seconds=10,
            markers_present=False,
            duration="30s",
            minimum_achieved_ratio=0.99,
        )

        self.assertTrue(result["passed"])


class DeliveryCacheProbeTests(unittest.TestCase):
    def test_records_cold_warm_and_revalidated_delivery(self) -> None:
        request_counts: dict[str, int] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                request_counts[path] = request_counts.get(path, 0) + 1
                etag = f'"{hashlib.sha256(path.encode()).hexdigest()}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    body = b""
                else:
                    self.send_response(200)
                    body = path.encode()
                self.send_header("ETag", etag)
                self.send_header(
                    "Cache-Control",
                    (
                        "private,no-store,no-transform"
                        if path.endswith("players.json")
                        else "public,max-age=60,must-revalidate,no-transform"
                    ),
                )
                self.send_header(
                    "CF-Cache-Status",
                    (
                        "DYNAMIC"
                        if path.endswith("players.json")
                        else "MISS" if request_counts[path] == 1 else "HIT"
                    ),
                )
                if request_counts[path] > 1 and not path.endswith("players.json"):
                    self.send_header("Age", "1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = root / "manifest.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "hotTile": "/maps/world/tiles/0/x0z0.prbm",
                            "settings": ["/maps/world/settings.json"],
                            "markers": [],
                            "players": ["/maps/world/live/players.json"],
                        }
                    ),
                    encoding="utf-8",
                )
                result = probe_delivery_cache.execute(
                    argparse.Namespace(
                        base_url=f"http://127.0.0.1:{server.server_port}",
                        manifest=manifest,
                        probe_id="unit-test",
                        accept_encoding="zstd",
                        user_agent="BlueMap-Cache-Probe/test",
                        require_cloudflare_cache=True,
                        timeout=5,
                    )
                )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["targets"][0]["cold"]["cfCacheStatus"], "MISS")
        self.assertEqual(result["targets"][0]["warm"]["cfCacheStatus"], "HIT")
        self.assertEqual(result["targets"][0]["revalidated"]["status"], 304)

    def test_rejects_any_cached_player_response(self) -> None:
        def response(
            *,
            status: int = 200,
            body_sha: str = "body",
            transferred_bytes: int = 4,
            cache_status: str | None = "DYNAMIC",
            age: str | None = None,
            cache_control: str = "public,max-age=60",
        ) -> dict[str, object]:
            return {
                "status": status,
                "durationMilliseconds": 1.0,
                "transferredBytes": transferred_bytes,
                "transferredSha256": body_sha,
                "age": age,
                "cfCacheStatus": cache_status,
                "etag": '"stable"',
                "lastModified": None,
                "cacheControl": cache_control,
                "contentEncoding": "zstd",
                "contentLength": str(transferred_bytes),
            }

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "hotTile": "/maps/world/tiles/0/x0z0.prbm",
                        "settings": [],
                        "markers": [],
                        "players": ["/maps/world/live/players.json"],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                probe_delivery_cache,
                "request",
                side_effect=[
                    response(),
                    response(),
                    response(status=304, body_sha="empty", transferred_bytes=0),
                    response(cache_control="private,no-store"),
                    response(
                        cache_status="HIT",
                        cache_control="private,no-store",
                    ),
                    response(
                        status=304,
                        body_sha="empty",
                        transferred_bytes=0,
                        age="0",
                        cache_control="private,no-store",
                    ),
                ],
            ):
                result = probe_delivery_cache.execute(
                    argparse.Namespace(
                        base_url="http://example.invalid",
                        manifest=manifest,
                        probe_id="unit-test",
                        accept_encoding="zstd",
                        user_agent="BlueMap-Cache-Probe/test",
                        require_cloudflare_cache=False,
                        timeout=5,
                    )
                )

        self.assertFalse(result["passed"])
        self.assertIn(
            "players: CF-Cache-Status was HIT for warm",
            result["errors"],
        )
        self.assertIn(
            "players: Age was present for revalidated",
            result["errors"],
        )


if __name__ == "__main__":
    unittest.main()
