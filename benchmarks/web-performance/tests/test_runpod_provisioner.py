from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


BENCHMARK_ROOT = Path(__file__).parents[1]
PROVISIONER_PATH = BENCHMARK_ROOT / "tools" / "manage_runpod_loadgen.py"
SPEC = importlib.util.spec_from_file_location(
    "manage_runpod_loadgen", PROVISIONER_PATH
)
assert SPEC is not None
assert SPEC.loader is not None
manage_runpod_loadgen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage_runpod_loadgen)


IMAGE = (
    "ghcr.io/jan-guenter/bluemap-perf-loadgen@sha256:"
    + "a" * 64
)
POD_ID = "pod-test-123"
RUN_ID = "formal-test"
DATA_CENTER = "EU-NL-1"


def documented_ready_pod() -> dict[str, object]:
    """Return the relevant fields observed from the current v1 CPU Pod API."""

    return {
        "adjustedCostPerHr": 0.28,
        "cpuFlavorId": "cpu5c",
        "desiredStatus": "RUNNING",
        "env": {
            "BLUEMAP_RUNPOD_CPU_FLAVOR": "cpu5c",
            "BLUEMAP_RUNPOD_IMAGE_DIGEST": "sha256:" + "a" * 64,
            "BLUEMAP_RUNPOD_RUN_ID": RUN_ID,
            "BLUEMAP_RUNPOD_VCPU_COUNT": "8",
        },
        "id": POD_ID,
        "imageName": IMAGE,
        "interruptible": None,
        "machine": {
            "dataCenterId": DATA_CENTER,
            "maxDownloadSpeedMbps": 1000,
            "maxUploadSpeedMbps": 500,
            "secureCloud": True,
        },
        "machineId": "machine-test-123",
        "name": f"bluemap-formal-{RUN_ID}",
        "portMappings": {"22": 23456},
        "publicIp": "192.0.2.10",
        "vcpuCount": 8,
    }


class RunPodProvisionerTests(unittest.TestCase):
    def test_ready_poll_expands_machine_and_accepts_documented_fields(
        self,
    ) -> None:
        observed_paths: list[str] = []

        def fake_request(
            api_key: str,
            method: str,
            path: str,
            **kwargs: object,
        ) -> tuple[int, object]:
            self.assertEqual(api_key, "test-api-key")
            self.assertEqual(method, "GET")
            observed_paths.append(path)
            return 200, documented_ready_pod()

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "pod-state.json"
            manage_runpod_loadgen.write_json_atomic(
                state_path,
                {
                    "formatVersion": 1,
                    "podId": POD_ID,
                    "runId": RUN_ID,
                    "status": "provisioning",
                },
            )
            with mock.patch.object(
                manage_runpod_loadgen, "api_request", fake_request
            ):
                pod = manage_runpod_loadgen.ready_pod(
                    "test-api-key",
                    pod_id=POD_ID,
                    run_id=RUN_ID,
                    image=IMAGE,
                    data_center=DATA_CENTER,
                    deadline=float("inf"),
                    state_path=state_path,
                )

        self.assertEqual(pod["machineId"], "machine-test-123")
        self.assertEqual(
            observed_paths,
            [f"/pods/{POD_ID}?includeMachine=true"],
        )

    def test_verify_expands_machine_and_checks_documented_fields(self) -> None:
        identity = {
            "runId": RUN_ID,
            "runpod": {
                "cpuFlavorId": "cpu5c",
                "dataCenterId": DATA_CENTER,
                "image": IMAGE,
                "imageDigest": "sha256:" + "a" * 64,
                "machineId": "machine-test-123",
                "podId": POD_ID,
                "publicIp": "192.0.2.10",
                "secureCloud": True,
                "vcpuCount": 8,
            },
            "ssh": {"port": 23456},
        }
        observed_paths: list[str] = []

        def fake_request(
            api_key: str,
            method: str,
            path: str,
            **kwargs: object,
        ) -> tuple[int, object]:
            self.assertEqual(api_key, "test-api-key")
            self.assertEqual(method, "GET")
            observed_paths.append(path)
            return 200, documented_ready_pod()

        with mock.patch.object(
            manage_runpod_loadgen, "api_request", fake_request
        ):
            pod = manage_runpod_loadgen.verify_api_identity(
                "test-api-key", identity
            )

        self.assertEqual(
            pod["machine"]["dataCenterId"],
            DATA_CENTER,
        )
        self.assertEqual(
            pod["machine"]["maxDownloadSpeedMbps"],
            1000,
        )
        self.assertEqual(
            pod["machine"]["maxUploadSpeedMbps"],
            500,
        )
        self.assertIs(pod["machine"]["secureCloud"], True)
        self.assertEqual(
            observed_paths,
            [f"/pods/{POD_ID}?includeMachine=true"],
        )

    def test_machine_expansion_is_required_for_ready_identity(self) -> None:
        pod_without_machine = documented_ready_pod()
        del pod_without_machine["machine"]

        with self.assertRaisesRegex(
            manage_runpod_loadgen.ProvisioningError,
            "machine identity is unavailable",
        ):
            manage_runpod_loadgen.assert_expected_pod(
                pod_without_machine,
                pod_id=POD_ID,
                run_id=RUN_ID,
                image=IMAGE,
                data_center=DATA_CENTER,
                require_ready=True,
            )

    def test_conflicting_v1_image_fields_are_rejected(self) -> None:
        pod = documented_ready_pod()
        pod["image"] = IMAGE.replace("a" * 64, "b" * 64)

        with self.assertRaisesRegex(
            manage_runpod_loadgen.ProvisioningError,
            "conflicting Pod image fields",
        ):
            manage_runpod_loadgen.assert_expected_pod(
                pod,
                pod_id=POD_ID,
                run_id=RUN_ID,
                image=IMAGE,
                data_center=DATA_CENTER,
                require_ready=True,
            )

    def test_create_payload_uses_documented_v1_request_names(self) -> None:
        payload = manage_runpod_loadgen.build_payload(
            RUN_ID,
            IMAGE,
            DATA_CENTER,
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey",
        )

        self.assertEqual(payload["cloudType"], "SECURE")
        self.assertEqual(payload["computeType"], "CPU")
        self.assertEqual(payload["cpuFlavorIds"], ["cpu5c"])
        self.assertEqual(payload["vcpuCount"], 8)
        self.assertEqual(payload["dataCenterIds"], [DATA_CENTER])
        self.assertEqual(payload["imageName"], IMAGE)
        self.assertEqual(payload["ports"], ["22/tcp"])
        self.assertEqual(payload["minDownloadMbps"], 500)
        self.assertEqual(payload["minUploadMbps"], 100)
        self.assertIs(payload["interruptible"], False)
        self.assertEqual(
            payload["env"]["BLUEMAP_RUNPOD_SSH_PUBLIC_KEY"],
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey",
        )
        self.assertNotIn("SSH_PUBLIC_KEY", payload["env"])


if __name__ == "__main__":
    unittest.main()
