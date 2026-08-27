from __future__ import annotations

import fnmatch
import unittest

from scripts.report_cookie_pool_coverage import cookie_pool_coverage


class KeyOnlyRedis:
    def __init__(self) -> None:
        self.keys = [
            b"cookie:A1VC38T7YXB528:150-0001:first-private-id",
            b"cookie:A1VC38T7YXB528:150-0001:second-private-id",
            b"cookie:A1VC38T7YXB528:530-0001:third-private-id",
            b"cookie:ATVPDKIKX0DER:10001:other-market-private-id",
        ]
        self.value_reads = 0

    def scan(self, cursor: int, *, match: str, count: int):
        rendered = [
            key for key in self.keys if fnmatch.fnmatch(key.decode("utf-8"), match)
        ]
        return 0, rendered

    def get(self, key):
        self.value_reads += 1
        raise AssertionError("Cookie values must not be read")

    def mget(self, keys):
        self.value_reads += 1
        raise AssertionError("Cookie values must not be read")


class CookiePoolCoverageTests(unittest.TestCase):
    def test_report_uses_key_metadata_without_cookie_values_or_ids(self) -> None:
        client = KeyOnlyRedis()

        report = cookie_pool_coverage(
            client,
            marketplace_id="A1VC38T7YXB528",
        )

        self.assertEqual(client.value_reads, 0)
        self.assertEqual(report["group_count"], 2)
        self.assertEqual(report["cookie_count"], 3)
        self.assertEqual(
            report["groups"],
            [
                {"postal_code": "150-0001", "cookie_count": 2},
                {"postal_code": "530-0001", "cookie_count": 1},
            ],
        )
        rendered = str(report)
        self.assertNotIn("private-id", rendered)
        self.assertIs(report["cookie_values_read"], False)
        self.assertIs(report["cookie_identifiers_disclosed"], False)


if __name__ == "__main__":
    unittest.main()
