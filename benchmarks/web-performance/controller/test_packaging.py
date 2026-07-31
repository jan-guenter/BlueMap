#!/usr/bin/env python3
"""Static safety tests for the durable formal benchmark controller package.

These tests deliberately inspect the rendered, reviewable Kubernetes manifest
instead of talking to a cluster.  They are intended to catch an accidental
expansion of the controller's authority or a regression to workstation/pod
based traffic generation before anything is applied.
"""

from __future__ import annotations

import re
import unittest
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


CONTROLLER_DIR = Path(__file__).resolve().parent
WEB_PERFORMANCE_DIR = CONTROLLER_DIR.parent
KUBERNETES_DIR = WEB_PERFORMANCE_DIR / "kubernetes"

DOCKERFILE = CONTROLLER_DIR / "Dockerfile"
ENTRYPOINT = CONTROLLER_DIR / "entrypoint.sh"
KUBECONFIG = CONTROLLER_DIR / "kubeconfig.yaml"
PUBLIC_INGRESS = KUBERNETES_DIR / "public-ingress.yaml"
FORMAL_CONTROLLER = KUBERNETES_DIR / "formal-controller.yaml"
PREPARE_BUNDLE = CONTROLLER_DIR / "prepare_frozen_bundle.py"
FORMAL_CONTROLLER_WORKFLOW = (
    WEB_PERFORMANCE_DIR.parents[1]
    / ".github"
    / "workflows"
    / "formal-controller.yml"
)

NAMESPACE = "minecraft"
CONTROLLER_NAME = "bluemap-perf-formal-controller"
PUBLIC_SERVICE = "bluemap-perf-public"
PUBLIC_HOST = "bluemap-test.guenter.cloud"
CONTROLLER_UID = 12345

CANDIDATE_DEPLOYMENTS = frozenset(
    {
        "bluemap-perf-java",
        "bluemap-perf-java-new-postgresql",
        "bluemap-perf-java-new-postgresql-r3",
        "bluemap-perf-java-php",
        "bluemap-perf-rust-postgresql",
        "bluemap-perf-rust-postgresql-r3",
    }
)
CANDIDATE_SERVICES = CANDIDATE_DEPLOYMENTS
FORMAL_SERVICES = CANDIDATE_SERVICES | {PUBLIC_SERVICE}
FORMAL_INGRESSES = frozenset({PUBLIC_SERVICE})
FORMAL_CONFIGMAPS = frozenset(
    {
        "bluemap-perf-java-config",
        "bluemap-perf-java-storage",
        "bluemap-perf-java-new-postgresql-config",
        "bluemap-perf-java-new-postgresql-storage",
        "bluemap-perf-java-new-postgresql-r3-config",
        "bluemap-perf-java-new-postgresql-r3-storage",
        "bluemap-perf-java-php-fpm",
        "bluemap-perf-java-php-nginx",
        "bluemap-perf-rust-postgresql-r3-rust",
        "bluemap-perf-rust-postgresql-rust",
    }
)

PROTECTED_REFERENCES = (
    "deployment/minecraft",
    "persistentvolumeclaim/minecraft-data",
    "pvc/minecraft-data",
    "pod/minecraft-maintenance-holder",
)

IMMUTABLE_IMAGE = re.compile(r"^[^\s@:]+(?:[/:][^\s@]+)*@sha256:[0-9a-f]{64}$")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_documents(path: Path) -> list[dict[str, Any]]:
    documents = list(yaml.safe_load_all(read_text(path)))
    return [document for document in documents if isinstance(document, dict)]


def one_resource(
    documents: list[dict[str, Any]], kind: str, name: str
) -> dict[str, Any]:
    matches = [
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {kind}/{name}, found {len(matches)}"
        )
    return matches[0]


def pod_spec(job: dict[str, Any]) -> dict[str, Any]:
    return job["spec"]["template"]["spec"]


def volume_by_name(spec: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [volume for volume in spec.get("volumes", []) if volume.get("name") == name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one Pod volume named {name!r}")
    return matches[0]


def mount_by_path(container: dict[str, Any], path: str) -> dict[str, Any]:
    matches = [
        mount
        for mount in container.get("volumeMounts", [])
        if mount.get("mountPath") == path
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one volumeMount at {path!r} in "
            f"container {container.get('name')!r}"
        )
    return matches[0]


class ControllerSourceTests(unittest.TestCase):
    def test_controller_uses_only_runpod_ssh_for_load_generation(self) -> None:
        entrypoint = read_text(ENTRYPOINT)
        required = (
            "--load-generator-backend runpod-ssh",
            "--load-generator-identity",
            "--load-generator-identity-key",
            "--traffic-base-url",
            "--formal-run-id",
            "--require-edge-bypass",
        )
        for fragment in required:
            with self.subTest(fragment=fragment):
                self.assertIn(
                    fragment,
                    entrypoint,
                    f"entrypoint must pass the frozen RunPod option {fragment!r}",
                )

        inspected = "\n".join(
            (
                read_text(DOCKERFILE),
                entrypoint,
                read_text(KUBECONFIG),
            )
        )
        forbidden = (
            "bluemap-perf-loadgen",
            "--load-generator-backend pod",
            "--load-generator-backend local",
            "kubectl exec",
            "kubectl port-forward",
            "k6 run",
        )
        for fragment in forbidden:
            with self.subTest(fragment=fragment):
                self.assertNotIn(
                    fragment,
                    inspected,
                    f"controller packaging must not generate traffic locally or "
                    f"inside the Kubernetes cluster ({fragment!r})",
                )

    def test_termination_cannot_launch_or_resume_a_formal_run(self) -> None:
        entrypoint = read_text(ENTRYPOINT)
        guard = '[[ "$termination_requested" == false ]] || exit 143'
        launch = 'run_child "${orchestrator_command[@]}"'
        self.assertIn(guard, entrypoint)
        self.assertLess(entrypoint.index(guard), entrypoint.index(launch))
        self.assertIn(
            '[[ "$termination_requested" == false ]] || return 143',
            entrypoint,
        )
        self.assertIn(
            "formal run root already exists; automatic resume is forbidden",
            entrypoint,
        )

    def test_bundle_publication_requires_an_explicit_fresh_source(self) -> None:
        helper = read_text(PREPARE_BUNDLE)
        self.assertIn('"--source-root"', helper)
        self.assertIn("dedicated fresh child directory", helper)
        self.assertNotIn(
            'SOURCE_INPUTS = SOURCE_ROOT / "formal-inputs"',
            helper,
            "publication must not default to the preserved historical bundle",
        )

    def test_publish_workflow_requires_exact_bundle_only_child_commit(self) -> None:
        workflow = read_text(FORMAL_CONTROLLER_WORKFLOW)
        self.assertIn(
            'git rev-list --parents -n 1 "$GITHUB_SHA"',
            workflow,
        )
        self.assertIn(
            '[[ "${#bundle_commit[@]}" -eq 2 ]]',
            workflow,
        )
        self.assertIn(
            '[[ "${bundle_commit[1]}" == "$benchmark_revision" ]]',
            workflow,
        )
        self.assertIn(
            "git diff-tree",
            workflow,
        )
        expected = [
            "benchmarks/web-performance/controller/frozen/controller-lock.json",
            "benchmarks/web-performance/controller/frozen/formal-inputs/"
            "bundle-manifest.json",
            "benchmarks/web-performance/controller/frozen/formal-inputs/"
            "matrix.json",
            "benchmarks/web-performance/controller/frozen/formal-inputs/"
            "runtime-admission-identities.json",
            "benchmarks/web-performance/controller/frozen/formal-inputs/"
            "schedule.json",
            "benchmarks/web-performance/controller/frozen/manifest.json",
        ]
        prefix = "$'A\\t"
        actual = [
            line.strip()[len(prefix) : -1]
            for line in workflow.splitlines()
            if line.strip().startswith(prefix) and line.strip().endswith("'")
        ]
        self.assertEqual(
            actual,
            expected,
            "workflow allowlist must contain exactly the six generated files",
        )

    def test_entrypoint_verifies_init_and_main_image_identity(self) -> None:
        entrypoint = read_text(ENTRYPOINT)
        required = (
            ".spec.initContainers[]?",
            ".spec.containers[]?",
            ".status.initContainerStatuses[]?",
            ".status.containerStatuses[]?",
            '"$init_image_reference" == "$controller_image_reference"',
            '"$controller_image_reference" == '
            '*"@$BLUEMAP_CONTROLLER_IMAGE_DIGEST"',
            '"$init_image_id" == *"@$BLUEMAP_CONTROLLER_IMAGE_DIGEST"',
            '"$controller_image_id" == *"@$BLUEMAP_CONTROLLER_IMAGE_DIGEST"',
        )
        for fragment in required:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, entrypoint)

    def test_dockerfile_consumes_only_tracked_formal_bundle_paths(self) -> None:
        dockerfile = read_text(DOCKERFILE)
        required = (
            "benchmarks/web-performance/controller/formal",
            "benchmarks/web-performance/controller/frozen",
        )
        for fragment in required:
            with self.subTest(fragment=fragment):
                self.assertIn(
                    fragment,
                    dockerfile,
                    f"controller image must consume tracked {fragment!r}",
                )

        ignored_artifact_paths = (
            "artifacts/formal-orchestrator",
            "artifacts/formal-analysis",
            "artifacts/snapshot/formal-inputs",
            "artifacts/snapshot/manifest.json",
        )
        for fragment in ignored_artifact_paths:
            with self.subTest(fragment=fragment):
                self.assertNotIn(
                    fragment,
                    dockerfile,
                    f"Dockerfile must not COPY ignored benchmark artifact {fragment!r}",
                )

    def test_dockerfile_base_and_runtime_are_nonroot(self) -> None:
        dockerfile = read_text(DOCKERFILE)
        first_from = next(
            line.strip() for line in dockerfile.splitlines() if line.startswith("FROM ")
        )
        self.assertRegex(
            first_from,
            r"^FROM \S+@sha256:[0-9a-f]{64}(?:\s+AS\s+\S+)?$",
            "the controller base image must be pinned by digest",
        )
        self.assertIn("USER 12345:12345", dockerfile)

    def test_kubeconfig_is_in_cluster_and_uses_projected_token(self) -> None:
        config = yaml.safe_load(read_text(KUBECONFIG))
        cluster = config["clusters"][0]["cluster"]
        user = config["users"][0]["user"]
        context = config["contexts"][0]["context"]
        self.assertEqual(cluster["server"], "https://kubernetes.default.svc")
        self.assertEqual(
            cluster["certificate-authority"],
            "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        )
        self.assertEqual(
            user["tokenFile"],
            "/var/run/secrets/kubernetes.io/serviceaccount/token",
        )
        self.assertEqual(context["namespace"], NAMESPACE)
        self.assertNotIn("token", user)
        self.assertNotIn("client-key", user)


class PublicRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = load_documents(PUBLIC_INGRESS)

    def test_public_service_selects_all_benchmark_web_candidates(self) -> None:
        service = one_resource(self.documents, "Service", PUBLIC_SERVICE)
        self.assertEqual(service["metadata"].get("namespace"), NAMESPACE)
        self.assertEqual(service["spec"].get("type"), "ClusterIP")
        self.assertEqual(
            service["spec"].get("selector"),
            {
                "app.kubernetes.io/name": "bluemap-web",
                "app.kubernetes.io/part-of": "bluemap-web-performance",
            },
        )
        self.assertEqual(
            service["spec"].get("ports"),
            [
                {
                    "name": "http",
                    "port": 8100,
                    "protocol": "TCP",
                    "targetPort": "http",
                }
            ],
        )

    def test_public_ingress_routes_exact_host_to_public_service(self) -> None:
        ingress = one_resource(self.documents, "Ingress", PUBLIC_SERVICE)
        self.assertEqual(ingress["metadata"].get("namespace"), NAMESPACE)
        self.assertEqual(ingress["spec"].get("ingressClassName"), "traefik")
        self.assertEqual(
            ingress["spec"].get("rules"),
            [
                {
                    "host": PUBLIC_HOST,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": PUBLIC_SERVICE,
                                        "port": {"name": "http"},
                                    }
                                },
                            }
                        ]
                    },
                }
            ],
        )


@unittest.skipUnless(
    FORMAL_CONTROLLER.exists(),
    f"{FORMAL_CONTROLLER} has not been created yet; controller-manifest checks skipped",
)
class FormalControllerManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = load_documents(FORMAL_CONTROLLER)
        cls.job = one_resource(cls.documents, "Job", CONTROLLER_NAME)
        cls.role = one_resource(cls.documents, "Role", CONTROLLER_NAME)
        cls.pvc = one_resource(
            cls.documents, "PersistentVolumeClaim", "bluemap-perf-formal-artifacts"
        )

    def test_manifest_uses_namespaced_controller_identity_only(self) -> None:
        forbidden_kinds = {"ClusterRole", "ClusterRoleBinding", "Secret"}
        self.assertFalse(
            forbidden_kinds & {document.get("kind") for document in self.documents},
            "controller package must not define cluster-wide RBAC or embed a Secret",
        )

        service_account = one_resource(
            self.documents, "ServiceAccount", CONTROLLER_NAME
        )
        role_binding = one_resource(self.documents, "RoleBinding", CONTROLLER_NAME)
        self.assertEqual(service_account["metadata"].get("namespace"), NAMESPACE)
        self.assertEqual(self.role["metadata"].get("namespace"), NAMESPACE)
        self.assertEqual(role_binding["metadata"].get("namespace"), NAMESPACE)
        self.assertEqual(
            role_binding.get("roleRef"),
            {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": CONTROLLER_NAME,
            },
        )
        self.assertEqual(
            role_binding.get("subjects"),
            [
                {
                    "kind": "ServiceAccount",
                    "name": CONTROLLER_NAME,
                    "namespace": NAMESPACE,
                }
            ],
        )

    def test_manifest_has_no_protected_resource_reference(self) -> None:
        manifest = read_text(FORMAL_CONTROLLER).lower()
        for reference in PROTECTED_REFERENCES:
            with self.subTest(reference=reference):
                self.assertNotIn(
                    reference,
                    manifest,
                    f"formal controller must never reference protected {reference}",
                )

        spec = pod_spec(self.job)
        claims = {
            volume["persistentVolumeClaim"]["claimName"]
            for volume in spec.get("volumes", [])
            if "persistentVolumeClaim" in volume
        }
        self.assertNotIn("minecraft-data", claims)

    def test_artifact_pvc_is_dedicated_longhorn_20gib_rwo(self) -> None:
        self.assertEqual(self.pvc["metadata"].get("namespace"), NAMESPACE)
        spec = self.pvc["spec"]
        self.assertEqual(spec.get("storageClassName"), "longhorn")
        self.assertEqual(spec.get("accessModes"), ["ReadWriteOnce"])
        self.assertEqual(
            spec.get("resources", {}).get("requests", {}).get("storage"), "20Gi"
        )

    def test_job_has_non_restarting_single_attempt_lifecycle(self) -> None:
        job_spec = self.job["spec"]
        spec = pod_spec(self.job)
        self.assertEqual(job_spec.get("backoffLimit"), 0)
        self.assertEqual(job_spec.get("parallelism", 1), 1)
        self.assertEqual(job_spec.get("completions", 1), 1)
        self.assertNotIn(
            "ttlSecondsAfterFinished",
            job_spec,
            "completed/failed controller evidence must not be removed automatically",
        )
        deadline = job_spec.get("activeDeadlineSeconds")
        self.assertIsInstance(deadline, int)
        self.assertGreater(deadline, 0)
        self.assertLessEqual(deadline, 72_000)
        self.assertEqual(spec.get("restartPolicy"), "Never")
        self.assertEqual(spec.get("serviceAccountName"), CONTROLLER_NAME)
        self.assertEqual(spec.get("nodeSelector"), {"kubernetes.io/hostname": "contabo1"})
        self.assertEqual(
            spec.get("terminationGracePeriodSeconds"),
            2_100,
            "grace must cover 40 seconds of child termination, six sequential "
            "300-second cleanup waits, and a 260-second margin",
        )
        controller = spec.get("containers", [])[0]
        pod_name_env = [
            item
            for item in controller.get("env", [])
            if item.get("name") == "BLUEMAP_CONTROLLER_POD_NAME"
        ]
        self.assertEqual(
            pod_name_env,
            [
                {
                    "name": "BLUEMAP_CONTROLLER_POD_NAME",
                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                }
            ],
            "controller must receive its actual Pod name for imageID verification",
        )

    def test_all_managed_resources_have_benchmark_safety_labels(self) -> None:
        expected = {
            "app.kubernetes.io/part-of": "bluemap-web-performance",
            "bluemap.guenter.cloud/experiment-id": "replace-with-formal-run-id",
        }
        for document in self.documents:
            with self.subTest(
                kind=document.get("kind"),
                name=document.get("metadata", {}).get("name"),
            ):
                labels = document.get("metadata", {}).get("labels", {})
                for key, value in expected.items():
                    self.assertEqual(labels.get(key), value)
        template_labels = self.job["spec"]["template"]["metadata"].get("labels", {})
        for key, value in expected.items():
            self.assertEqual(template_labels.get(key), value)

    def test_job_images_are_immutable_digests(self) -> None:
        spec = pod_spec(self.job)
        containers = spec.get("initContainers", []) + spec.get("containers", [])
        self.assertGreaterEqual(len(containers), 2)
        for container in containers:
            with self.subTest(container=container.get("name")):
                self.assertRegex(
                    container.get("image", ""),
                    IMMUTABLE_IMAGE,
                    "every controller and init image must be pinned by sha256 digest",
                )
                self.assertNotEqual(
                    container.get("imagePullPolicy"),
                    "Never",
                    "a digest-pinned controller image still needs to be pullable",
                )
        init = next(
            container
            for container in spec.get("initContainers", [])
            if container.get("name") == "prepare-runpod-credentials"
        )
        controller = next(
            container
            for container in spec.get("containers", [])
            if container.get("name") == "controller"
        )
        self.assertEqual(
            init["image"],
            controller["image"],
            "the init and main containers must use the identical image reference",
        )

    def test_job_and_all_containers_are_locked_to_nonroot_uid(self) -> None:
        spec = pod_spec(self.job)
        security = spec.get("securityContext", {})
        self.assertIs(security.get("runAsNonRoot"), True)
        self.assertEqual(security.get("runAsUser"), CONTROLLER_UID)
        self.assertEqual(security.get("runAsGroup"), CONTROLLER_UID)
        self.assertEqual(security.get("fsGroup"), CONTROLLER_UID)
        self.assertEqual(
            security.get("seccompProfile"), {"type": "RuntimeDefault"}
        )

        containers = spec.get("initContainers", []) + spec.get("containers", [])
        for container in containers:
            with self.subTest(container=container.get("name")):
                container_security = container.get("securityContext", {})
                self.assertIs(container_security.get("runAsNonRoot"), True)
                self.assertEqual(container_security.get("runAsUser"), CONTROLLER_UID)
                self.assertEqual(container_security.get("runAsGroup"), CONTROLLER_UID)
                self.assertIs(container_security.get("allowPrivilegeEscalation"), False)
                self.assertIs(container_security.get("readOnlyRootFilesystem"), True)
                self.assertEqual(
                    container_security.get("capabilities", {}).get("drop"), ["ALL"]
                )

    def test_nonroot_init_copies_runpod_key_to_private_emptydir(self) -> None:
        spec = pod_spec(self.job)
        secret_volumes = [
            volume for volume in spec.get("volumes", []) if "secret" in volume
        ]
        self.assertEqual(
            len(secret_volumes),
            1,
            "exactly one projected RunPod SSH Secret volume is expected",
        )
        secret_volume = secret_volumes[0]
        self.assertEqual(
            secret_volume["secret"].get("secretName"), "bluemap-perf-runpod-ssh"
        )
        self.assertEqual(
            secret_volume["secret"].get("defaultMode"),
            0o440,
            "projected key should be group-readable only for the nonroot init copy",
        )

        credential_volumes = [
            volume
            for volume in spec.get("volumes", [])
            if "emptyDir" in volume
            and any(
                mount.get("name") == volume.get("name")
                and mount.get("mountPath") == "/opt/bluemap-runtime/credentials"
                for container in spec.get("containers", [])
                for mount in container.get("volumeMounts", [])
            )
        ]
        self.assertEqual(
            len(credential_volumes),
            1,
            "controller credentials must live on one emptyDir populated by init",
        )
        credentials_name = credential_volumes[0]["name"]

        copying_init = []
        for container in spec.get("initContainers", []):
            command = " ".join(
                str(part)
                for part in container.get("command", []) + container.get("args", [])
            )
            mounted_names = {
                mount.get("name") for mount in container.get("volumeMounts", [])
            }
            if secret_volume["name"] in mounted_names and credentials_name in mounted_names:
                copying_init.append((container, command))
        self.assertEqual(
            len(copying_init),
            1,
            "one nonroot init container must copy Secret key into credentials emptyDir",
        )
        init, command = copying_init[0]
        security = init.get("securityContext", {})
        self.assertEqual(security.get("runAsUser"), CONTROLLER_UID)
        self.assertEqual(security.get("runAsGroup"), CONTROLLER_UID)
        self.assertRegex(command, r"\bumask\s+0?77\b")
        self.assertRegex(
            command,
            r"(?:\binstall\b[^\n;]*(?:-m|--mode)[ =]?0?600\b|"
            r"\bchmod\s+0?600\b)",
            "init copy must explicitly create a mode-0600 SSH identity",
        )

        controller = spec.get("containers", [])
        self.assertEqual(len(controller), 1, "Job should have one controller container")
        credentials_mount = mount_by_path(
            controller[0], "/opt/bluemap-runtime/credentials"
        )
        self.assertEqual(credentials_mount.get("name"), credentials_name)
        self.assertIs(
            credentials_mount.get("readOnly"),
            True,
            "controller must consume the copied key read-only",
        )

    def test_job_mounts_only_its_dedicated_artifact_claim(self) -> None:
        spec = pod_spec(self.job)
        claims = [
            (volume["name"], volume["persistentVolumeClaim"]["claimName"])
            for volume in spec.get("volumes", [])
            if "persistentVolumeClaim" in volume
        ]
        self.assertEqual(
            claims,
            [("artifacts", "bluemap-perf-formal-artifacts")],
            "the formal controller must mount only its dedicated artifact PVC",
        )
        controllers = spec.get("containers", [])
        self.assertEqual(len(controllers), 1)
        artifact_mounts = [
            mount
            for mount in controllers[0].get("volumeMounts", [])
            if mount.get("name") == "artifacts"
        ]
        self.assertEqual(len(artifact_mounts), 1)
        self.assertFalse(artifact_mounts[0].get("readOnly", False))

    def test_job_contains_no_in_cluster_load_generator(self) -> None:
        job_text = yaml.safe_dump(self.job, sort_keys=True).lower()
        for forbidden in (
            "bluemap-perf-loadgen",
            "--load-generator-backend pod",
            "--load-generator-backend local",
            "kubectl exec",
            "kubectl port-forward",
            "k6 run",
        ):
            with self.subTest(fragment=forbidden):
                self.assertNotIn(forbidden, job_text)

    def test_role_is_the_exact_minimal_controller_authority(self) -> None:
        allowed: dict[tuple[str, str], tuple[frozenset[str], frozenset[str] | None]]
        allowed = {
            ("apps", "deployments"): (frozenset({"get"}), CANDIDATE_DEPLOYMENTS),
            ("apps", "deployments/scale"): (
                frozenset({"get", "patch", "update"}),
                CANDIDATE_DEPLOYMENTS,
            ),
            ("apps", "replicasets"): (frozenset({"get"}), None),
            ("", "pods"): (frozenset({"get", "list"}), None),
            ("", "services"): (frozenset({"get"}), FORMAL_SERVICES),
            ("", "configmaps"): (frozenset({"get"}), FORMAL_CONFIGMAPS),
            ("networking.k8s.io", "ingresses"): (
                frozenset({"get"}),
                FORMAL_INGRESSES,
            ),
            ("discovery.k8s.io", "endpointslices"): (
                frozenset({"list"}),
                None,
            ),
            ("metrics.k8s.io", "pods"): (frozenset({"get"}), None),
        }
        observed_verbs: dict[tuple[str, str], set[str]] = defaultdict(set)
        observed_names: dict[tuple[str, str], set[str]] = defaultdict(set)

        for index, rule in enumerate(self.role.get("rules", [])):
            api_groups = rule.get("apiGroups", [])
            resources = rule.get("resources", [])
            verbs = rule.get("verbs", [])
            names = rule.get("resourceNames")
            self.assertEqual(
                len(api_groups),
                1,
                f"Role rule {index} must use one API group so its grant is auditable",
            )
            self.assertNotIn("*", resources)
            self.assertNotIn("*", verbs)
            self.assertFalse(
                {"create", "delete", "deletecollection"} & set(verbs),
                f"Role rule {index} contains a destructive verb",
            )

            for resource in resources:
                key = (api_groups[0], resource)
                self.assertIn(
                    key,
                    allowed,
                    f"Role rule {index} grants unexpected API resource {key}",
                )
                allowed_verbs, expected_names = allowed[key]
                self.assertTrue(
                    set(verbs) <= allowed_verbs,
                    f"Role rule {index} overgrants verbs for {key}",
                )
                if expected_names is None:
                    self.assertIsNone(
                        names,
                        f"dynamic {key} reads must not carry misleading resourceNames",
                    )
                else:
                    self.assertIsNotNone(
                        names, f"{key} must be restricted with resourceNames"
                    )
                    self.assertTrue(
                        set(names) <= expected_names,
                        f"Role rule {index} names unexpected {key} objects",
                    )
                    observed_names[key].update(names)
                observed_verbs[key].update(verbs)

        self.assertEqual(set(observed_verbs), set(allowed))
        for key, (expected_verbs, expected_names) in allowed.items():
            with self.subTest(resource=key):
                self.assertEqual(frozenset(observed_verbs[key]), expected_verbs)
                if expected_names is not None:
                    self.assertEqual(frozenset(observed_names[key]), expected_names)

        role_text = yaml.safe_dump(self.role, sort_keys=True).lower()
        for forbidden in (
            "pods/exec",
            "pods/portforward",
            "secrets",
            "persistentvolumeclaims",
        ):
            self.assertNotIn(forbidden, role_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
