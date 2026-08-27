from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from amazon_crawler.application.cookie_maintenance import CookieMaintenanceService
from amazon_crawler.config import load_cookie_targets
from amazon_crawler.domain.resources import HarvestReport


class FakeHarvester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def ensure_capacity(
        self, marketplace_id: str, postal_code: str, target_count: int
    ) -> HarvestReport:
        self.calls.append((marketplace_id, postal_code, target_count))
        return HarvestReport(
            marketplace_id=marketplace_id,
            postal_code=postal_code,
            requested=target_count,
            created=1,
            rejected=0,
            available_after=target_count,
        )


class SelectivelyFailingHarvester(FakeHarvester):
    async def ensure_capacity(
        self, marketplace_id: str, postal_code: str, target_count: int
    ) -> HarvestReport:
        self.calls.append((marketplace_id, postal_code, target_count))
        if postal_code == "10001":
            raise RuntimeError("redis://user:password@private.example.test/0")
        return HarvestReport(
            marketplace_id=marketplace_id,
            postal_code=postal_code,
            requested=target_count,
            created=1,
            rejected=0,
            available_after=target_count,
        )


class CookieMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_forever_repeats_targets_and_stops_cleanly(self) -> None:
        harvester = FakeHarvester()
        service = CookieMaintenanceService(
            {"default": harvester},
            {"default": {"US": {"10001": 1}}},
            interval_seconds=60,
        )
        service._interval_seconds = 0.01
        task = asyncio.create_task(service.run_forever())
        for _ in range(50):
            if len(harvester.calls) >= 2:
                break
            await asyncio.sleep(0.01)
        service.stop()
        await asyncio.wait_for(task, timeout=1)

        self.assertGreaterEqual(len(harvester.calls), 2)
        self.assertTrue(all(call == ("US", "10001", 1) for call in harvester.calls))

    async def test_run_once_covers_every_declared_target(self) -> None:
        harvester = FakeHarvester()
        service = CookieMaintenanceService(
            {"default": harvester},
            {
                "default": {
                    "US": {"10001": 5, "94105": 3},
                    "JP": {"100-0001": 2},
                }
            },
            interval_seconds=60,
        )
        reports = await service.run_once()
        self.assertEqual(len(reports), 3)
        self.assertEqual(
            harvester.calls,
            [("US", "10001", 5), ("US", "94105", 3), ("JP", "100-0001", 2)],
        )

    async def test_target_file_is_strictly_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "targets.json"
            path.write_text(json.dumps({"us": {"10001": 5}}), encoding="utf-8")
            self.assertEqual(
                load_cookie_targets(path),
                {"default": {"US": {"10001": 5}}},
            )
            path.write_text(
                json.dumps(
                    {
                        "default": {"US": {"10001": 5}},
                        "overseas": {"JP": {"100-0001": 2}},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                load_cookie_targets(path)["overseas"],
                {"JP": {"100-0001": 2}},
            )
            path.write_text(json.dumps({"US": {"10001": -1}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cookie_targets(path)

    async def test_unknown_pool_is_rejected_before_maintenance_starts(self) -> None:
        with self.assertRaisesRegex(ValueError, "unconfigured pools"):
            CookieMaintenanceService(
                {"default": FakeHarvester()},
                {"overseas": {"JP": {"100-0001": 2}}},
                interval_seconds=60,
            )

    async def test_one_target_failure_does_not_abort_or_leak_secrets(self) -> None:
        harvester = SelectivelyFailingHarvester()
        service = CookieMaintenanceService(
            {"default": harvester},
            {"default": {"US": {"10001": 2, "94105": 3}}},
            interval_seconds=60,
        )
        reports = await service.run_once()
        self.assertEqual(len(reports), 2)
        self.assertFalse(reports[0]["ok"])
        self.assertEqual(reports[0]["error_type"], "RuntimeError")
        self.assertTrue(reports[1]["ok"])
        self.assertEqual(
            harvester.calls,
            [("US", "10001", 2), ("US", "94105", 3)],
        )
        rendered = json.dumps(reports)
        self.assertNotIn("password", rendered)
        self.assertNotIn("private.example.test", rendered)


if __name__ == "__main__":
    unittest.main()
