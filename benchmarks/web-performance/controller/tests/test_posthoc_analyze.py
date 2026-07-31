from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "posthoc_analyze.py"
SPEC = importlib.util.spec_from_file_location("posthoc_analyze_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
POSTHOC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = POSTHOC
SPEC.loader.exec_module(POSTHOC)


class PostHocAnalyzeTest(unittest.TestCase):
    def write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_frozen_binding_checks_all_four_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            run.mkdir()
            frozen = root / "frozen.py"
            active = root / "active.py"
            frozen.write_text("frozen\n", encoding="utf-8")
            active.write_text("corrected\n", encoding="utf-8")
            digest = POSTHOC.sha256_file(frozen)
            bundle = root / "bundle.json"
            lock = root / "lock.json"
            self.write_json(bundle, {"analyzerSha256": digest})
            self.write_json(run / "state.json", {"analyzerSha256": digest})
            self.write_json(
                lock,
                {"controllers": [{"path": "analyze.py", "sha256": digest}]},
            )

            result = POSTHOC.verify_frozen_binding(
                run_root=run,
                bundle_manifest=bundle,
                controller_lock=lock,
                frozen_analyzer=frozen,
                active_analyzer=active,
            )
            self.assertEqual(result["frozenAnalyzer"]["sha256"], digest)
            self.assertNotEqual(result["activeAnalyzer"]["sha256"], digest)

            self.write_json(run / "state.json", {"analyzerSha256": "0" * 64})
            with self.assertRaisesRegex(POSTHOC.PostHocError, "binding mismatch"):
                POSTHOC.verify_frozen_binding(
                    run_root=run,
                    bundle_manifest=bundle,
                    controller_lock=lock,
                    frozen_analyzer=frozen,
                    active_analyzer=active,
                )

    def test_inventory_excludes_analysis_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            (run / "results").mkdir(parents=True)
            (run / "analysis").mkdir()
            (run / "results" / "raw.json").write_text("raw", encoding="utf-8")
            (run / "analysis" / "old-report.json").write_text("derived", encoding="utf-8")
            inventory, summary = POSTHOC.build_raw_inventory(run)
            self.assertEqual(
                [item["path"] for item in inventory["files"]],
                ["results/raw.json"],
            )
            self.assertEqual(summary["fileCount"], 1)
            (run / "link").symlink_to(run / "results" / "raw.json")
            with self.assertRaisesRegex(POSTHOC.PostHocError, "symlink"):
                POSTHOC.build_raw_inventory(run)

    def test_digest_bridge_is_narrow_and_restored(self) -> None:
        seen: list[tuple[str, str]] = []

        def bundle(value: int, analyzer_digest: str) -> tuple[int, str]:
            seen.append(("bundle", analyzer_digest))
            return value, analyzer_digest

        def state(*, analyzer_digest: str, other: str) -> str:
            seen.append(("state", analyzer_digest))
            return other

        module = SimpleNamespace(validate_frozen_bundle=bundle, validate_state=state)
        original_bundle = module.validate_frozen_bundle
        original_state = module.validate_state
        with POSTHOC.bind_frozen_analyzer_digest(module, "a" * 64, "f" * 64):
            self.assertEqual(
                module.validate_frozen_bundle(3, "a" * 64), (3, "f" * 64)
            )
            self.assertEqual(
                module.validate_state(analyzer_digest="a" * 64, other="kept"),
                "kept",
            )
            with self.assertRaisesRegex(POSTHOC.PostHocError, "unexpected self-digest"):
                module.validate_state(analyzer_digest="x" * 64, other="kept")
        self.assertIs(module.validate_frozen_bundle, original_bundle)
        self.assertIs(module.validate_state, original_state)
        self.assertEqual(seen, [("bundle", "f" * 64), ("state", "f" * 64)])

    def test_analysis_failure_is_wrapped_without_a_traceback(self) -> None:
        error = POSTHOC.PostHocError(
            "active analyzer rejected the historical evidence: failed phase"
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(POSTHOC, "parse_args", return_value=SimpleNamespace()),
            mock.patch.object(POSTHOC, "execute", side_effect=error),
            redirect_stderr(stderr),
        ):
            self.assertEqual(POSTHOC.main([]), 1)
        self.assertIn("failed phase", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_execute_wraps_dynamic_analysis_failure_and_rechecks_inventory(self) -> None:
        class CorrectedAnalysisFailure(Exception):
            pass

        def reject(_args: object) -> None:
            raise CorrectedAnalysisFailure("failed phase")

        module = SimpleNamespace(
            AnalysisFailure=CorrectedAnalysisFailure,
            analyze=reject,
            render_markdown=lambda _report: "",
            validate_frozen_bundle=lambda analyzer_digest: analyzer_digest,
            validate_state=lambda *, analyzer_digest: analyzer_digest,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            run.mkdir()
            sentinel = run / "raw.json"
            sentinel.write_text("unchanged", encoding="utf-8")
            bundle = root / "frozen" / "formal-inputs" / "bundle-manifest.json"
            controller_lock = bundle.parent.parent / "controller-lock.json"
            output = root / "posthoc-output"
            active_sha = "a" * 64
            frozen_sha = "f" * 64
            args = SimpleNamespace(
                run_root=run,
                output_dir=output,
                policy_id="failed-phase-aware-v1",
                matrix=bundle.parent / "matrix.json",
                schedule=bundle.parent / "schedule.json",
                runtime_admission_identities=(
                    bundle.parent / "runtime-admission-identities.json"
                ),
                bundle_manifest=bundle,
                controller_lock=controller_lock,
                frozen_analyzer=root / "frozen-analyze.py",
                active_analyzer=root / "active-analyze.py",
            )
            provenance = {
                "activeAnalyzer": {"sha256": active_sha},
                "frozenAnalyzer": {"sha256": frozen_sha},
            }
            inventory = {"files": [{"path": "raw.json"}]}
            summary = {"inventorySha256": "1" * 64}
            with (
                mock.patch.object(
                    POSTHOC,
                    "verify_frozen_binding",
                    return_value=provenance,
                ),
                mock.patch.object(
                    POSTHOC,
                    "build_raw_inventory",
                    side_effect=[(inventory, summary), (inventory, summary)],
                ) as inventory_mock,
                mock.patch.object(
                    POSTHOC,
                    "load_active_analyzer",
                    return_value=module,
                ),
                mock.patch.object(
                    POSTHOC,
                    "sha256_file",
                    return_value=active_sha,
                ),
            ):
                with self.assertRaisesRegex(
                    POSTHOC.PostHocError,
                    "rejected the historical evidence: failed phase",
                ) as raised:
                    POSTHOC.execute(args)
            self.assertIsInstance(raised.exception.__cause__, CorrectedAnalysisFailure)
            self.assertEqual(inventory_mock.call_count, 2)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse(output.exists())

    def test_active_analyzer_requires_an_exception_failure_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            analyzer = Path(temporary) / "analyze.py"
            analyzer.write_text(
                "AnalysisFailure = object()\n"
                "def analyze(args): return ({}, 0)\n"
                "def render_markdown(report): return ''\n"
                "def validate_frozen_bundle(*args, **kwargs): return None\n"
                "def validate_state(*args, **kwargs): return None\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                POSTHOC.PostHocError,
                "not an exception class",
            ):
                POSTHOC.load_active_analyzer(
                    analyzer,
                    POSTHOC.sha256_file(analyzer),
                )


if __name__ == "__main__":
    unittest.main()
