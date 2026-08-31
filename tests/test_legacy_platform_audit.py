from __future__ import annotations

import unittest
from pathlib import Path

from scripts.audit_legacy_platform_contracts import audit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OLD_ROOT = PROJECT_ROOT.parent
REFERENCE_SOURCE_AVAILABLE = (OLD_ROOT / "main.py").is_file()


class LegacyPlatformAuditTests(unittest.TestCase):
    @unittest.skipUnless(
        REFERENCE_SOURCE_AVAILABLE,
        "optional migration-reference source is not present in this checkout",
    )
    def test_legacy_platform_mechanisms_have_v2_replacements(self) -> None:
        report = audit(OLD_ROOT, PROJECT_ROOT)

        self.assertTrue(report["ok"])
        self.assertFalse(report["imports_executed"])
        self.assertEqual(report["legacy_task_count"], 11)
        self.assertEqual(report["missing_v2_paths"], [])
        self.assertEqual(
            {group["name"] for group in report["groups"]},
            {
                "task_orchestration",
                "result_and_status_delivery",
                "crash_recovery",
                "cookie_lifecycle",
                "proxy_lifecycle",
                "management_surface",
            },
        )
        for group in report["groups"]:
            self.assertTrue(group["ok"], group["name"])
            self.assertTrue(all(group["checks"].values()), group["name"])


if __name__ == "__main__":
    unittest.main()
