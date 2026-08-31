from __future__ import annotations

import unittest

from scripts.audit_public_artifacts import RULES


class PublicArtifactAuditTests(unittest.TestCase):
    def test_cookie_placeholders_are_allowed_but_real_values_are_rejected(self) -> None:
        pattern = RULES["cookie_value"]
        self.assertIsNone(pattern.search("session-id=SESSION_ID_VALUE; ubid-main=UBID_VALUE"))
        self.assertIsNone(pattern.search("session-id=<your-cookie-value>"))
        self.assertIsNotNone(pattern.search("session-id=123-1234567-1234567"))


if __name__ == "__main__":
    unittest.main()
