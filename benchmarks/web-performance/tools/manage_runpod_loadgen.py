#!/usr/bin/env python3
"""Provision and verify the fixed RunPod source used by formal benchmarks.

The RunPod API key is accepted only through RUNPOD_API_KEY. It is removed from
the child-process environment before any SSH utility is started and is never
written to disk or included in an error message.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


API_BASE = "https://rest.runpod.io/v1"
CPU_FLAVOR = "cpu5c"
VCPU_COUNT = 8
MIN_DOWNLOAD_MBPS = 500
MIN_UPLOAD_MBPS = 100
CONTAINER_DISK_GB = 10
ALLOWED_DATA_CENTERS = frozenset(
    {"EU-NL-1", "EU-FR-1", "EU-CZ-1", "EU-SE-1", "EU-RO-1"}
)
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
POD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,191}$")
IMAGE_RE = re.compile(
    r"^ghcr\.io/[a-z0-9][a-z0-9._-]*/"
    r"bluemap-perf-loadgen@(?P<digest>sha256:[a-f0-9]{64})$"
)
PUBLIC_KEY_RE = re.compile(
    r"^ssh-ed25519 (?P<key>[A-Za-z0-9+/]+={0,3})(?: [^\r\n]+)?$"
)


class ProvisioningError(RuntimeError):
    """Fail-closed lifecycle error."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the bearer credential to a redirected origin."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json_atomic(path: Path, value: Any, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise ProvisioningError(f"refusing to replace symlink: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ProvisioningError(f"not a regular non-symlink JSON file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProvisioningError(f"could not read JSON file {path}: {error}") from error
    if not isinstance(value, dict):
        raise ProvisioningError(f"JSON file does not contain an object: {path}")
    return value


def require_program(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ProvisioningError(f"required executable is unavailable: {name}")
    return path


def validate_output_directory(path: Path, *, create: bool) -> Path:
    path = path.absolute()
    if path.is_symlink():
        raise ProvisioningError(f"output directory must not be a symlink: {path}")
    if create:
        if path.exists() and any(path.iterdir()):
            raise ProvisioningError(f"output directory is not empty: {path}")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    elif not path.is_dir():
        raise ProvisioningError(f"output directory does not exist: {path}")
    return path


def validate_run_id(value: str) -> str:
    if RUN_ID_RE.fullmatch(value) is None:
        raise ProvisioningError(
            "run ID must contain 1-63 lowercase letters, digits, or hyphens"
        )
    return value


def validate_image(value: str) -> tuple[str, str]:
    match = IMAGE_RE.fullmatch(value)
    if match is None:
        raise ProvisioningError(
            "image must be an immutable "
            "ghcr.io/<owner>/bluemap-perf-loadgen@sha256:<digest> reference"
        )
    return value, match.group("digest")


def validate_data_center(value: str) -> str:
    if value not in ALLOWED_DATA_CENTERS:
        allowed = ", ".join(sorted(ALLOWED_DATA_CENTERS))
        raise ProvisioningError(f"data center must be one of: {allowed}")
    return value


def validate_private_key(path: Path) -> Path:
    path = path.absolute()
    if not path.is_file() or path.is_symlink():
        raise ProvisioningError("SSH private key must be a regular non-symlink file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ProvisioningError("SSH private key must not be group/world accessible")
    return path


def load_public_key(public_path: Path, private_path: Path) -> str:
    if not public_path.is_file() or public_path.is_symlink():
        raise ProvisioningError("SSH public key must be a regular non-symlink file")
    public_key = public_path.read_text(encoding="utf-8").strip()
    match = PUBLIC_KEY_RE.fullmatch(public_key)
    if match is None:
        raise ProvisioningError("SSH public key must contain one Ed25519 public key")

    ssh_keygen = require_program("ssh-keygen")
    completed = subprocess.run(
        [ssh_keygen, "-y", "-f", str(private_path)],
        check=False,
        capture_output=True,
        text=True,
        env=child_environment(),
        timeout=15,
    )
    if completed.returncode != 0:
        raise ProvisioningError("could not derive the SSH public key")
    derived = " ".join(completed.stdout.strip().split()[:2])
    supplied_without_comment = " ".join(public_key.split()[:2])
    if derived != supplied_without_comment:
        raise ProvisioningError("SSH public and private keys do not form a pair")
    return public_key


def child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("RUNPOD_API_KEY", None)
    return environment


def consume_api_key() -> str:
    api_key = os.environ.pop("RUNPOD_API_KEY", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", api_key):
        raise ProvisioningError(
            "RUNPOD_API_KEY is missing or has an unexpected format"
        )
    return api_key


def api_request(
    api_key: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    expected_statuses: frozenset[int],
    timeout: float = 30,
) -> tuple[int, Any | None]:
    if not path.startswith("/") or ".." in path:
        raise ProvisioningError("unsafe RunPod API path")
    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "BlueMap-RunPod-Provisioner/1",
    }
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{API_BASE}{path}", data=body, headers=headers, method=method
    )
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            response_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        response_body = error.read()
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise ProvisioningError(
            f"RunPod API {method} {path} failed before receiving a response"
        ) from error

    if status not in expected_statuses:
        # The body can echo submitted environment data. Do not include it.
        raise ProvisioningError(
            f"RunPod API {method} {path} returned unexpected HTTP {status}"
        )
    if not response_body:
        return status, None
    try:
        value = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise ProvisioningError(
            f"RunPod API {method} {path} returned invalid JSON"
        ) from error
    return status, value


def build_payload(
    run_id: str, image: str, data_center: str, public_key: str
) -> dict[str, Any]:
    image_digest = validate_image(image)[1]
    return {
        "cloudType": "SECURE",
        "computeType": "CPU",
        "containerDiskInGb": CONTAINER_DISK_GB,
        "cpuFlavorIds": [CPU_FLAVOR],
        "cpuFlavorPriority": "custom",
        "dataCenterIds": [data_center],
        "dataCenterPriority": "custom",
        "dockerEntrypoint": [],
        "dockerStartCmd": [],
        "env": {
            "BLUEMAP_RUNPOD_CPU_FLAVOR": CPU_FLAVOR,
            "BLUEMAP_RUNPOD_IMAGE_DIGEST": image_digest,
            "BLUEMAP_RUNPOD_RUN_ID": run_id,
            "BLUEMAP_RUNPOD_VCPU_COUNT": str(VCPU_COUNT),
            "BLUEMAP_RUNPOD_SSH_PUBLIC_KEY": public_key,
        },
        "globalNetworking": False,
        "imageName": image,
        "interruptible": False,
        "locked": False,
        "minDownloadMbps": MIN_DOWNLOAD_MBPS,
        "minUploadMbps": MIN_UPLOAD_MBPS,
        "name": f"bluemap-formal-{run_id}",
        "ports": ["22/tcp"],
        "supportPublicIp": True,
        "vcpuCount": VCPU_COUNT,
        "volumeInGb": 0,
    }


def string_field(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProvisioningError(f"RunPod response field is missing: {name}")
    return value


def number_field(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProvisioningError(f"RunPod response field is not numeric: {name}")
    return float(value)


def pod_id_from_response(pod: Any) -> str:
    if not isinstance(pod, dict):
        raise ProvisioningError("RunPod create response is not an object")
    pod_id = string_field(pod.get("id"), "id")
    if POD_ID_RE.fullmatch(pod_id) is None:
        raise ProvisioningError("RunPod returned an invalid Pod ID")
    return pod_id


def pod_image_from_response(
    pod: dict[str, Any], *, required: bool
) -> str | None:
    """Normalize the two image field names returned by RunPod's v1 API."""

    image = pod.get("image")
    image_name = pod.get("imageName")
    for field_name, value in (("image", image), ("imageName", image_name)):
        if value is not None and (not isinstance(value, str) or not value):
            raise ProvisioningError(
                f"RunPod returned an invalid Pod {field_name} field"
            )
    if image is not None and image_name is not None and image != image_name:
        raise ProvisioningError("RunPod returned conflicting Pod image fields")
    result = image if image is not None else image_name
    if required and result is None:
        raise ProvisioningError("RunPod response field is missing: image")
    return result


def assert_expected_pod(
    pod: dict[str, Any],
    *,
    pod_id: str,
    run_id: str,
    image: str,
    data_center: str,
    require_ready: bool,
) -> None:
    if pod_id_from_response(pod) != pod_id:
        raise ProvisioningError("RunPod API returned a different Pod ID")
    expected_name = f"bluemap-formal-{run_id}"
    if pod.get("name") not in (None, expected_name):
        raise ProvisioningError("RunPod Pod name changed")

    checks = {
        "image": (pod_image_from_response(pod, required=False), image),
        "cpuFlavorId": (pod.get("cpuFlavorId"), CPU_FLAVOR),
        "vcpuCount": (pod.get("vcpuCount"), VCPU_COUNT),
        "machine.dataCenterId": (
            (pod.get("machine") or {}).get("dataCenterId"),
            data_center,
        ),
    }
    for name, (actual, expected) in checks.items():
        if actual is not None and actual != expected:
            raise ProvisioningError(
                f"RunPod immutable field {name} differs from the request"
            )

    environment = pod.get("env")
    if isinstance(environment, dict) and environment.get(
        "BLUEMAP_RUNPOD_RUN_ID"
    ) not in (None, run_id):
        raise ProvisioningError("RunPod Pod run-ID marker changed")

    if not require_ready:
        return
    if pod.get("desiredStatus") != "RUNNING":
        raise ProvisioningError("RunPod Pod is not in RUNNING desired state")
    # CPU Pods currently serialize this v1 field as null even when the create
    # request explicitly set false. Reject true and every non-boolean value
    # other than that documented backend representation.
    interruptible = pod.get("interruptible")
    if interruptible is not None and interruptible is not False:
        raise ProvisioningError("RunPod Pod is unexpectedly interruptible")
    machine = pod.get("machine")
    if not isinstance(machine, dict):
        raise ProvisioningError("RunPod machine identity is unavailable")
    if machine.get("secureCloud") is not True:
        raise ProvisioningError("RunPod Pod is not on Secure Cloud")
    if machine.get("dataCenterId") != data_center:
        raise ProvisioningError("RunPod data-center placement differs from request")
    if pod_image_from_response(pod, required=True) != image:
        raise ProvisioningError("RunPod image differs from immutable request")
    if pod.get("cpuFlavorId") != CPU_FLAVOR or pod.get("vcpuCount") != VCPU_COUNT:
        raise ProvisioningError("RunPod CPU allocation differs from request")
    if number_field(
        machine.get("maxDownloadSpeedMbps"), "machine.maxDownloadSpeedMbps"
    ) < MIN_DOWNLOAD_MBPS:
        raise ProvisioningError("RunPod download capacity is below the requested floor")
    if number_field(
        machine.get("maxUploadSpeedMbps"), "machine.maxUploadSpeedMbps"
    ) < MIN_UPLOAD_MBPS:
        raise ProvisioningError("RunPod upload capacity is below the requested floor")
    ipaddress.ip_address(string_field(pod.get("publicIp"), "publicIp"))
    mappings = pod.get("portMappings")
    if not isinstance(mappings, dict):
        raise ProvisioningError("RunPod TCP port mapping is unavailable")
    port = mappings.get("22")
    if isinstance(port, bool) or not isinstance(port, (int, float)):
        raise ProvisioningError("RunPod SSH port mapping is unavailable")
    if int(port) != port or not 1 <= int(port) <= 65535:
        raise ProvisioningError("RunPod SSH port mapping is invalid")
    string_field(pod.get("machineId"), "machineId")
    if not isinstance(environment, dict):
        raise ProvisioningError("RunPod environment identity is unavailable")
    expected_environment = {
        "BLUEMAP_RUNPOD_CPU_FLAVOR": CPU_FLAVOR,
        "BLUEMAP_RUNPOD_IMAGE_DIGEST": validate_image(image)[1],
        "BLUEMAP_RUNPOD_RUN_ID": run_id,
        "BLUEMAP_RUNPOD_VCPU_COUNT": str(VCPU_COUNT),
    }
    for key, expected in expected_environment.items():
        if environment.get(key) != expected:
            raise ProvisioningError(f"RunPod environment marker changed: {key}")


def ready_pod(
    api_key: str,
    *,
    pod_id: str,
    run_id: str,
    image: str,
    data_center: str,
    deadline: float,
    state_path: Path,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        _, value = api_request(
            api_key,
            "GET",
            f"/pods/{pod_id}?includeMachine=true",
            expected_statuses=frozenset({200}),
        )
        if not isinstance(value, dict):
            raise ProvisioningError("RunPod get-Pod response is not an object")
        assert_expected_pod(
            value,
            pod_id=pod_id,
            run_id=run_id,
            image=image,
            data_center=data_center,
            require_ready=False,
        )
        state = read_json(state_path)
        state["lastObservedAt"] = utc_now()
        state["lastDesiredStatus"] = value.get("desiredStatus")
        state["machineId"] = value.get("machineId")
        write_json_atomic(state_path, state)
        try:
            assert_expected_pod(
                value,
                pod_id=pod_id,
                run_id=run_id,
                image=image,
                data_center=data_center,
                require_ready=True,
            )
        except ProvisioningError:
            if value.get("desiredStatus") in {"EXITED", "TERMINATED"}:
                raise
            time.sleep(5)
            continue
        return value
    raise ProvisioningError("timed out waiting for the RunPod Pod to become ready")


def scan_host_key(host: str, port: int, deadline: float) -> tuple[str, str]:
    ssh_keyscan = require_program("ssh-keyscan")
    consecutive: list[str] = []
    while time.monotonic() < deadline:
        completed = subprocess.run(
            [
                ssh_keyscan,
                "-T",
                "10",
                "-p",
                str(port),
                "-t",
                "ed25519",
                host,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=child_environment(),
            timeout=15,
        )
        keys: set[str] = set()
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[1] == "ssh-ed25519":
                keys.add(f"{fields[1]} {fields[2]}")
        if len(keys) == 1:
            current = next(iter(keys))
            consecutive.append(current)
            consecutive = consecutive[-3:]
            if len(consecutive) == 3 and len(set(consecutive)) == 1:
                encoded = current.split()[1]
                try:
                    blob = base64.b64decode(encoded, validate=True)
                except ValueError as error:
                    raise ProvisioningError(
                        "RunPod SSH host key is not valid base64"
                    ) from error
                fingerprint = base64.b64encode(hashlib.sha256(blob).digest()).decode()
                return current, f"SHA256:{fingerprint.rstrip('=')}"
        else:
            consecutive.clear()
        time.sleep(2)
    raise ProvisioningError("timed out capturing a stable Ed25519 SSH host key")


def identity_from_pod(
    pod: dict[str, Any],
    *,
    run_id: str,
    image: str,
    image_digest: str,
    data_center: str,
    host_key: str,
    host_key_fingerprint: str,
) -> dict[str, Any]:
    machine = pod["machine"]
    port = int(pod["portMappings"]["22"])
    cost = pod.get("adjustedCostPerHr", pod.get("costPerHr"))
    return {
        "backend": "runpod-ssh",
        "capturedAt": utc_now(),
        "formatVersion": 1,
        "remoteRoot": "/artifacts",
        "runId": run_id,
        "runpod": {
            "costPerHour": float(cost) if cost is not None else None,
            "cpuFlavorId": CPU_FLAVOR,
            "dataCenterId": data_center,
            "image": image,
            "imageDigest": image_digest,
            "machineId": string_field(pod.get("machineId"), "machineId"),
            "maxDownloadMbps": number_field(
                machine.get("maxDownloadSpeedMbps"),
                "machine.maxDownloadSpeedMbps",
            ),
            "maxUploadMbps": number_field(
                machine.get("maxUploadSpeedMbps"), "machine.maxUploadSpeedMbps"
            ),
            "minDownloadMbps": MIN_DOWNLOAD_MBPS,
            "minUploadMbps": MIN_UPLOAD_MBPS,
            "podId": pod_id_from_response(pod),
            "publicIp": string_field(pod.get("publicIp"), "publicIp"),
            "secureCloud": True,
            "vcpuCount": VCPU_COUNT,
        },
        "ssh": {
            "host": string_field(pod.get("publicIp"), "publicIp"),
            "hostKey": host_key,
            "hostKeyFingerprint": host_key_fingerprint,
            "port": port,
            "user": "loadgen",
        },
    }


def validate_live_identity(
    identity_path: Path, private_key: Path
) -> dict[str, Any]:
    helper = Path(__file__).with_name("runpod_loadgen.sh")
    if not helper.is_file() or helper.is_symlink():
        raise ProvisioningError(f"RunPod SSH helper is unavailable: {helper}")
    completed = subprocess.run(
        [
            str(helper),
            "--identity",
            str(identity_path),
            "--identity-key",
            str(private_key),
            "validate",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=child_environment(),
        timeout=45,
    )
    if completed.returncode != 0:
        raise ProvisioningError(
            "SSH connection or live RunPod identity validation failed"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ProvisioningError("live RunPod identity is not valid JSON") from error
    if not isinstance(value, dict):
        raise ProvisioningError("live RunPod identity is not an object")
    return value


def identity_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def capture_ready_identity(
    api_key: str,
    *,
    run_id: str,
    image: str,
    image_digest: str,
    data_center: str,
    private_key: Path,
    output_dir: Path,
    wait_seconds: int,
    ssh_wait_seconds: int,
) -> dict[str, Any]:
    state_path = output_dir / "pod-state.json"
    identity_path = output_dir / "frozen-identity.json"
    live_identity_path = output_dir / "live-identity-before.json"
    candidate_identity_path = output_dir / "frozen-identity.candidate.json"
    candidate_live_identity_path = output_dir / "live-identity-before.candidate.json"
    if identity_path.exists() or live_identity_path.exists():
        raise ProvisioningError("refusing to overwrite existing RunPod identity files")
    state = read_json(state_path)
    pod_id = string_field(state.get("podId"), "pod-state.podId")

    ready = ready_pod(
        api_key,
        pod_id=pod_id,
        run_id=run_id,
        image=image,
        data_center=data_center,
        deadline=time.monotonic() + wait_seconds,
        state_path=state_path,
    )
    host = string_field(ready.get("publicIp"), "publicIp")
    port = int(ready["portMappings"]["22"])
    host_key, host_key_fingerprint = scan_host_key(
        host, port, time.monotonic() + ssh_wait_seconds
    )
    identity = identity_from_pod(
        ready,
        run_id=run_id,
        image=image,
        image_digest=image_digest,
        data_center=data_center,
        host_key=host_key,
        host_key_fingerprint=host_key_fingerprint,
    )
    write_json_atomic(candidate_identity_path, identity)
    live_identity = validate_live_identity(candidate_identity_path, private_key)
    write_json_atomic(candidate_live_identity_path, live_identity)
    os.replace(candidate_identity_path, identity_path)
    os.replace(candidate_live_identity_path, live_identity_path)

    state = read_json(state_path)
    state.update(
        {
            "identityFile": identity_path.name,
            "identitySha256": identity_digest(identity_path),
            "readyAt": utc_now(),
            "status": "ready",
        }
    )
    write_json_atomic(state_path, state)
    return {
        "frozenIdentity": str(identity_path),
        "hostKeyFingerprint": host_key_fingerprint,
        "liveIdentity": str(live_identity_path),
        "podId": pod_id,
        "runId": run_id,
        "status": "ready",
    }


def command_plan(args: argparse.Namespace) -> int:
    run_id = validate_run_id(args.run_id)
    image, _ = validate_image(args.image)
    data_center = validate_data_center(args.data_center)
    private_key = validate_private_key(args.ssh_private_key)
    public_key = load_public_key(args.ssh_public_key, private_key)
    json.dump(
        build_payload(run_id, image, data_center, public_key),
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


def command_create(args: argparse.Namespace) -> int:
    run_id = validate_run_id(args.run_id)
    if args.confirm_create != run_id:
        raise ProvisioningError("--confirm-create must exactly equal --run-id")
    image, image_digest = validate_image(args.image)
    data_center = validate_data_center(args.data_center)
    private_key = validate_private_key(args.ssh_private_key)
    public_key = load_public_key(args.ssh_public_key, private_key)
    output_dir = validate_output_directory(args.output_dir, create=True)
    state_path = output_dir / "pod-state.json"
    identity_path = output_dir / "frozen-identity.json"
    live_identity_path = output_dir / "live-identity-before.json"
    api_key = consume_api_key()
    created_pod_id: str | None = None

    try:
        _, created = api_request(
            api_key,
            "POST",
            "/pods",
            payload=build_payload(run_id, image, data_center, public_key),
            expected_statuses=frozenset({201}),
        )
        created_pod_id = pod_id_from_response(created)
        state = {
            "createdAt": utc_now(),
            "formatVersion": 1,
            "image": image,
            "podId": created_pod_id,
            "requestedDataCenterId": data_center,
            "runId": run_id,
            "status": "provisioning",
        }
        write_json_atomic(state_path, state)
        print(
            f"RunPod Pod created: {created_pod_id}; "
            f"recovery state: {state_path}",
            file=sys.stderr,
        )

        result = capture_ready_identity(
            api_key,
            run_id=run_id,
            image=image,
            image_digest=image_digest,
            data_center=data_center,
            private_key=private_key,
            output_dir=output_dir,
            wait_seconds=args.wait_seconds,
            ssh_wait_seconds=args.ssh_wait_seconds,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except BaseException:
        if created_pod_id is not None:
            try:
                state = read_json(state_path)
                state["failedAt"] = utc_now()
                state["status"] = "identity-capture-failed"
                write_json_atomic(state_path, state)
            except Exception:
                pass
            print(
                "A Pod was created and was not deleted automatically. "
                "Run the explicit delete command with Pod ID "
                f"{created_pod_id}.",
                file=sys.stderr,
            )
        raise
    finally:
        api_key = ""


def command_recover(args: argparse.Namespace) -> int:
    output_dir = validate_output_directory(args.output_dir, create=False)
    private_key = validate_private_key(args.ssh_private_key)
    state_path = output_dir / "pod-state.json"
    state = read_json(state_path)
    if state.get("formatVersion") != 1:
        raise ProvisioningError("RunPod recovery state has an unknown format")
    if state.get("status") != "identity-capture-failed":
        raise ProvisioningError("RunPod state is not eligible for identity recovery")
    pod_id = string_field(state.get("podId"), "pod-state.podId")
    if POD_ID_RE.fullmatch(pod_id) is None:
        raise ProvisioningError("RunPod recovery state has an invalid Pod ID")
    if args.confirm_recover != pod_id:
        raise ProvisioningError(
            "--confirm-recover must exactly equal the frozen Pod ID"
        )
    run_id = validate_run_id(string_field(state.get("runId"), "pod-state.runId"))
    image, image_digest = validate_image(
        string_field(state.get("image"), "pod-state.image")
    )
    data_center = validate_data_center(
        string_field(
            state.get("requestedDataCenterId"),
            "pod-state.requestedDataCenterId",
        )
    )
    if (output_dir / "deletion.json").exists():
        raise ProvisioningError("refusing to recover a Pod with deletion evidence")
    api_key = consume_api_key()
    try:
        state["recoveryStartedAt"] = utc_now()
        state["status"] = "recovering"
        write_json_atomic(state_path, state)
        result = capture_ready_identity(
            api_key,
            run_id=run_id,
            image=image,
            image_digest=image_digest,
            data_center=data_center,
            private_key=private_key,
            output_dir=output_dir,
            wait_seconds=args.wait_seconds,
            ssh_wait_seconds=args.ssh_wait_seconds,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except BaseException:
        try:
            state = read_json(state_path)
            state["failedAt"] = utc_now()
            state["status"] = "identity-capture-failed"
            write_json_atomic(state_path, state)
        except Exception:
            pass
        print(
            "RunPod identity recovery failed; the Pod was not deleted. "
            f"Exact Pod ID: {pod_id}.",
            file=sys.stderr,
        )
        raise
    finally:
        api_key = ""


def load_ready_identity(output_dir: Path) -> tuple[dict[str, Any], Path]:
    identity_path = output_dir / "frozen-identity.json"
    identity = read_json(identity_path)
    if (
        identity.get("formatVersion") != 1
        or identity.get("backend") != "runpod-ssh"
        or identity.get("runpod", {}).get("cpuFlavorId") != CPU_FLAVOR
        or identity.get("runpod", {}).get("vcpuCount") != VCPU_COUNT
        or identity.get("runpod", {}).get("minDownloadMbps")
        != MIN_DOWNLOAD_MBPS
        or identity.get("runpod", {}).get("minUploadMbps") != MIN_UPLOAD_MBPS
        or number_field(
            identity.get("runpod", {}).get("maxDownloadMbps"),
            "runpod.maxDownloadMbps",
        )
        < MIN_DOWNLOAD_MBPS
        or number_field(
            identity.get("runpod", {}).get("maxUploadMbps"),
            "runpod.maxUploadMbps",
        )
        < MIN_UPLOAD_MBPS
        or identity.get("runpod", {}).get("secureCloud") is not True
    ):
        raise ProvisioningError("frozen RunPod identity is invalid")
    return identity, identity_path


def verify_api_identity(api_key: str, identity: dict[str, Any]) -> dict[str, Any]:
    run_id = validate_run_id(string_field(identity.get("runId"), "runId"))
    runpod = identity["runpod"]
    pod_id = string_field(runpod.get("podId"), "runpod.podId")
    image, _ = validate_image(string_field(runpod.get("image"), "runpod.image"))
    data_center = validate_data_center(
        string_field(runpod.get("dataCenterId"), "runpod.dataCenterId")
    )
    _, pod = api_request(
        api_key,
        "GET",
        f"/pods/{pod_id}?includeMachine=true",
        expected_statuses=frozenset({200}),
    )
    if not isinstance(pod, dict):
        raise ProvisioningError("RunPod get-Pod response is not an object")
    assert_expected_pod(
        pod,
        pod_id=pod_id,
        run_id=run_id,
        image=image,
        data_center=data_center,
        require_ready=True,
    )
    if pod.get("machineId") != runpod.get("machineId"):
        raise ProvisioningError("RunPod machine identity changed")
    if pod.get("publicIp") != runpod.get("publicIp"):
        raise ProvisioningError("RunPod public IP changed")
    if int(pod["portMappings"]["22"]) != identity["ssh"].get("port"):
        raise ProvisioningError("RunPod SSH port mapping changed")
    return pod


def command_verify(args: argparse.Namespace) -> int:
    output_dir = validate_output_directory(args.output_dir, create=False)
    private_key = validate_private_key(args.ssh_private_key)
    identity, identity_path = load_ready_identity(output_dir)
    api_key = consume_api_key()
    try:
        verify_api_identity(api_key, identity)
        live = validate_live_identity(identity_path, private_key)
    finally:
        api_key = ""
    print(json.dumps(live, indent=2, sort_keys=True))
    return 0


def deletion_target(output_dir: Path) -> tuple[str, str, str, dict[str, Any] | None]:
    state = read_json(output_dir / "pod-state.json")
    pod_id = string_field(state.get("podId"), "pod-state.podId")
    run_id = validate_run_id(string_field(state.get("runId"), "pod-state.runId"))
    image = string_field(state.get("image"), "pod-state.image")
    identity_path = output_dir / "frozen-identity.json"
    identity = read_json(identity_path) if identity_path.exists() else None
    if identity is not None and identity.get("runpod", {}).get("podId") != pod_id:
        raise ProvisioningError("state and frozen identity refer to different Pods")
    return pod_id, run_id, image, identity


def command_delete(args: argparse.Namespace) -> int:
    output_dir = validate_output_directory(args.output_dir, create=False)
    pod_id, run_id, image, identity = deletion_target(output_dir)
    if args.confirm_delete != pod_id:
        raise ProvisioningError("--confirm-delete must exactly equal the frozen Pod ID")
    api_key = consume_api_key()
    deletion_path = output_dir / "deletion.json"
    try:
        status, pod = api_request(
            api_key,
            "GET",
            f"/pods/{pod_id}",
            expected_statuses=frozenset({200, 404}),
        )
        if status == 404:
            deletion = {
                "confirmedAbsentAt": utc_now(),
                "formatVersion": 1,
                "podId": pod_id,
                "runId": run_id,
                "status": "already-absent",
            }
            write_json_atomic(deletion_path, deletion)
            print(json.dumps(deletion, indent=2, sort_keys=True))
            return 0
        if not isinstance(pod, dict) or pod_id_from_response(pod) != pod_id:
            raise ProvisioningError("RunPod deletion target identity is invalid")
        if pod.get("name") != f"bluemap-formal-{run_id}":
            raise ProvisioningError("RunPod deletion target name differs from state")
        environment = pod.get("env")
        if (
            not isinstance(environment, dict)
            or environment.get("BLUEMAP_RUNPOD_RUN_ID") != run_id
        ):
            raise ProvisioningError("RunPod deletion target run-ID marker differs")
        if pod_image_from_response(pod, required=True) != image:
            raise ProvisioningError("RunPod deletion target image differs from state")
        if identity is not None and identity["runpod"].get("image") != image:
            raise ProvisioningError("frozen identity image differs from state")

        api_request(
            api_key,
            "DELETE",
            f"/pods/{pod_id}",
            expected_statuses=frozenset({204}),
        )
        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            observed_status, _ = api_request(
                api_key,
                "GET",
                f"/pods/{pod_id}",
                expected_statuses=frozenset({200, 404}),
            )
            if observed_status == 404:
                break
            time.sleep(3)
        else:
            raise ProvisioningError(
                "RunPod accepted deletion but the Pod is still returned by the API"
            )
        deletion = {
            "deletedAt": utc_now(),
            "formatVersion": 1,
            "identitySha256": (
                identity_digest(output_dir / "frozen-identity.json")
                if identity is not None
                else None
            ),
            "podId": pod_id,
            "runId": run_id,
            "status": "deleted",
        }
        write_json_atomic(deletion_path, deletion)
        print(json.dumps(deletion, indent=2, sort_keys=True))
        return 0
    finally:
        api_key = ""


def common_launch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--data-center",
        default="EU-NL-1",
        help="one fixed EU data center (default: EU-NL-1)",
    )
    parser.add_argument("--ssh-public-key", required=True, type=Path)
    parser.add_argument("--ssh-private-key", required=True, type=Path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage the fixed external RunPod k6 source"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser(
        "plan", help="print the non-secret v1 create payload without an API call"
    )
    common_launch_arguments(plan)
    plan.set_defaults(handler=command_plan)

    create = subparsers.add_parser(
        "create", help="create, pin, and verify one fixed RunPod CPU Pod"
    )
    common_launch_arguments(create)
    create.add_argument("--output-dir", required=True, type=Path)
    create.add_argument("--confirm-create", required=True)
    create.add_argument("--wait-seconds", type=int, default=900)
    create.add_argument("--ssh-wait-seconds", type=int, default=180)
    create.set_defaults(handler=command_create)

    recover = subparsers.add_parser(
        "recover",
        help="resume identity capture for one explicitly confirmed existing Pod",
    )
    recover.add_argument("--output-dir", required=True, type=Path)
    recover.add_argument("--ssh-private-key", required=True, type=Path)
    recover.add_argument("--confirm-recover", required=True)
    recover.add_argument("--wait-seconds", type=int, default=900)
    recover.add_argument("--ssh-wait-seconds", type=int, default=180)
    recover.set_defaults(handler=command_recover)

    verify = subparsers.add_parser(
        "verify", help="recheck API, machine, network, and pinned SSH identity"
    )
    verify.add_argument("--output-dir", required=True, type=Path)
    verify.add_argument("--ssh-private-key", required=True, type=Path)
    verify.set_defaults(handler=command_verify)

    delete = subparsers.add_parser(
        "delete", help="delete only the exact Pod frozen in the state directory"
    )
    delete.add_argument("--output-dir", required=True, type=Path)
    delete.add_argument("--confirm-delete", required=True)
    delete.add_argument("--wait-seconds", type=int, default=120)
    delete.set_defaults(handler=command_delete)

    args = parser.parse_args()
    for name in ("wait_seconds", "ssh_wait_seconds"):
        value = getattr(args, name, None)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=sys.stderr)
        raise SystemExit(130) from None
    except (
        KeyError,
        OSError,
        ProvisioningError,
        TypeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
