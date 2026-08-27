from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_parity_manifest import (
    EXPECTED_SHARED_CAPABILITIES,
    EXPECTED_TASKS,
    validate_manifest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PROJECT_ROOT / "contracts" / "legacy_parity.v1.json"


class ParityManifestTests(unittest.TestCase):
    def test_manifest_covers_every_legacy_task(self) -> None:
        errors = validate_manifest(MANIFEST, require_complete=False)
        self.assertEqual(errors, [])
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual({task["kind"] for task in payload["tasks"]}, EXPECTED_TASKS)
        self.assertEqual(
            {item["name"] for item in payload["shared_capabilities"]},
            EXPECTED_SHARED_CAPABILITIES,
        )

    def test_shared_platform_capabilities_are_completion_gates(self) -> None:
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        payload["shared_capabilities"] = payload["shared_capabilities"][1:]
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "contracts").mkdir()
            path = root / "contracts" / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            errors = validate_manifest(path, require_complete=False)

        self.assertTrue(any("missing shared capabilities" in error for error in errors))

    def test_incomplete_manifest_cannot_pass_final_gate(self) -> None:
        errors = validate_manifest(MANIFEST, require_complete=True)
        self.assertTrue(errors)
        self.assertIn("overall_status is not complete", errors)
        self.assertTrue(
            any("controlled cookie production evidence" in error for error in errors)
        )

    def test_final_gate_requires_real_evidence_objects(self) -> None:
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        payload["overall_status"] = "complete"
        for task in payload["tasks"]:
            task["current_status"] = "complete"
            task["evidence"] = ["contract_test", "offline_replay"]
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            errors = validate_manifest(path, require_complete=True)
        self.assertTrue(any("missing passing evidence" in error for error in errors))
        self.assertTrue(any("controlled_integration" in error for error in errors))

    def test_passing_evidence_must_point_to_a_real_project_file(self) -> None:
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        payload["tasks"][0]["evidence"][0]["path"] = "tests/does-not-exist.py"
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "contracts").mkdir()
            path = root / "contracts" / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            errors = validate_manifest(path, require_complete=False)
        self.assertTrue(any("evidence path does not exist" in error for error in errors))

    def test_controlled_evidence_cannot_be_an_unstructured_file(self) -> None:
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        payload["tasks"][0]["evidence"].append(
            {
                "type": "controlled_integration",
                "path": "evidence/controlled/search.json",
                "passing": True,
            }
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "contracts").mkdir()
            evidence_root = root / "evidence" / "controlled"
            evidence_root.mkdir(parents=True)
            (evidence_root / "search.json").write_text("{}", encoding="utf-8")
            path = root / "contracts" / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            errors = validate_manifest(path, require_complete=False)
        self.assertTrue(any("controlled evidence schema_version" in error for error in errors))
        self.assertTrue(any("requires at least 3 SHA-256 sample hashes" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
