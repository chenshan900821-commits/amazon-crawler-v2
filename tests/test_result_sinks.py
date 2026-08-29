from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amazon_crawler.application.delivery_worker import DeliveryWorker
from amazon_crawler.application.service import CrawlerService
from amazon_crawler.domain.errors import ConflictError, ValidationError
from amazon_crawler.domain.models import ClaimedDelivery, CrawlResult, NormalizedInput
from amazon_crawler.infra.legacy_compat import LegacyRedisResultBuffer
from amazon_crawler.infra.result_sinks import (
    JsonlResultSink,
    ResultSinkRegistry,
)
from amazon_crawler.infra.sqlite_store import SQLiteStore
from amazon_crawler.plugins.registry import PluginRegistry


class FakePlugin:
    kind = "amazon.product"
    base_kind = "product"

    def normalize(self, source, marketplace_id, postal_code):
        asin = str(source).upper()
        return NormalizedInput(
            asin=asin,
            marketplace_id=marketplace_id or "US",
            source=str(source),
            product_url=f"https://www.amazon.com/dp/{asin}",
            postal_code=postal_code,
        )


class ExplodingSink:
    name = "exploding"
    description = "test sink"

    def publish(self, delivery):
        raise RuntimeError("cookie=session-secret; mysql://user:pass@localhost")


class FakeEvalRedis:
    def __init__(self) -> None:
        self.markers: set[str] = set()
        self.values: dict[str, list[bytes]] = {}

    def eval(self, script, numkeys, *keys_and_args):
        self.asserted_numkeys = numkeys
        marker, result_key, dimension_key = keys_and_args[:3]
        args = list(keys_and_args[3:])
        if marker in self.markers:
            return [0, 0, 1]
        result_count = int(args[1])
        cursor = 2
        result_values = args[cursor : cursor + result_count]
        cursor += result_count
        dimension_count = int(args[cursor])
        cursor += 1
        dimension_values = args[cursor : cursor + dimension_count]
        self.values.setdefault(result_key, []).extend(result_values)
        self.values.setdefault(dimension_key, []).extend(dimension_values)
        self.markers.add(marker)
        return [result_count, dimension_count, 0]


def delivery(**overrides) -> ClaimedDelivery:
    values = {
        "id": "delivery_test",
        "result_id": "result_test",
        "job_id": "job_test",
        "item_id": "item_test",
        "sink_name": "jsonl",
        "kind": "amazon.product",
        "schema_version": "amazon.product.v1",
        "data": {"data_asin": "B000000001", "title": "Fixture"},
        "evidence": {"sha256": "a" * 64},
        "collected_at": "2026-08-23T00:00:00+00:00",
        "attempts": 1,
        "max_attempts": 3,
        "lease_owner": "worker:0",
        "lease_token": "fixture-delivery-lease-token",
    }
    values.update(overrides)
    return ClaimedDelivery(**values)


class ResultSinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = SQLiteStore(self.root / "state.db", delivery_max_attempts=2)
        self.store.initialize()
        self.registry = PluginRegistry([FakePlugin()])

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def _completed_job(
        self,
        sink_names: list[str],
        asin: str = "B000000001",
    ) -> tuple[str, ClaimedDelivery | None]:
        service = CrawlerService(
            self.store,
            self.registry,
            result_sinks={"sqlite", "jsonl", "exploding"},
        )
        job, _ = service.create_job(
            inputs=[asin],
            marketplace_id="US",
            options={"result_sinks": sink_names},
            external_result_write_authorized=True,
        )
        item = self.store.claim_next("crawl-worker", 15)
        self.assertIsNotNone(item)
        self.store.complete_item(
            item,
            CrawlResult(
                data={"data_asin": "B000000001", "title": "Fixture"},
                evidence={"sha256": "a" * 64},
            ),
        )
        return job["id"], None

    async def test_unconfigured_sink_is_rejected_before_job_creation(self) -> None:
        service = CrawlerService(
            self.store, self.registry, result_sinks={"sqlite", "jsonl"}
        )
        with self.assertRaisesRegex(ValidationError, "not configured"):
            service.create_job(
                inputs=["B000000001"],
                options={"result_sinks": ["sqlite", "legacy_mysql"]},
            )
        self.assertEqual(self.store.list_jobs(), [])

    async def test_result_and_outbox_rows_are_committed_together(self) -> None:
        job_id, _ = self._completed_job(["sqlite", "jsonl"])
        self.assertEqual(self.store.get_job(job_id)["status"], "succeeded")
        self.assertEqual(len(self.store.list_results(job_id)), 1)
        deliveries = self.store.list_deliveries(job_id)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["sink_name"], "jsonl")
        self.assertEqual(deliveries[0]["status"], "pending")

    async def test_jsonl_delivery_is_deduplicated_and_marked_delivered(self) -> None:
        job_id, _ = self._completed_job(["jsonl"])
        sinks = ResultSinkRegistry([JsonlResultSink(self.root / "jsonl")])
        worker = DeliveryWorker(
            store=self.store,
            sinks=sinks,
            lease_seconds=15,
            poll_seconds=0.01,
            concurrency=1,
            worker_id="delivery-worker",
        )
        self.assertEqual(await worker.run_until_idle(), 1)
        record_file = self.root / "jsonl" / "amazon.product.jsonl"
        records = [json.loads(line) for line in record_file.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["result_id"], self.store.list_results(job_id)[0]["id"]
        )
        self.assertEqual(self.store.list_deliveries(job_id)[0]["status"], "delivered")

        sink = sinks.get("jsonl")
        first = sink.publish(delivery(id=records[0]["delivery_id"]))
        second = sink.publish(delivery(id=records[0]["delivery_id"]))
        self.assertTrue(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(len(record_file.read_text().splitlines()), 1)
        self.assertEqual(os.stat(self.root / "jsonl").st_mode & 0o777, 0o700)
        self.assertEqual(
            os.stat(self.root / "jsonl" / ".receipts").st_mode & 0o777, 0o700
        )
        self.assertEqual(os.stat(record_file).st_mode & 0o777, 0o600)

    async def test_job_scoped_delivery_does_not_consume_other_outbox_rows(self) -> None:
        other_job_id, _ = self._completed_job(["jsonl"], "B000000001")
        target_job_id, _ = self._completed_job(["jsonl"], "B000000002")
        sinks = ResultSinkRegistry([JsonlResultSink(self.root / "jsonl-scoped")])
        worker = DeliveryWorker(
            store=self.store,
            sinks=sinks,
            lease_seconds=15,
            poll_seconds=0.01,
            concurrency=1,
            worker_id="scoped-delivery-worker",
        )

        self.assertEqual(
            await worker.run_until_idle(job_id=target_job_id),
            1,
        )
        self.assertEqual(
            self.store.list_deliveries(target_job_id)[0]["status"],
            "delivered",
        )
        self.assertEqual(
            self.store.list_deliveries(other_job_id)[0]["status"],
            "pending",
        )

    async def test_jsonl_sink_refuses_symlinked_result_and_receipt_targets(
        self,
    ) -> None:
        external = self.root / "external.txt"
        external.write_text("unchanged", encoding="utf-8")
        result_root = self.root / "jsonl-symlink-result"
        result_root.mkdir()
        destination = result_root / "amazon.product.jsonl"
        try:
            destination.symlink_to(external)
        except OSError as exc:  # pragma: no cover - platform capability
            self.skipTest(f"symbolic links are unavailable: {exc}")
        with self.assertRaisesRegex(RuntimeError, "symbolic links"):
            JsonlResultSink(result_root).publish(delivery())
        self.assertEqual(external.read_text(encoding="utf-8"), "unchanged")

        receipt_root = self.root / "jsonl-symlink-receipts"
        receipt_root.mkdir()
        (receipt_root / ".receipts").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
            JsonlResultSink(receipt_root).publish(delivery())
        self.assertEqual(external.read_text(encoding="utf-8"), "unchanged")

    async def test_delivery_failure_retries_without_persisting_secret_text(
        self,
    ) -> None:
        job_id, _ = self._completed_job(["exploding"])
        worker = DeliveryWorker(
            store=self.store,
            sinks=ResultSinkRegistry([ExplodingSink()]),
            lease_seconds=15,
            poll_seconds=0.01,
            concurrency=1,
            worker_id="delivery-worker",
        )
        self.assertTrue(await worker.process_one())
        stored = str(self.store.list_deliveries(job_id))
        self.assertIn("result sink delivery failed (RuntimeError)", stored)
        self.assertNotIn("session-secret", stored)
        self.assertNotIn("user:pass", stored)

    async def test_expired_delivery_lease_is_recovered_and_stale_ack_rejected(
        self,
    ) -> None:
        self._completed_job(["jsonl"])
        stale = self.store.claim_delivery("dead-worker", 15)
        self.assertIsNotNone(stale)
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE result_outbox SET lease_expires_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), stale.id),
            )
        self.assertEqual(self.store.recover_expired_delivery_leases(), 1)
        current = self.store.claim_delivery("dead-worker", 15)
        self.assertEqual(current.id, stale.id)
        self.assertNotEqual(current.lease_token, stale.lease_token)
        with self.assertRaises(ConflictError):
            self.store.complete_delivery(stale, {"ok": True})
        self.store.complete_delivery(current, {"ok": True})

    async def test_legacy_redis_publish_once_uses_atomic_delivery_marker(self) -> None:
        client = FakeEvalRedis()
        writer = LegacyRedisResultBuffer(client)
        payload = {"data": {"items": [{"data_asin": "B000000001"}]}}
        first = writer.publish_once("delivery-1", "search_jp", payload)
        second = writer.publish_once("delivery-1", "search_jp", payload)
        self.assertEqual(first["result_rows"], 1)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(
            len(client.values["amazon_search_jp_items_buffer"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
