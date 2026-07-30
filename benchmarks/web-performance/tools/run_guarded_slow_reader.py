#!/usr/bin/env python3
"""Run the destructive graceful-drain probe against one verified benchmark Pod."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PART_OF_LABEL = "app.kubernetes.io/part-of"
PART_OF_VALUE = "bluemap-web-performance"
EXPERIMENT_LABEL = "bluemap.guenter.cloud/experiment-id"
BENCHMARK_PREFIX = "bluemap-perf-"
PROTECTED_NAMES = {"minecraft", "minecraft-data"}
DNS_SUBDOMAIN = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
LABEL_VALUE = re.compile(r"^[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?$")


class SafetyError(RuntimeError):
    """A destructive-action guard failed."""


@dataclass(frozen=True)
class VerifiedTarget:
    namespace: str
    experiment_id: str
    deployment_name: str
    deployment_uid: str
    replicaset_name: str
    replicaset_uid: str
    pod_name: str
    pod_uid: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify one disposable BlueMap Deployment/Pod ownership chain, "
            "then delete only that Pod while a large response is read slowly."
        )
    )
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument(
        "--confirm-delete-pod",
        required=True,
        help="must exactly repeat --pod to authorize its deletion",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--local-port", type=int, default=18100)
    parser.add_argument("--remote-port", type=int, default=8100)
    parser.add_argument("--grace-period-seconds", type=int, default=30)
    parser.add_argument("--rollout-timeout-seconds", type=int, default=120)
    parser.add_argument("--request-timeout-seconds", type=float, default=90)
    parser.add_argument("--ready-timeout-seconds", type=float, default=30)
    parser.add_argument("--bytes-per-second", type=int, default=1024 * 1024)
    parser.add_argument("--initial-delay-seconds", type=float, default=2)
    parser.add_argument("--minimum-object-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--accept-encoding", default="zstd")
    return parser.parse_args()


def require_exact_benchmark_name(value: str, field: str) -> None:
    if value in PROTECTED_NAMES:
        raise SafetyError(f"{field} explicitly rejects protected target {value!r}")
    if not value.startswith(BENCHMARK_PREFIX):
        raise SafetyError(f"{field} must start with {BENCHMARK_PREFIX!r}")
    if len(value) > 253 or DNS_SUBDOMAIN.fullmatch(value) is None:
        raise SafetyError(f"{field} must be one exact Kubernetes resource name")


def require_namespace(value: str) -> None:
    if len(value) > 63 or DNS_SUBDOMAIN.fullmatch(value) is None or "." in value:
        raise SafetyError("--namespace must be one exact Kubernetes namespace name")


def require_experiment_id(value: str) -> None:
    if len(value) > 63 or LABEL_VALUE.fullmatch(value) is None:
        raise SafetyError("--experiment-id must be one nonempty Kubernetes label value")


def metadata(
    resource: dict[str, Any],
    kind: str,
    api_version: str,
    name: str,
    namespace: str,
) -> dict:
    if resource.get("kind") != kind or resource.get("apiVersion") != api_version:
        raise SafetyError(
            f"expected {api_version} {kind}, received "
            f"{resource.get('apiVersion')!r} {resource.get('kind')!r}"
        )
    value = resource.get("metadata")
    if not isinstance(value, dict):
        raise SafetyError(f"{kind}/{name} has no metadata")
    if value.get("name") != name or value.get("namespace") != namespace:
        raise SafetyError(
            f"{kind}/{name} response did not exactly match its requested identity"
        )
    if not isinstance(value.get("uid"), str) or not value["uid"]:
        raise SafetyError(f"{kind}/{name} has no UID")
    return value


def require_safety_labels(
    resource_metadata: dict[str, Any],
    kind: str,
    name: str,
    experiment_id: str,
) -> None:
    labels = resource_metadata.get("labels")
    if not isinstance(labels, dict):
        raise SafetyError(f"{kind}/{name} has no labels")
    if labels.get(PART_OF_LABEL) != PART_OF_VALUE:
        raise SafetyError(f"{kind}/{name} must have {PART_OF_LABEL}={PART_OF_VALUE}")
    if labels.get(EXPERIMENT_LABEL) != experiment_id:
        raise SafetyError(
            f"{kind}/{name} must have exact {EXPERIMENT_LABEL}={experiment_id}"
        )


def controller_reference(
    resource_metadata: dict[str, Any],
    resource: str,
    expected_kind: str,
) -> dict[str, Any]:
    references = resource_metadata.get("ownerReferences", [])
    if not isinstance(references, list):
        raise SafetyError(f"{resource} ownerReferences is malformed")
    controllers = [
        reference
        for reference in references
        if isinstance(reference, dict) and reference.get("controller") is True
    ]
    if len(controllers) != 1 or controllers[0].get("kind") != expected_kind:
        raise SafetyError(
            f"{resource} must have exactly one {expected_kind} controller"
        )
    reference = controllers[0]
    if not isinstance(reference.get("name"), str) or not reference["name"]:
        raise SafetyError(f"{resource} controller name is missing")
    if not isinstance(reference.get("uid"), str) or not reference["uid"]:
        raise SafetyError(f"{resource} controller UID is missing")
    return reference


def label_selector_matches(selector: Any, labels: Any) -> bool:
    if not isinstance(selector, dict) or not isinstance(labels, dict):
        return False
    match_labels = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if not isinstance(match_labels, dict) or not isinstance(expressions, list):
        return False
    if not match_labels and not expressions:
        return False
    if any(labels.get(key) != value for key, value in match_labels.items()):
        return False

    for expression in expressions:
        if not isinstance(expression, dict):
            return False
        key = expression.get("key")
        operator = expression.get("operator")
        values = expression.get("values", [])
        if not isinstance(key, str) or not isinstance(values, list):
            return False
        if operator == "In":
            if not values or labels.get(key) not in values:
                return False
        elif operator == "NotIn":
            if not values or (key in labels and labels[key] in values):
                return False
        elif operator == "Exists":
            if values or key not in labels:
                return False
        elif operator == "DoesNotExist":
            if values or key in labels:
                return False
        else:
            return False
    return True


def require_ready_pod(pod: dict[str, Any], pod_name: str) -> None:
    pod_metadata = pod["metadata"]
    if pod_metadata.get("deletionTimestamp") is not None:
        raise SafetyError(f"Pod/{pod_name} is already terminating")
    status = pod.get("status")
    if not isinstance(status, dict) or status.get("phase") != "Running":
        raise SafetyError(f"Pod/{pod_name} is not Running")
    conditions = status.get("conditions", [])
    if not isinstance(conditions, list) or not any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    ):
        raise SafetyError(f"Pod/{pod_name} is not Ready")


def validate_target(
    deployment: dict[str, Any],
    replicaset: dict[str, Any],
    pod: dict[str, Any],
    *,
    namespace: str,
    deployment_name: str,
    pod_name: str,
    experiment_id: str,
) -> VerifiedTarget:
    require_namespace(namespace)
    require_exact_benchmark_name(deployment_name, "--deployment")
    require_exact_benchmark_name(pod_name, "--pod")
    require_experiment_id(experiment_id)

    deployment_metadata = metadata(
        deployment, "Deployment", "apps/v1", deployment_name, namespace
    )
    pod_metadata = metadata(pod, "Pod", "v1", pod_name, namespace)
    require_safety_labels(
        deployment_metadata, "Deployment", deployment_name, experiment_id
    )
    require_safety_labels(pod_metadata, "Pod", pod_name, experiment_id)
    require_ready_pod(pod, pod_name)

    pod_controller = controller_reference(pod_metadata, f"Pod/{pod_name}", "ReplicaSet")
    replicaset_name = pod_controller["name"]
    require_exact_benchmark_name(replicaset_name, "Pod controller ReplicaSet")
    replicaset_metadata = metadata(
        replicaset, "ReplicaSet", "apps/v1", replicaset_name, namespace
    )
    if pod_controller["uid"] != replicaset_metadata["uid"]:
        raise SafetyError("Pod controller UID does not match the ReplicaSet UID")

    replicaset_controller = controller_reference(
        replicaset_metadata, f"ReplicaSet/{replicaset_name}", "Deployment"
    )
    if (
        replicaset_controller["name"] != deployment_name
        or replicaset_controller["uid"] != deployment_metadata["uid"]
    ):
        raise SafetyError(
            "ReplicaSet is not owned by the exact named Deployment and UID"
        )

    pod_labels = pod_metadata.get("labels")
    deployment_selector = deployment.get("spec", {}).get("selector")
    replicaset_selector = replicaset.get("spec", {}).get("selector")
    if not label_selector_matches(deployment_selector, pod_labels):
        raise SafetyError("named Deployment selector does not select the named Pod")
    if not label_selector_matches(replicaset_selector, pod_labels):
        raise SafetyError("owning ReplicaSet selector does not select the named Pod")

    return VerifiedTarget(
        namespace=namespace,
        experiment_id=experiment_id,
        deployment_name=deployment_name,
        deployment_uid=deployment_metadata["uid"],
        replicaset_name=replicaset_name,
        replicaset_uid=replicaset_metadata["uid"],
        pod_name=pod_name,
        pod_uid=pod_metadata["uid"],
    )


class Kubectl:
    def __init__(
        self,
        kubeconfig: Path,
        namespace: str,
        runner: Runner = subprocess.run,
    ) -> None:
        self.base = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            namespace,
        ]
        self.namespace = namespace
        self.runner = runner

    def run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                [*self.base, *arguments],
                input=input_text,
                text=True,
                capture_output=True,
                check=True,
                timeout=timeout,
            )
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            raise SafetyError(
                f"kubectl {' '.join(arguments)} failed: {stderr or error}"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise SafetyError(f"kubectl {' '.join(arguments)} timed out") from error

    def get_json(self, kind: str, name: str) -> dict[str, Any]:
        result = self.run(["get", kind, name, "--output=json"])
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SafetyError(
                f"kubectl returned invalid JSON for {kind}/{name}"
            ) from error
        if not isinstance(value, dict):
            raise SafetyError(f"kubectl returned non-object JSON for {kind}/{name}")
        return value

    def verify(
        self,
        deployment_name: str,
        pod_name: str,
        experiment_id: str,
    ) -> VerifiedTarget:
        deployment = self.get_json("deployment", deployment_name)
        pod = self.get_json("pod", pod_name)
        pod_metadata = metadata(pod, "Pod", "v1", pod_name, self.namespace)
        pod_controller = controller_reference(
            pod_metadata, f"Pod/{pod_name}", "ReplicaSet"
        )
        replicaset = self.get_json("replicaset", pod_controller["name"])
        return validate_target(
            deployment,
            replicaset,
            pod,
            namespace=self.namespace,
            deployment_name=deployment_name,
            pod_name=pod_name,
            experiment_id=experiment_id,
        )

    def delete_verified_pod(
        self,
        target: VerifiedTarget,
        grace_period_seconds: int,
    ) -> dict[str, Any]:
        namespace = urllib.parse.quote(target.namespace, safe="")
        pod_name = urllib.parse.quote(target.pod_name, safe="")
        path = f"/api/v1/namespaces/{namespace}/pods/{pod_name}"
        options = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "gracePeriodSeconds": grace_period_seconds,
            "propagationPolicy": "Background",
            "preconditions": {"uid": target.pod_uid},
        }
        result = self.run(
            ["delete", f"--raw={path}", "--filename=-"],
            input_text=json.dumps(options),
        )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SafetyError("Pod deletion returned invalid JSON") from error
        if not isinstance(response, dict):
            raise SafetyError("Pod deletion returned non-object JSON")
        return response


def same_target(expected: VerifiedTarget, actual: VerifiedTarget) -> None:
    if actual != expected:
        raise SafetyError(
            "target identity changed after verification; refusing Pod deletion"
        )


def load_large_object(manifest_path: Path) -> str:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SafetyError(f"could not read manifest: {error}") from error
    path = manifest.get("largeObject") if isinstance(manifest, dict) else None
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
    ):
        raise SafetyError("manifest largeObject must be one absolute HTTP path")
    return path


def ensure_port_available(port: int) -> None:
    if not 1024 <= port <= 65535:
        raise SafetyError("--local-port must be between 1024 and 65535")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            raise SafetyError(f"local port {port} is unavailable") from error


def wait_for_port_forward(
    process: subprocess.Popen[str],
    port: int,
    log_path: Path,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SafetyError(f"kubectl port-forward exited early; inspect {log_path}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise SafetyError(f"kubectl port-forward did not become ready; inspect {log_path}")


def capture_expected_response(
    port: int,
    path: str,
    accept_encoding: str,
    timeout_seconds: float,
    minimum_object_bytes: int,
    headers_path: Path,
) -> tuple[int, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_seconds)
    digest = hashlib.sha256()
    byte_count = 0
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept-Encoding": accept_encoding,
                "User-Agent": "BlueMap-Slow-Reader/baseline",
            },
        )
        response = connection.getresponse()
        headers = response.getheaders()
        headers_path.write_text(
            json.dumps(
                {"status": response.status, "headers": headers},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if response.status != 200:
            raise SafetyError(
                f"large-object baseline returned HTTP {response.status}, expected 200"
            )
        while chunk := response.read(64 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
        declared_length = response.getheader("Content-Length")
        if declared_length is not None:
            try:
                expected_length = int(declared_length)
            except ValueError as error:
                raise SafetyError(
                    "baseline Content-Length is not an integer"
                ) from error
            if expected_length != byte_count:
                raise SafetyError(
                    "baseline transferred length does not match Content-Length"
                )
    finally:
        connection.close()

    if byte_count < minimum_object_bytes:
        raise SafetyError(
            f"large-object baseline is only {byte_count} bytes; "
            f"minimum is {minimum_object_bytes}"
        )
    return byte_count, digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def wait_for_ready_file(
    ready_path: Path,
    process: subprocess.Popen[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if ready_path.is_file() and ready_path.stat().st_size:
            try:
                value = json.loads(ready_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise SafetyError("slow-reader ready file is invalid JSON") from error
            if not isinstance(value, dict):
                raise SafetyError("slow-reader ready file is not a JSON object")
            return value
        if process.poll() is not None:
            raise SafetyError("slow reader exited before becoming ready")
        time.sleep(0.1)
    raise SafetyError("slow reader did not become ready before timeout")


def terminate_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def validate_numeric_args(args: argparse.Namespace) -> None:
    for name in (
        "remote_port",
        "grace_period_seconds",
        "rollout_timeout_seconds",
        "bytes_per_second",
        "minimum_object_bytes",
    ):
        if getattr(args, name) < 1:
            raise SafetyError(f"--{name.replace('_', '-')} must be positive")
    if args.remote_port > 65535:
        raise SafetyError("--remote-port must not exceed 65535")
    for name in ("request_timeout_seconds", "ready_timeout_seconds"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            raise SafetyError(f"--{name.replace('_', '-')} must be positive")
    if not math.isfinite(args.initial_delay_seconds) or args.initial_delay_seconds < 0:
        raise SafetyError("--initial-delay-seconds must not be negative")
    if args.accept_encoding not in {"zstd", "gzip", "deflate", "identity"}:
        raise SafetyError("--accept-encoding must be zstd, gzip, deflate, or identity")


def execute(args: argparse.Namespace) -> dict[str, Any]:
    validate_numeric_args(args)
    require_namespace(args.namespace)
    require_exact_benchmark_name(args.deployment, "--deployment")
    require_exact_benchmark_name(args.pod, "--pod")
    require_experiment_id(args.experiment_id)
    if args.confirm_delete_pod != args.pod:
        raise SafetyError("--confirm-delete-pod must exactly equal --pod")
    if not args.kubeconfig.is_file():
        raise SafetyError("--kubeconfig must name an existing file")
    if args.artifact_dir.exists():
        raise SafetyError("--artifact-dir must not already exist")
    large_object = load_large_object(args.manifest)
    ensure_port_available(args.local_port)
    args.artifact_dir.mkdir(parents=True, mode=0o700)

    kubectl = Kubectl(args.kubeconfig, args.namespace)
    target = kubectl.verify(args.deployment, args.pod, args.experiment_id)
    atomic_json(args.artifact_dir / "verified-target.json", asdict(target))

    port_forward: subprocess.Popen[str] | None = None
    slow_reader: subprocess.Popen[str] | None = None
    deleted = False
    port_log_path = args.artifact_dir / "port-forward.log"
    slow_log_path = args.artifact_dir / "slow-reader.log"
    try:
        with port_log_path.open("w", encoding="utf-8") as port_log:
            port_forward = subprocess.Popen(
                [
                    *kubectl.base,
                    "port-forward",
                    f"pod/{target.pod_name}",
                    f"{args.local_port}:{args.remote_port}",
                    "--address=127.0.0.1",
                ],
                text=True,
                stdout=port_log,
                stderr=subprocess.STDOUT,
            )
        wait_for_port_forward(
            port_forward,
            args.local_port,
            port_log_path,
            args.ready_timeout_seconds,
        )
        same_target(
            target,
            kubectl.verify(args.deployment, args.pod, args.experiment_id),
        )

        expected_length, expected_sha256 = capture_expected_response(
            args.local_port,
            large_object,
            args.accept_encoding,
            args.request_timeout_seconds,
            args.minimum_object_bytes,
            args.artifact_dir / "expected-headers.json",
        )
        expected = {
            "path": large_object,
            "acceptEncoding": args.accept_encoding,
            "storedRepresentationLength": expected_length,
            "storedRepresentationSha256": expected_sha256,
        }
        atomic_json(args.artifact_dir / "expected.json", expected)

        ready_path = args.artifact_dir / "ready.json"
        result_path = args.artifact_dir / "result.json"
        with slow_log_path.open("w", encoding="utf-8") as slow_log:
            slow_reader = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("slow_reader.py")),
                    f"http://127.0.0.1:{args.local_port}{large_object}",
                    "--accept-encoding",
                    args.accept_encoding,
                    "--expected-length",
                    str(expected_length),
                    "--expected-sha256",
                    expected_sha256,
                    "--bytes-per-second",
                    str(args.bytes_per_second),
                    "--initial-delay-seconds",
                    str(args.initial_delay_seconds),
                    "--timeout-seconds",
                    str(args.request_timeout_seconds),
                    "--user-agent",
                    f"BlueMap-Slow-Reader/{args.experiment_id}",
                    "--ready-file",
                    str(ready_path),
                    "--output",
                    str(result_path),
                ],
                text=True,
                stdout=slow_log,
                stderr=subprocess.STDOUT,
            )
        wait_for_ready_file(ready_path, slow_reader, args.ready_timeout_seconds)

        same_target(
            target,
            kubectl.verify(args.deployment, args.pod, args.experiment_id),
        )
        deletion_response = kubectl.delete_verified_pod(
            target, args.grace_period_seconds
        )
        deleted = True
        atomic_json(args.artifact_dir / "deletion-response.json", deletion_response)

        try:
            slow_reader_exit = slow_reader.wait(
                timeout=args.request_timeout_seconds + 5
            )
        except subprocess.TimeoutExpired as error:
            raise SafetyError("slow reader exceeded its overall timeout") from error
        if slow_reader_exit != 0:
            raise SafetyError(f"slow reader failed; inspect {slow_log_path}")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SafetyError(
                "slow reader did not produce valid result JSON"
            ) from error
        if not isinstance(result, dict) or result.get("complete") is not True:
            raise SafetyError("slow reader did not complete the verified response")

        kubectl.run(
            [
                "wait",
                "--for=delete",
                f"pod/{target.pod_name}",
                f"--timeout={args.rollout_timeout_seconds}s",
            ],
            timeout=args.rollout_timeout_seconds + 10,
        )
        kubectl.run(
            [
                "rollout",
                "status",
                f"deployment/{target.deployment_name}",
                f"--timeout={args.rollout_timeout_seconds}s",
            ],
            timeout=args.rollout_timeout_seconds + 10,
        )
        return {
            "verifiedTarget": asdict(target),
            "expected": expected,
            "result": result,
            "podDeletionSubmitted": True,
            "replacementRolloutReady": True,
        }
    finally:
        terminate_process(slow_reader)
        terminate_process(port_forward)
        atomic_json(
            args.artifact_dir / "run-state.json",
            {
                "podDeletionSubmitted": deleted,
                "finishedAtEpochSeconds": time.time(),
            },
        )


def main() -> int:
    args = parse_args()
    try:
        result = execute(args)
    except (OSError, SafetyError, ValueError) as error:
        print(f"GUARDED SLOW-READER FAILURE: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
