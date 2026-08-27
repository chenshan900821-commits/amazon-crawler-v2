from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import tempfile
import unittest
from pathlib import Path

from amazon_crawler.domain.resources import HarvestReport
from scripts.collect_cookie_production_evidence import (
    CookieProductionTarget,
    _atomic_write_outside_project,
    _build_resources,
    collect_cookie_production_receipt,
)
from scripts.compile_controlled_evidence import ControlledEvidenceError
from scripts.verify_parity_manifest import _validate_cookie_production_evidence


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeRedis:
    def __init__(self, marketplace_id: str, postal_code: str) -> None:
        self.marketplace_id = marketplace_id
        self.postal_code = postal_code
        self.values: dict[str, str] = {
            f"cookie:{marketplace_id}:{postal_code}:old": '{"session-id":"old"}'
        }
        self.ttls: dict[str, int] = {next(iter(self.values)): 3600}

    def add(self) -> None:
        key = f"cookie:{self.marketplace_id}:{self.postal_code}:new"
        self.values[key] = '{"session-id":"new"}'
        self.ttls[key] = 172800

    def scan(self, cursor: int, *, match: str, count: int):
        if cursor:
            return 0, []
        return 0, [key for key in self.values if fnmatch.fnmatchcase(key, match)]

    def mget(self, keys: list[str]):
        return [self.values.get(key) for key in keys]

    def get(self, key: str):
        return self.values.get(key)

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -2)


class FakeHarvester:
    backend = "curl_cffi"
    tls_impersonation = True

    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.calls: list[tuple[str, str, int]] = []

    async def ensure_capacity(
        self, marketplace_id: str, postal_code: str, target_count: int
    ) -> HarvestReport:
        self.calls.append((marketplace_id, postal_code, target_count))
        before = len(self.client.values)
        self.client.add()
        return HarvestReport(
            marketplace_id=self.client.marketplace_id,
            postal_code=postal_code,
            requested=target_count,
            created=1,
            rejected=0,
            available_after=before + 1,
        )


class CookieProductionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_redacted_us_jp_receipt_through_maintenance(self) -> None:
        clients = {
            "default": FakeRedis("ATVPDKIKX0DER", "10001"),
            "overseas": FakeRedis("A1VC38T7YXB528", "140-0001"),
        }
        harvesters = {
            pool: FakeHarvester(client) for pool, client in clients.items()
        }
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        times = iter((0.0, 0.0, 0.0))
        receipt = await collect_cookie_production_receipt(
            targets=[
                CookieProductionTarget("default", "US", "10001"),
                CookieProductionTarget("overseas", "JP", "140-0001"),
            ],
            harvesters=harvesters,
            clients=clients,
            authorization_reference="approved-cookie-production-1",
            project_root=PROJECT_ROOT,
            minimum_target_interval_seconds=10,
            sleeper=sleeper,
            clock=lambda: next(times),
        )

        self.assertEqual([item["marketplace_id"] for item in receipt["operations"]], ["US", "JP"])
        self.assertEqual(receipt["operations"][0]["postal_code_hash"], hashlib.sha256(b"10001").hexdigest())
        self.assertTrue(receipt["checks"]["maintenance_run_once_passed"])
        self.assertTrue(receipt["checks"]["cookie_values_redacted"])
        self.assertEqual(sleeps, [10.0])
        self.assertNotIn("10001", str(receipt))
        self.assertNotIn("session-id", str(receipt))
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "receipt.json"
            _atomic_write_outside_project(output, receipt, project_root=PROJECT_ROOT)
            self.assertEqual(_validate_cookie_production_evidence(output), [])

    async def test_rejects_wrong_scope_or_fast_interval_before_writes(self) -> None:
        clients = {
            "default": FakeRedis("ATVPDKIKX0DER", "10001"),
            "overseas": FakeRedis("A1VC38T7YXB528", "140-0001"),
        }
        harvesters = {
            pool: FakeHarvester(client) for pool, client in clients.items()
        }
        with self.assertRaisesRegex(ControlledEvidenceError, "exactly default/US"):
            await collect_cookie_production_receipt(
                targets=[CookieProductionTarget("default", "US", "10001")],
                harvesters=harvesters,
                clients=clients,
                authorization_reference="approval",
                project_root=PROJECT_ROOT,
            )
        with self.assertRaisesRegex(ControlledEvidenceError, "at least 10"):
            await collect_cookie_production_receipt(
                targets=[
                    CookieProductionTarget("default", "US", "10001"),
                    CookieProductionTarget("overseas", "JP", "140-0001"),
                ],
                harvesters=harvesters,
                clients=clients,
                authorization_reference="approval",
                project_root=PROJECT_ROOT,
                minimum_target_interval_seconds=9,
            )
        self.assertEqual(harvesters["default"].calls, [])
        self.assertEqual(harvesters["overseas"].calls, [])

    async def test_raw_receipt_cannot_be_written_inside_project(self) -> None:
        with self.assertRaisesRegex(ControlledEvidenceError, "outside the project"):
            _atomic_write_outside_project(
                PROJECT_ROOT / "forbidden-cookie-receipt.json",
                {},
                project_root=PROJECT_ROOT,
            )

    def test_live_attempt_limit_is_strictly_bounded(self) -> None:
        with self.assertRaisesRegex(ControlledEvidenceError, "between 1 and 5"):
            _build_resources(object(), max_attempts_per_cookie=0)
        with self.assertRaisesRegex(ControlledEvidenceError, "between 1 and 5"):
            _build_resources(object(), max_attempts_per_cookie=6)
