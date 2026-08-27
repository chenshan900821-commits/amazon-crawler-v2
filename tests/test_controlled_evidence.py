from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.compile_controlled_evidence import (
    ControlledEvidenceError,
    SCENARIO_CASES,
    compile_evidence,
    validate_compiled_pair,
    validate_run_report,
)
from scripts.compile_cookie_production_evidence import (
    compile_cookie_production_evidence,
)
from scripts.promote_parity_manifest import promote_manifest
from scripts.verify_parity_manifest import (
    EXPECTED_TASKS,
    _validate_controlled_evidence,
    validate_manifest,
)
from scripts.runtime_source_fingerprint import runtime_source_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid_report() -> dict[str, object]:
    scenarios = []
    for index, kind in enumerate(sorted(EXPECTED_TASKS)):
        for case in sorted(SCENARIO_CASES):
            identity = f"{kind}:{case}"
            scenarios.append(
                {
                    "kind": kind,
                    "case": case,
                    "status": "passed",
                    "authorized": True,
                    "marketplace_id": "US" if index % 2 == 0 else "JP",
                    "postal_code_hash": digest(f"postal-{index % 2}"),
                    "sample_hash": digest(f"sample:{identity}"),
                    "input_hash": digest(f"input:{identity}"),
                    "response_sha256": digest(f"response:{identity}"),
                    "legacy_result_hash": digest(f"legacy:{identity}"),
                    "v2_result_hash": digest(f"v2:{identity}"),
                    "field_contract_match": True,
                    "outcome_match": True,
                    "differences": [],
                }
            )
    return {
        "schema_version": "controlled-run-report.v1",
        "run_id": "isolated-run-20260822",
        "authorization_reference": "change-ticket-123",
        "validated_at": "2026-08-22T09:00:00+08:00",
        "environment": "isolated",
        "runtime": {
            "transport_backend": "curl_cffi",
            "tls_impersonation": True,
            "v2_source_sha256": runtime_source_sha256(PROJECT_ROOT),
        },
        "checks": {
            "cookie_proxy_redaction": True,
            "checkpoint_recovery": True,
            "legacy_bridge_roundtrip": True,
            "tls_impersonation": True,
        },
        "check_receipts": {
            check: {
                "schema_version": "controlled-check-receipt.v1",
                "check": check,
                "status": "passed",
                "environment": "isolated",
                "run_id": "isolated-run-20260822",
                "authorization_reference": "change-ticket-123",
                "validated_at": "2026-08-22T09:00:00+08:00",
                "artifact_sha256": digest(f"check:{check}"),
            }
            for check in (
                "cookie_proxy_redaction",
                "checkpoint_recovery",
                "legacy_bridge_roundtrip",
                "tls_impersonation",
            )
        },
        "scenarios": scenarios,
    }


def valid_cookie_production_receipt() -> dict[str, object]:
    operations = []
    for pool, marketplace, postal in (
        ("default", "US", "10001"),
        ("overseas", "JP", "140-0001"),
    ):
        operations.append(
            {
                "pool": pool,
                "marketplace_id": marketplace,
                "postal_code_hash": digest(postal),
                "available_before": 1,
                "created": 1,
                "rejected": 0,
                "available_after": 2,
                "ttl_seconds": 172800,
                "address_confirmed": True,
                "consumer_visible": True,
            }
        )
    return {
        "schema_version": "controlled-cookie-production-evidence.v1",
        "capability": "cookie_production_scheduler",
        "status": "passed",
        "environment": "isolated",
        "validated_at": "2026-08-22T09:00:00+08:00",
        "authorization_reference": "change-ticket-123",
        "runtime": {
            "transport_backend": "curl_cffi",
            "tls_impersonation": True,
            "v2_source_sha256": runtime_source_sha256(PROJECT_ROOT),
        },
        "checks": {
            "external_write_authorized": True,
            "address_confirmed": True,
            "ttl_verified": True,
            "consumer_visible": True,
            "cookie_values_redacted": True,
            "maintenance_run_once_passed": True,
        },
        "operations": operations,
    }


class ControlledEvidenceTests(unittest.TestCase):
    def test_complete_run_compiles_gate_valid_receipts(self) -> None:
        report = valid_report()
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir).resolve()
            output_dir = project_root / "evidence" / "controlled"
            written = compile_evidence(
                report,
                project_root=project_root,
                output_dir=output_dir,
            )

            self.assertEqual(len(written), len(EXPECTED_TASKS) * 2)
            for kind in EXPECTED_TASKS:
                errors = _validate_controlled_evidence(
                    project_root=project_root,
                    kind=kind,
                    evidence_path=output_dir / f"{kind}.json",
                )
                self.assertEqual(errors, [], kind)

    def test_missing_scenario_cannot_compile(self) -> None:
        report = valid_report()
        report["scenarios"].pop()

        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "exactly 33 scenarios",
        ):
            validate_run_report(report)

    def test_failed_bridge_or_shadow_difference_cannot_compile(self) -> None:
        failed_bridge = valid_report()
        failed_bridge["checks"]["legacy_bridge_roundtrip"] = False
        with self.assertRaisesRegex(ControlledEvidenceError, "failing checks"):
            validate_run_report(failed_bridge)

        mismatch = valid_report()
        mismatch["scenarios"][0]["differences"] = ["selling_price"]
        with self.assertRaisesRegex(ControlledEvidenceError, "unresolved differences"):
            validate_run_report(mismatch)

    def test_global_boolean_without_matching_receipt_cannot_compile(self) -> None:
        report = valid_report()
        report["check_receipts"].pop("checkpoint_recovery")

        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "receipt for every global check",
        ):
            validate_run_report(report)

    def test_secret_fields_and_credential_uris_are_rejected(self) -> None:
        for field, value in (
            ("cookie", "session-secret"),
            ("database_url", "mysql://user:pass@example/db"),
        ):
            with self.subTest(field=field):
                report = copy.deepcopy(valid_report())
                report[field] = value
                with self.assertRaisesRegex(
                    ControlledEvidenceError,
                    "forbidden secret field",
                ):
                    validate_run_report(report)

        report = valid_report()
        report["run_id"] = "https://user:pass@example.test/run"
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "credential-bearing URI",
        ):
            validate_run_report(report)

    def test_compiler_refuses_symlinked_artifact_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir).resolve()
            output_dir = project_root / "evidence" / "controlled"
            output_dir.mkdir(parents=True)
            outside = project_root / "outside.json"
            outside.write_text("untouched", encoding="utf-8")
            (output_dir / "category_asin_list-comparison.json").symlink_to(outside)

            with self.assertRaisesRegex(
                ControlledEvidenceError,
                "symlinked evidence artifact",
            ):
                compile_evidence(
                    valid_report(),
                    project_root=project_root,
                    output_dir=output_dir,
                )

            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

    def test_compiled_pair_rejects_tampered_sample_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir).resolve()
            output_dir = project_root / "evidence" / "controlled"
            compile_evidence(
                valid_report(),
                project_root=project_root,
                output_dir=output_dir,
            )
            kind = "product"
            receipt = json.loads(
                (output_dir / f"{kind}.json").read_text(encoding="utf-8")
            )
            comparison = json.loads(
                (output_dir / f"{kind}-comparison.json").read_text(
                    encoding="utf-8"
                )
            )
            receipt["sample_hashes"][0] = digest("tampered")

            with self.assertRaisesRegex(
                ControlledEvidenceError,
                "sample hashes do not match",
            ):
                validate_compiled_pair(
                    receipt,
                    comparison,
                    expected_kind=kind,
                )

    def test_manifest_promotion_requires_review_and_passes_final_gate(self) -> None:
        source_manifest = json.loads(
            (PROJECT_ROOT / "contracts" / "legacy_parity.v1.json").read_text(
                encoding="utf-8"
            )
        )
        cookie_evidence_path = "evidence/controlled/cookie-production.json"
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir).resolve()
            manifest_path = project_root / "contracts" / "legacy_parity.v1.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(source_manifest),
                encoding="utf-8",
            )
            for task in source_manifest["tasks"]:
                for item in task["evidence"]:
                    path = project_root / item["path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch(exist_ok=True)
            for capability in source_manifest["shared_capabilities"]:
                for item in capability["evidence"]:
                    path = project_root / item["path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch(exist_ok=True)
            cookie_evidence_file = project_root / cookie_evidence_path
            cookie_evidence_file.parent.mkdir(parents=True, exist_ok=True)
            cookie_evidence_file.write_text(
                json.dumps(valid_cookie_production_receipt()),
                encoding="utf-8",
            )
            evidence_dir = project_root / "evidence" / "controlled"
            compile_evidence(
                valid_report(),
                project_root=project_root,
                output_dir=evidence_dir,
            )

            with self.assertRaisesRegex(
                ControlledEvidenceError,
                "explicit confirmation",
            ):
                promote_manifest(
                    manifest_path=manifest_path,
                    evidence_dir=evidence_dir,
                    confirmed_reviewed=False,
                )

            result = promote_manifest(
                manifest_path=manifest_path,
                evidence_dir=evidence_dir,
                confirmed_reviewed=True,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(
                validate_manifest(manifest_path, require_complete=True),
                [],
            )

    def test_cookie_production_compiler_requires_external_valid_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir).resolve()
            project_root = root / "project"
            project_root.mkdir()
            source = root / "raw-cookie-production.json"
            source.write_text(
                json.dumps(valid_cookie_production_receipt()),
                encoding="utf-8",
            )
            output = (
                project_root / "evidence" / "controlled" / "cookie-production.json"
            )

            result = compile_cookie_production_evidence(
                source,
                project_root=project_root,
                output_path=output,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["operation_count"], 2)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                valid_cookie_production_receipt(),
            )
            with self.assertRaisesRegex(
                ControlledEvidenceError,
                "outside the project",
            ):
                compile_cookie_production_evidence(
                    output,
                    project_root=project_root,
                    output_path=output,
                )


if __name__ == "__main__":
    unittest.main()
