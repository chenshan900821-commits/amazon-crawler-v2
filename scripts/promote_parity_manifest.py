from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from scripts.compile_controlled_evidence import (
    ControlledEvidenceError,
    validate_compiled_pair,
)
from scripts.verify_parity_manifest import (
    CONTROLLED_COOKIE_EVIDENCE_TYPE,
    COMPLETE_EVIDENCE,
    EXPECTED_TASKS,
    _validate_cookie_production_evidence,
    _validate_controlled_evidence,
    validate_manifest,
)


def promote_manifest(
    *,
    manifest_path: Path,
    evidence_dir: Path,
    confirmed_reviewed: bool,
) -> dict[str, Any]:
    if not confirmed_reviewed:
        raise ControlledEvidenceError(
            "promotion requires explicit confirmation that every comparison was reviewed"
        )
    if manifest_path.is_symlink():
        raise ControlledEvidenceError("refusing to replace a symlinked manifest")
    if evidence_dir.is_symlink():
        raise ControlledEvidenceError("refusing a symlinked evidence directory")
    manifest_path = manifest_path.resolve()
    project_root = manifest_path.parent.parent.resolve()
    evidence_dir = evidence_dir.resolve()
    if not evidence_dir.is_relative_to(project_root):
        raise ControlledEvidenceError("evidence directory must be inside the project")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or {task.get("kind") for task in tasks} != EXPECTED_TASKS:
        raise ControlledEvidenceError("manifest task matrix is incomplete")

    evidence_paths: dict[str, str] = {}
    for kind in EXPECTED_TASKS:
        receipt_path = evidence_dir / f"{kind}.json"
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise ControlledEvidenceError(f"missing controlled receipt for {kind}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        report = receipt.get("comparison_report")
        raw_comparison_path = report.get("path") if isinstance(report, dict) else None
        if not isinstance(raw_comparison_path, str):
            raise ControlledEvidenceError(f"missing comparison report for {kind}")
        raw_path = project_root / raw_comparison_path
        if raw_path.is_symlink():
            raise ControlledEvidenceError(f"symlinked comparison report for {kind}")
        comparison_path = raw_path.resolve()
        if (
            not comparison_path.is_relative_to(project_root)
            or not comparison_path.is_file()
        ):
            raise ControlledEvidenceError(f"missing comparison report for {kind}")
        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
        validate_compiled_pair(receipt, comparison, expected_kind=kind)
        errors = _validate_controlled_evidence(
            project_root=project_root,
            kind=kind,
            evidence_path=receipt_path,
        )
        if errors:
            raise ControlledEvidenceError(
                f"controlled receipt for {kind} failed final validation"
            )
        evidence_paths[kind] = str(receipt_path.relative_to(project_root))

    cookie_receipt_path = evidence_dir / "cookie-production.json"
    if cookie_receipt_path.is_symlink() or not cookie_receipt_path.is_file():
        raise ControlledEvidenceError("missing controlled Cookie production receipt")
    cookie_errors = _validate_cookie_production_evidence(cookie_receipt_path)
    if cookie_errors:
        raise ControlledEvidenceError(
            "controlled Cookie production receipt failed final validation"
        )
    shared = payload.get("shared_capabilities")
    if not isinstance(shared, list):
        raise ControlledEvidenceError("manifest shared capabilities are invalid")
    cookie_capability = next(
        (
            capability
            for capability in shared
            if isinstance(capability, dict)
            and capability.get("name") == "cookie_production_scheduler"
        ),
        None,
    )
    if not isinstance(cookie_capability, dict) or not isinstance(
        cookie_capability.get("evidence"), list
    ):
        raise ControlledEvidenceError(
            "manifest Cookie production capability is incomplete"
        )
    cookie_capability["evidence"] = [
        item
        for item in cookie_capability["evidence"]
        if not isinstance(item, dict)
        or item.get("type") != CONTROLLED_COOKIE_EVIDENCE_TYPE
    ]
    cookie_capability["evidence"].append(
        {
            "type": CONTROLLED_COOKIE_EVIDENCE_TYPE,
            "path": str(cookie_receipt_path.relative_to(project_root)),
            "passing": True,
        }
    )

    for task in tasks:
        evidence = task.get("evidence")
        if not isinstance(evidence, list):
            raise ControlledEvidenceError(
                f"manifest evidence is invalid for {task.get('kind')}"
            )
        existing_types = {
            item.get("type")
            for item in evidence
            if isinstance(item, dict) and item.get("passing") is True
        }
        missing_before_controlled = (COMPLETE_EVIDENCE - {"controlled_integration"}) - existing_types
        if missing_before_controlled:
            raise ControlledEvidenceError(
                f"offline evidence is incomplete for {task.get('kind')}"
            )
        evidence[:] = [
            item
            for item in evidence
            if not isinstance(item, dict)
            or item.get("type") != "controlled_integration"
        ]
        evidence.append(
            {
                "type": "controlled_integration",
                "path": evidence_paths[str(task["kind"])],
                "passing": True,
            }
        )
        task["current_status"] = "complete"
    payload["overall_status"] = "complete"

    candidate_fd, candidate_name = tempfile.mkstemp(
        prefix=".legacy-parity-candidate-",
        suffix=".json",
        dir=manifest_path.parent,
    )
    candidate_path = Path(candidate_name)
    try:
        with os.fdopen(candidate_fd, "w", encoding="utf-8") as candidate:
            json.dump(payload, candidate, ensure_ascii=False, indent=2)
            candidate.write("\n")
        errors = validate_manifest(candidate_path, require_complete=True)
        if errors:
            raise ControlledEvidenceError(
                "candidate manifest did not pass the final completion gate"
            )
        os.replace(candidate_path, manifest_path)
    finally:
        if candidate_path.exists():
            candidate_path.unlink()
    return {
        "ok": True,
        "overall_status": "complete",
        "task_count": len(EXPECTED_TASKS),
        "manifest": str(manifest_path.relative_to(project_root)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Promote the parity manifest only after all compiled controlled "
            "comparisons have been reviewed. Performs no external writes."
        )
    )
    project_root = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "--manifest",
        type=Path,
        default=project_root / "contracts" / "legacy_parity.v1.json",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=project_root / "evidence" / "controlled",
    )
    parser.add_argument("--confirm-reviewed", action="store_true")
    args = parser.parse_args()
    try:
        result = promote_manifest(
            manifest_path=args.manifest,
            evidence_dir=args.evidence_dir,
            confirmed_reviewed=args.confirm_reviewed,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ControlledEvidenceError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "controlled evidence promotion could not be completed"
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": message},
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
