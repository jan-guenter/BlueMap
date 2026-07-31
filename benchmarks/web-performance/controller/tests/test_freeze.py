from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from support import freeze, orchestrate, write_json  # noqa: E402


class TrackedFreezerTests(unittest.TestCase):
    LOAD_GENERATOR_IMAGE = (
        "ghcr.io/jan-guenter/bluemap-perf-loadgen@sha256:" + "a" * 64
    )

    def test_default_output_is_a_canonical_formal_run_root(self) -> None:
        self.assertEqual(
            orchestrate.validate_run_root(freeze.DEFAULT_OUTPUT_DIR),
            freeze.DEFAULT_OUTPUT_DIR.resolve(),
        )
        self.assertEqual(
            freeze.DEFAULT_MANIFEST,
            freeze.DEFAULT_SNAPSHOT_DIR / "manifest.json",
        )
        self.assertNotEqual(
            freeze.DEFAULT_MANIFEST.parent,
            freeze.DEFAULT_OUTPUT_DIR,
        )

    def test_freeze_requires_exact_image_and_never_accepts_source_revision(self) -> None:
        parsed = freeze.parse_args(
            ["validate", "--load-generator-image", self.LOAD_GENERATOR_IMAGE]
        )
        self.assertEqual(parsed.load_generator_image, self.LOAD_GENERATOR_IMAGE)
        for invalid in (
            "ghcr.io/jan-guenter/bluemap-perf-loadgen:latest",
            "ghcr.io/upstream/bluemap-perf-loadgen@sha256:" + "a" * 64,
            "ghcr.io/jan-guenter/bluemap-perf-loadgen@sha256:" + "0" * 64,
            self.LOAD_GENERATOR_IMAGE + "\n",
        ):
            with self.subTest(image=invalid), self.assertRaises(
                orchestrate.SafetyError
            ):
                freeze.load_generator_control(invalid)
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                freeze.parse_args(["validate"])
            with self.assertRaises(SystemExit):
                freeze.parse_args(
                    [
                        "validate",
                        "--load-generator-image",
                        self.LOAD_GENERATOR_IMAGE,
                        "--source-revision",
                        "b" * 40,
                    ]
                )

    def test_revision_is_current_and_plan_mutates_only_allowlisted_candidates(
        self,
    ) -> None:
        current = subprocess.run(
            [
                "git",
                "-C",
                str(freeze.REPOSITORY_ROOT),
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(freeze.REQUIRED_REVISION, current)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                freeze,
                "validate_control_lock",
                return_value=(
                    {
                        name: orchestrate.file_sha256(path)
                        for name, path in freeze.REVIEWED_CONTROLLERS.items()
                    },
                    "a" * 64,
                ),
            ):
                plan = freeze.freeze_plan(
                    root / "manifest.json",
                    root / "output",
                    self.LOAD_GENERATOR_IMAGE,
                )
        self.assertIs(plan["clusterContacted"], False)
        self.assertEqual(
            set(plan["scaleDownExactly"]),
            {
                f"deployment/{deployment}"
                for deployment in orchestrate.FORMAL_DEPLOYMENTS
            },
        )
        self.assertEqual(
            {
                item["deployment"]: item["replicas"]
                for item in plan["activateExactly"]
            },
            {
                f"deployment/{target.deployment}": target.replica_count
                for target in orchestrate.TARGETS.values()
            },
        )
        rendered = json.dumps(plan, sort_keys=True)
        for protected in orchestrate.PROTECTED_RESOURCES:
            self.assertNotIn(protected, json.dumps(plan["activateExactly"]))
            self.assertNotIn(protected, json.dumps(plan["scaleDownExactly"]))
        self.assertEqual(
            plan["loadGenerator"],
            {
                "backend": "runpod-ssh",
                "image": self.LOAD_GENERATOR_IMAGE,
                "imageDigest": "sha256:" + "a" * 64,
                "sourceRevision": freeze.REQUIRED_REVISION,
            },
        )
        self.assertEqual(
            plan["loadGeneratorSha256"],
            hashlib.sha256(
                orchestrate.canonical_json(plan["loadGenerator"])
            ).hexdigest(),
        )
        self.assertIn("bluemap-perf-loadgen", rendered)

    def test_build_frozen_matrix_resolves_every_identity_placeholder(self) -> None:
        template = orchestrate.load_json(freeze.MATRIX_EXAMPLE)
        manifest = {"mapIds": ["world"], "markers": []}
        identities: dict[str, dict[str, object]] = {}
        for variant_index, variant in enumerate(template["variants"], start=1):
            identities[variant["id"]] = {
                "expectedImages": [
                    {
                        "kind": image["kind"],
                        "name": image["name"],
                        "digest": (
                            "sha256:"
                            + format(
                                (variant_index + image_index) % 15 + 1,
                                "x",
                            )
                            * 64
                        ),
                    }
                    for image_index, image in enumerate(
                        variant["expectedImages"],
                        start=1,
                    )
                ],
                "expectedSanitizedConfigSha256": (
                    format(variant_index + 8, "x") * 64
                ),
                "expectedSanitizedRuntimeSpecSha256": (
                    format(variant_index + 1, "x") * 64
                ),
            }
        manifest_bytes = (
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        ).encode()
        matrix = freeze.build_frozen_matrix(
            template,
            manifest,
            identities,
            hashlib.sha256(manifest_bytes).hexdigest(),
        )
        self.assertEqual(matrix["benchmarkGitRevision"], freeze.REQUIRED_REVISION)
        self.assertEqual(matrix["mapIds"], ["world"])
        self.assertEqual(freeze.placeholders(matrix), [])
        self.assertEqual(
            [variant["id"] for variant in matrix["variants"]],
            list(orchestrate.TARGETS),
        )

    def test_controller_lock_binds_all_three_tracked_controls_and_detects_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "controller-lock.json"
            valid = {
                "formatVersion": 1,
                "requiredRevision": freeze.REQUIRED_REVISION,
                "controllers": [
                    {
                        "path": name,
                        "sha256": orchestrate.file_sha256(path),
                    }
                    for name, path in freeze.REVIEWED_CONTROLLERS.items()
                ],
            }
            write_json(lock_path, valid)
            with patch.object(freeze, "CONTROL_LOCK", lock_path):
                hashes, digest = freeze.validate_control_lock()
            self.assertEqual(set(hashes), set(freeze.REVIEWED_CONTROLLERS))
            self.assertEqual(digest, orchestrate.file_sha256(lock_path))

            valid["controllers"][2]["sha256"] = "f" * 64
            write_json(lock_path, valid)
            with (
                patch.object(freeze, "CONTROL_LOCK", lock_path),
                self.assertRaisesRegex(
                    orchestrate.SafetyError,
                    "analyze.py changed",
                ),
            ):
                freeze.validate_control_lock()


if __name__ == "__main__":
    unittest.main()
