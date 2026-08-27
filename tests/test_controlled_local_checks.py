from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.collect_controlled_v2_shadow import validate_shadow_plan
from scripts.compile_controlled_evidence import ControlledEvidenceError
from scripts.generate_controlled_local_checks import (
    generate_local_check_receipts,
)
from tests.test_controlled_v2_shadow import valid_plan


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ControlledLocalCheckTests(unittest.TestCase):
    def test_real_state_and_secret_self_tests_generate_plan_valid_receipts(self) -> None:
        plan = valid_plan()
        with tempfile.TemporaryDirectory() as tempdir:
            output_dir = Path(tempdir) / "checks"
            written = generate_local_check_receipts(
                run_id=plan["run_id"],
                authorization_reference=plan["authorization_reference"],
                output_dir=output_dir,
                project_root=PROJECT_ROOT,
            )

            self.assertEqual(len(written), 4)
            for check in ("checkpoint_recovery", "cookie_proxy_redaction"):
                artifact_path = output_dir / f"{check}-artifact.json"
                receipt_path = output_dir / f"{check}.json"
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                self.assertEqual(receipt["check"], check)
                self.assertEqual(receipt["status"], "passed")
                self.assertEqual(artifact["status"], "passed")
                self.assertEqual(
                    receipt["artifact_sha256"],
                    hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                )
                plan["check_receipts"][check] = receipt

            self.assertEqual(
                validate_shadow_plan(plan)["schema_version"],
                "controlled-shadow-plan.v1",
            )
            checkpoint = json.loads(
                (output_dir / "checkpoint_recovery-artifact.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(checkpoint["observations"]["stale_result_rejected"])
            self.assertEqual(checkpoint["observations"]["restart_checkpoint"], 2)
            redaction = json.loads(
                (output_dir / "cookie_proxy_redaction-artifact.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(all(redaction["observations"].values()))

    def test_check_artifacts_cannot_be_written_inside_project_or_through_symlink(self) -> None:
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "outside the project",
        ):
            generate_local_check_receipts(
                run_id="run-1",
                authorization_reference="ticket-1",
                output_dir=PROJECT_ROOT / "evidence" / "raw-checks",
                project_root=PROJECT_ROOT,
            )

        with tempfile.TemporaryDirectory() as tempdir:
            output_dir = Path(tempdir) / "checks"
            output_dir.mkdir()
            outside = Path(tempdir) / "outside.json"
            outside.write_text("untouched", encoding="utf-8")
            (output_dir / "checkpoint_recovery-artifact.json").symlink_to(outside)

            with self.assertRaisesRegex(
                ControlledEvidenceError,
                "symlinked check artifact",
            ):
                generate_local_check_receipts(
                    run_id="run-1",
                    authorization_reference="ticket-1",
                    output_dir=output_dir,
                    project_root=PROJECT_ROOT,
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")


if __name__ == "__main__":
    unittest.main()
