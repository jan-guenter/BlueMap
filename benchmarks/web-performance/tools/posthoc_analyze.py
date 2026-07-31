#!/usr/bin/env python3
"""Run a corrected analyzer against preserved evidence without claiming a frozen run.

This deliberately lives outside ``controller/formal``.  It verifies the analyzer
that was bound into the historical run, inventories the evidence, then executes a
different analyzer under an explicit post-hoc policy label.  It never writes below
the historical run root.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Sequence


class PostHocError(RuntimeError):
    """A provenance or evidence-integrity precondition failed."""


DIGEST = re.compile(r"^[0-9a-f]{64}$")
POLICY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
INVENTORY_SCOPE = "historical-run-excluding-derived-analysis-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_regular_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PostHocError(f"{label} is missing, not regular, or a symlink: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PostHocError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise PostHocError(f"{label} must contain a JSON object: {path}")
    return value


def require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise PostHocError(f"{label} is not a lowercase SHA-256 digest")
    return value


def verify_frozen_binding(
    *,
    run_root: Path,
    bundle_manifest: Path,
    controller_lock: Path,
    frozen_analyzer: Path,
    active_analyzer: Path,
) -> dict[str, Any]:
    for path, label in (
        (frozen_analyzer, "frozen analyzer"),
        (active_analyzer, "active analyzer"),
    ):
        if path.is_symlink() or not path.is_file():
            raise PostHocError(f"{label} is missing, not regular, or a symlink: {path}")

    bundle = load_regular_object(bundle_manifest, "bundle manifest")
    state_path = run_root / "state.json"
    state = load_regular_object(state_path, "run state")
    lock = load_regular_object(controller_lock, "controller lock")
    controllers = lock.get("controllers")
    matches = (
        [
            item
            for item in controllers
            if isinstance(item, dict) and item.get("path") == "analyze.py"
        ]
        if isinstance(controllers, list)
        else []
    )
    if len(matches) != 1:
        raise PostHocError("controller lock must contain exactly one analyze.py binding")

    frozen_sha = sha256_file(frozen_analyzer)
    bindings = {
        "frozen file": frozen_sha,
        "bundle manifest": require_digest(
            bundle.get("analyzerSha256"), "bundle analyzerSha256"
        ),
        "run state": require_digest(state.get("analyzerSha256"), "state analyzerSha256"),
        "controller lock": require_digest(matches[0].get("sha256"), "lock analyze.py sha256"),
    }
    if len(set(bindings.values())) != 1:
        raise PostHocError(f"historical frozen analyzer binding mismatch: {bindings}")

    active_sha = sha256_file(active_analyzer)
    if active_sha == frozen_sha:
        raise PostHocError(
            "active analyzer is identical to the frozen analyzer; this is not post-hoc"
        )
    return {
        "frozenAnalyzer": {"path": str(frozen_analyzer), "sha256": frozen_sha},
        "activeAnalyzer": {"path": str(active_analyzer), "sha256": active_sha},
        "bundleManifest": {"path": str(bundle_manifest), "sha256": sha256_file(bundle_manifest)},
        "controllerLock": {"path": str(controller_lock), "sha256": sha256_file(controller_lock)},
        "runState": {"path": str(state_path), "sha256": sha256_file(state_path)},
    }


def build_raw_inventory(run_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if run_root.is_symlink() or not run_root.is_dir():
        raise PostHocError(f"run root is missing, not a directory, or a symlink: {run_root}")
    files: list[dict[str, Any]] = []
    for path in sorted(
        run_root.rglob("*"), key=lambda item: item.relative_to(run_root).as_posix()
    ):
        relative = path.relative_to(run_root)
        if relative.parts and relative.parts[0] == "analysis":
            continue
        if path.is_symlink():
            raise PostHocError(f"raw evidence contains a symlink: {relative.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PostHocError(f"raw evidence contains a special file: {relative.as_posix()}")
        files.append(
            {"path": relative.as_posix(), "size": path.stat().st_size, "sha256": sha256_file(path)}
        )
    inventory = {
        "formatVersion": 1,
        "scope": INVENTORY_SCOPE,
        "excludedTopLevel": ["analysis"],
        "files": files,
    }
    summary = {
        "inventoryScope": INVENTORY_SCOPE,
        "inventorySha256": canonical_sha256(inventory),
        "fileCount": len(files),
        "totalBytes": sum(item["size"] for item in files),
    }
    return inventory, summary


def load_active_analyzer(path: Path, digest: str) -> ModuleType:
    name = f"bluemap_posthoc_analyzer_{digest[:16]}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PostHocError(f"cannot load active analyzer: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    for symbol in ("analyze", "render_markdown", "validate_frozen_bundle", "validate_state"):
        if not callable(getattr(module, symbol, None)):
            raise PostHocError(f"active analyzer has no callable {symbol}()")
    analysis_failure = getattr(module, "AnalysisFailure", None)
    if (
        not isinstance(analysis_failure, type)
        or not issubclass(analysis_failure, Exception)
    ):
        raise PostHocError(
            "active analyzer AnalysisFailure is not an exception class"
        )
    return module


@contextlib.contextmanager
def bind_frozen_analyzer_digest(
    module: ModuleType, active_sha: str, frozen_sha: str
) -> Iterator[None]:
    """Substitute only the analyzer digest passed to the two binding validators."""

    originals: dict[str, Any] = {}
    for name in ("validate_frozen_bundle", "validate_state"):
        original = getattr(module, name)
        originals[name] = original
        signature = inspect.signature(original)

        def bridge(
            *args: Any,
            __original: Any = original,
            __signature: Any = signature,
            **kwargs: Any,
        ) -> Any:
            bound = __signature.bind(*args, **kwargs)
            if bound.arguments.get("analyzer_digest") != active_sha:
                raise PostHocError("active analyzer supplied an unexpected self-digest")
            bound.arguments["analyzer_digest"] = frozen_sha
            return __original(*bound.args, **bound.kwargs)

        setattr(module, name, bridge)
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(module, name, original)


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def execute(args: argparse.Namespace) -> int:
    run_root = args.run_root.resolve()
    output_dir = args.output_dir.resolve()
    if output_dir == run_root or run_root in output_dir.parents:
        raise PostHocError("post-hoc output directory must be outside the historical run root")
    if output_dir.exists():
        raise PostHocError(f"post-hoc output directory must not already exist: {output_dir}")
    if not POLICY_ID.fullmatch(args.policy_id):
        raise PostHocError("policy id must match [a-z0-9][a-z0-9._-]{0,127}")

    paths = {
        name: Path(getattr(args, name)).resolve()
        for name in (
            "matrix",
            "schedule",
            "runtime_admission_identities",
            "bundle_manifest",
            "controller_lock",
            "frozen_analyzer",
            "active_analyzer",
        )
    }
    expected_lock = paths["bundle_manifest"].parent.parent / "controller-lock.json"
    if paths["controller_lock"] != expected_lock.resolve():
        raise PostHocError(
            "controller lock is not the lock adjacent to the selected frozen bundle"
        )
    provenance = verify_frozen_binding(
        run_root=run_root,
        bundle_manifest=paths["bundle_manifest"],
        controller_lock=paths["controller_lock"],
        frozen_analyzer=paths["frozen_analyzer"],
        active_analyzer=paths["active_analyzer"],
    )
    inventory_before, raw_summary = build_raw_inventory(run_root)
    active_sha = provenance["activeAnalyzer"]["sha256"]
    frozen_sha = provenance["frozenAnalyzer"]["sha256"]
    module = load_active_analyzer(paths["active_analyzer"], active_sha)
    analyzer_args = argparse.Namespace(
        matrix=paths["matrix"],
        schedule=paths["schedule"],
        runtime_admission_identities=paths["runtime_admission_identities"],
        bundle_manifest=paths["bundle_manifest"],
        run_root=run_root,
        output_dir=output_dir,
    )
    analysis_failure = module.AnalysisFailure
    rejected_evidence: Exception | None = None
    report: Any = None
    status: Any = None
    try:
        with bind_frozen_analyzer_digest(module, active_sha, frozen_sha):
            try:
                report, status = module.analyze(analyzer_args)
            except analysis_failure as error:
                rejected_evidence = error
    finally:
        # A rejected analysis must not bypass the immutable-input checks.
        inventory_after, raw_summary_after = build_raw_inventory(run_root)
    if sha256_file(paths["active_analyzer"]) != active_sha:
        raise PostHocError("active analyzer changed during execution")
    if inventory_after != inventory_before or raw_summary_after != raw_summary:
        raise PostHocError("historical raw evidence changed during post-hoc analysis")
    if rejected_evidence is not None:
        raise PostHocError(
            f"active analyzer rejected the historical evidence: {rejected_evidence}"
        ) from rejected_evidence
    if not isinstance(report, dict) or status not in (0, 2):
        raise PostHocError("active analyzer returned an invalid report or exit status")

    generated_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    wrapper_path = Path(__file__).resolve()
    provenance.update(
        {
            "formatVersion": 1,
            "analysisMode": "posthoc",
            "formalClaim": False,
            "policyId": args.policy_id,
            "generatedAt": generated_at,
            "frozenBindingVerified": True,
            "wrapper": {"path": str(wrapper_path), "sha256": sha256_file(wrapper_path)},
            "rawEvidence": {
                "root": str(run_root),
                **raw_summary,
                "inventoryFile": "raw-evidence-inventory.json",
            },
        }
    )
    underlying_status = report.get("analysisStatus")
    report["analysisMode"] = "posthoc"
    report["formalClaim"] = False
    report["underlyingAnalysisStatus"] = underlying_status
    report["analysisStatus"] = f"posthoc-{underlying_status or 'unknown'}"
    report["postHocProvenance"] = provenance

    output_dir.mkdir(parents=True, mode=0o700)
    atomic_json(output_dir / "raw-evidence-inventory.json", inventory_before)
    atomic_json(output_dir / "provenance.json", provenance)
    atomic_json(output_dir / "report.json", report)
    banner = (
        "# POST-HOC BlueMap web-performance analysis\n\n"
        "**Derivative result only: this is not the frozen formal analyzer result.**\n\n"
        f"- Policy: `{args.policy_id}`\n"
        f"- Frozen analyzer SHA-256: `{frozen_sha}`\n"
        f"- Active analyzer SHA-256: `{active_sha}`\n"
        f"- Raw evidence inventory SHA-256: `{raw_summary['inventorySha256']}`\n\n"
        "## Corrected analyzer report\n\n"
    )
    atomic_text(output_dir / "report.md", banner + module.render_markdown(report))
    atomic_text(output_dir / "exit-status.txt", f"{status}\n")
    checksummed = sorted(
        (
            "exit-status.txt",
            "provenance.json",
            "raw-evidence-inventory.json",
            "report.json",
            "report.md",
        )
    )
    atomic_text(
        output_dir / "SHA256SUMS",
        "".join(f"{sha256_file(output_dir / name)}  {name}\n" for name in checksummed),
    )
    print(
        json.dumps(
            {"outputDir": str(output_dir), "status": status, "provenance": provenance},
            sort_keys=True,
        )
    )
    return status


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "matrix",
        "schedule",
        "runtime-admission-identities",
        "bundle-manifest",
        "controller-lock",
        "run-root",
        "frozen-analyzer",
        "active-analyzer",
        "output-dir",
    ):
        parser.add_argument(f"--{name}", required=True, type=Path, dest=name.replace("-", "_"))
    parser.add_argument("--policy-id", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return execute(parse_args(argv))
    except (PostHocError, OSError, ValueError) as error:
        print(f"post-hoc analysis failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
