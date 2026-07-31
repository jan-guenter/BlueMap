from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
SOURCE_REVISION = "b" * 40


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
    def test_source_revision_requires_a_full_lowercase_git_sha(self) -> None:
        self.assertEqual(
            manage_runpod_loadgen.validate_source_revision(SOURCE_REVISION),
            SOURCE_REVISION,
        )
        for invalid in (
            "",
            "0" * 40,
            "b" * 39,
            "b" * 39 + "\n",
            "b" * 41,
            "b" * 40 + "\n",
            "B" * 40,
            "g" * 40,
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                manage_runpod_loadgen.ProvisioningError,
                "nonzero, full lowercase 40-character Git SHA",
            ):
                manage_runpod_loadgen.validate_source_revision(invalid)

    def test_image_reference_is_restricted_to_the_formal_repository(self) -> None:
        self.assertEqual(
            manage_runpod_loadgen.validate_image(IMAGE),
            (IMAGE, "sha256:" + "a" * 64),
        )
        for invalid in (
            IMAGE.replace("jan-guenter", "another-owner"),
            IMAGE.replace("@sha256:", ":latest@sha256:"),
            IMAGE.replace("@sha256:", ":sha-test"),
            IMAGE + "\n",
            IMAGE.replace("a" * 64, "0" * 64),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                manage_runpod_loadgen.ProvisioningError
            ):
                manage_runpod_loadgen.validate_image(invalid)

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
        self.assertNotIn("BLUEMAP_SOURCE_REVISION", payload["env"])

    def test_runtime_source_revision_marker_is_rejected(self) -> None:
        pod = documented_ready_pod()
        pod["env"]["BLUEMAP_SOURCE_REVISION"] = SOURCE_REVISION

        with self.assertRaisesRegex(
            manage_runpod_loadgen.ProvisioningError,
            "must come from the baked image",
        ):
            manage_runpod_loadgen.assert_expected_pod(
                pod,
                pod_id=POD_ID,
                run_id=RUN_ID,
                image=IMAGE,
                data_center=DATA_CENTER,
                require_ready=True,
            )

    def test_frozen_identity_carries_the_expected_source_revision(self) -> None:
        identity = manage_runpod_loadgen.identity_from_pod(
            documented_ready_pod(),
            run_id=RUN_ID,
            image=IMAGE,
            image_digest="sha256:" + "a" * 64,
            source_revision=SOURCE_REVISION,
            data_center=DATA_CENTER,
            host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey",
            host_key_fingerprint="SHA256:test-fingerprint",
        )

        self.assertEqual(identity["sourceRevision"], SOURCE_REVISION)

    def test_create_persists_and_propagates_source_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_key = root / "id_ed25519"
            public_key = root / "id_ed25519.pub"
            output_dir = root / "state"
            private_key.write_text("test-private-key\n", encoding="utf-8")
            private_key.chmod(0o600)
            public_key.write_text("test-public-key\n", encoding="utf-8")
            args = SimpleNamespace(
                confirm_create=RUN_ID,
                data_center=DATA_CENTER,
                image=IMAGE,
                output_dir=output_dir,
                run_id=RUN_ID,
                source_revision=SOURCE_REVISION,
                ssh_private_key=private_key,
                ssh_public_key=public_key,
                ssh_wait_seconds=1,
                wait_seconds=1,
            )
            captured: dict[str, object] = {}

            def fake_capture(api_key: str, **kwargs: object) -> dict[str, object]:
                captured.update(kwargs)
                return {"status": "ready"}

            with (
                mock.patch.object(
                    manage_runpod_loadgen,
                    "load_public_key",
                    return_value=(
                        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey"
                    ),
                ),
                mock.patch.object(
                    manage_runpod_loadgen,
                    "consume_api_key",
                    return_value="test-api-key",
                ),
                mock.patch.object(
                    manage_runpod_loadgen,
                    "api_request",
                    return_value=(201, {"id": POD_ID}),
                ),
                mock.patch.object(
                    manage_runpod_loadgen,
                    "capture_ready_identity",
                    side_effect=fake_capture,
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(manage_runpod_loadgen.command_create(args), 0)

            state = manage_runpod_loadgen.read_json(
                output_dir / "pod-state.json"
            )
            self.assertEqual(state["sourceRevision"], SOURCE_REVISION)
            self.assertEqual(captured["source_revision"], SOURCE_REVISION)

    def test_recover_uses_only_the_persisted_source_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            private_key = output_dir / "id_ed25519"
            private_key.write_text("test-private-key\n", encoding="utf-8")
            private_key.chmod(0o600)
            manage_runpod_loadgen.write_json_atomic(
                output_dir / "pod-state.json",
                {
                    "formatVersion": 1,
                    "image": IMAGE,
                    "podId": POD_ID,
                    "requestedDataCenterId": DATA_CENTER,
                    "runId": RUN_ID,
                    "sourceRevision": SOURCE_REVISION,
                    "status": "identity-capture-failed",
                },
            )
            args = SimpleNamespace(
                confirm_recover=POD_ID,
                output_dir=output_dir,
                ssh_private_key=private_key,
                ssh_wait_seconds=1,
                wait_seconds=1,
            )
            captured: dict[str, object] = {}

            def fake_capture(api_key: str, **kwargs: object) -> dict[str, object]:
                captured.update(kwargs)
                return {"status": "ready"}

            with (
                mock.patch.object(
                    manage_runpod_loadgen,
                    "consume_api_key",
                    return_value="test-api-key",
                ),
                mock.patch.object(
                    manage_runpod_loadgen,
                    "capture_ready_identity",
                    side_effect=fake_capture,
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(manage_runpod_loadgen.command_recover(args), 0)

            self.assertEqual(captured["source_revision"], SOURCE_REVISION)


if __name__ == "__main__":
    unittest.main()
