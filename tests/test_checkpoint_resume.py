from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amazon_crawler.application.scoped_runner import run_scoped_job
from amazon_crawler.application.service import CrawlerService
from amazon_crawler.application.worker import Worker, _public_failure_details
from amazon_crawler.domain.errors import ConflictError
from amazon_crawler.domain.models import (
    CrawlResult,
    FollowupJob,
    NormalizedInput,
    PluginOutcome,
)
from amazon_crawler.infra.sqlite_store import SQLiteStore
from amazon_crawler.plugins.registry import PluginRegistry


class FakeProductPlugin:
    kind = "amazon.product"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def normalize(self, source: str, marketplace_id: str | None, postal_code: str | None):
        market = (marketplace_id or "US").upper()
        asin = source.upper()
        return NormalizedInput(
            asin=asin,
            marketplace_id=market,
            source=source,
            product_url=f"https://www.amazon.com/dp/{asin}",
            postal_code=postal_code,
        )

    async def execute(self, item):
        self.calls.append(item.input["asin"])
        return PluginOutcome(
            result=CrawlResult(
                data={
                    "asin": item.input["asin"],
                    "title": f"Product {item.input['asin']}",
                    "parser_version": "fake/1",
                    "quality": {"core_field_coverage": 1.0},
                },
                evidence={"sha256": "test"},
            )
        )


class FakeDiscoveryPlugin:
    kind = "merchant_home"

    def normalize(self, source, marketplace_id, postal_code):
        payload = {"seller_id": str(source), "marketplace_id": marketplace_id or "US"}
        return NormalizedInput.from_payload(
            payload,
            input_key=f"merchant_home:{payload['marketplace_id']}:{source}",
            marketplace_id=payload["marketplace_id"],
            postal_code=postal_code,
            source=str(source),
        )

    async def execute(self, item):
        child = NormalizedInput(
            asin="B000000099",
            marketplace_id="US",
            source="B000000099",
            product_url="https://www.amazon.com/dp/B000000099",
        )
        return PluginOutcome(
            result=CrawlResult(
                data={"child_tasks": [{"asin": "B000000099"}], "row_count": 1},
                schema_version="test.discovery.v1",
                followup_jobs=(
                    FollowupJob(
                        kind="amazon.product",
                        inputs=(child,),
                        execution_mode=item.execution_mode.value,
                        max_attempts=item.max_attempts,
                        reason="test_discovery",
                    ),
                ),
            )
        )


class ExplodingProductPlugin(FakeProductPlugin):
    async def execute(self, item):
        raise RuntimeError("cookie=session-secret; proxy=http://user:pass@host")


class NoopDeliveryWorker:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    async def run_until_idle(self, *, job_id=None, **_kwargs) -> int:
        self.job_ids.append(job_id)
        return 0


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "crawler.db"
        self.store = SQLiteStore(self.db_path)
        self.store.initialize()
        self.plugin = FakeProductPlugin()
        self.registry = PluginRegistry([self.plugin])
        self.service = CrawlerService(self.store, self.registry)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def worker(self, store=None, plugin=None, worker_id="test-worker") -> Worker:
        plugin = plugin or self.plugin
        return Worker(
            store=store or self.store,
            plugins=PluginRegistry([plugin]),
            lease_seconds=15,
            poll_seconds=0.01,
            concurrency=1,
            worker_id=worker_id,
        )

    async def test_initialize_migrates_running_pre_token_leases_safely(self) -> None:
        legacy_path = Path(self.tempdir.name) / "pre-token.db"
        import sqlite3

        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE job_items (
                    id TEXT PRIMARY KEY, job_id TEXT, seq INTEGER,
                    status TEXT, available_at TEXT, lease_owner TEXT,
                    lease_expires_at TEXT, last_error_code TEXT, last_error TEXT
                );
                CREATE TABLE result_outbox (
                    id TEXT PRIMARY KEY, job_id TEXT, item_id TEXT,
                    status TEXT, attempts INTEGER, max_attempts INTEGER,
                    available_at TEXT, lease_owner TEXT, lease_expires_at TEXT,
                    last_error TEXT, created_at TEXT, updated_at TEXT
                );
                INSERT INTO job_items(
                    id, job_id, seq, status, available_at, lease_owner,
                    lease_expires_at
                ) VALUES ('old-item', 'old-job', 1, 'running', '', 'same-worker', '');
                INSERT INTO result_outbox(
                    id, job_id, item_id, status, attempts, max_attempts,
                    available_at, lease_owner, lease_expires_at, created_at, updated_at
                ) VALUES (
                    'old-delivery', 'old-job', 'old-item', 'running', 1, 8,
                    '', 'same-worker', '', '', ''
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        migrated = SQLiteStore(legacy_path)
        migrated.initialize()
        with migrated._connect() as connection:
            item_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(job_items)")
            }
            delivery_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(result_outbox)")
            }
            item = connection.execute(
                "SELECT status, lease_owner, lease_token FROM job_items WHERE id = 'old-item'"
            ).fetchone()
            delivery = connection.execute(
                "SELECT status, lease_owner, lease_token FROM result_outbox "
                "WHERE id = 'old-delivery'"
            ).fetchone()
        self.assertIn("lease_token", item_columns)
        self.assertIn("lease_token", delivery_columns)
        self.assertEqual(tuple(item), ("pending", None, None))
        self.assertEqual(tuple(delivery), ("pending", None, None))

    async def test_restart_resumes_pending_without_repeating_success(self) -> None:
        job, _ = self.service.create_job(
            inputs=["B000000001", "B000000002", "B000000003"], marketplace_id="US"
        )
        processed = await self.worker().run_until_idle(max_items=1)
        self.assertEqual(processed, 1)
        after_first = self.store.get_job(job["id"])
        self.assertEqual(after_first["checkpoint_seq"], 1)
        self.assertEqual(after_first["succeeded_items"], 1)

        reopened_store = SQLiteStore(self.db_path)
        reopened_store.initialize()
        restarted_plugin = FakeProductPlugin()
        processed_after_restart = await self.worker(
            store=reopened_store, plugin=restarted_plugin, worker_id="restarted"
        ).run_until_idle()
        self.assertEqual(processed_after_restart, 2)
        self.assertNotIn("B000000001", restarted_plugin.calls)
        completed = reopened_store.get_job(job["id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["checkpoint_seq"], 3)
        self.assertEqual(len(reopened_store.list_results(job["id"])), 3)

    async def test_job_scoped_worker_does_not_consume_other_queued_jobs(self) -> None:
        other, _ = self.service.create_job(
            inputs=["B000000001"],
            marketplace_id="US",
            priority=100,
        )
        target, _ = self.service.create_job(
            inputs=["B000000002"],
            marketplace_id="US",
            priority=-100,
        )

        processed = await self.worker().run_until_idle(job_id=target["id"])

        self.assertEqual(processed, 1)
        self.assertEqual(self.store.get_job(target["id"])["status"], "succeeded")
        self.assertEqual(self.store.get_job(other["id"])["status"], "pending")
        self.assertEqual(self.plugin.calls, ["B000000002"])

    async def test_scoped_runner_starts_worker_and_returns_terminal_result(self) -> None:
        job, _ = self.service.create_job(
            inputs=["B000000003"],
            marketplace_id="US",
        )
        delivery_worker = NoopDeliveryWorker()
        app = SimpleNamespace(
            store=self.store,
            worker=self.worker(),
            delivery_worker=delivery_worker,
            settings=SimpleNamespace(poll_seconds=0.01),
        )

        report = await run_scoped_job(
            app,
            job["id"],
            timeout_seconds=10,
        )

        self.assertTrue(report["completed"])
        self.assertEqual(report["job"]["status"], "succeeded")
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["runner"]["stopped_reason"], "terminal")
        self.assertTrue(report["runner"]["started"])
        self.assertFalse(report["runner"]["consumed_other_jobs"])
        self.assertEqual(delivery_worker.job_ids, [job["id"]])

    async def test_scoped_runner_follows_child_jobs_but_not_unrelated_jobs(self) -> None:
        discovery = FakeDiscoveryPlugin()
        registry = PluginRegistry([discovery, self.plugin])
        service = CrawlerService(self.store, registry)
        unrelated, _ = service.create_job(
            kind="amazon.product",
            inputs=["B000000088"],
            marketplace_id="US",
            priority=100,
        )
        parent, _ = service.create_job(
            kind="merchant_home",
            inputs=["SELLER123"],
            marketplace_id="US",
            priority=-100,
        )
        delivery_worker = NoopDeliveryWorker()
        app = SimpleNamespace(
            store=self.store,
            worker=Worker(
                store=self.store,
                plugins=registry,
                lease_seconds=15,
                poll_seconds=0.01,
                concurrency=1,
                worker_id="lineage-worker",
            ),
            delivery_worker=delivery_worker,
            settings=SimpleNamespace(poll_seconds=0.01),
        )

        report = await run_scoped_job(
            app,
            parent["id"],
            timeout_seconds=10,
        )

        self.assertTrue(report["completed"])
        self.assertEqual(report["lineage_status"], "succeeded")
        self.assertEqual(len(report["jobs"]), 2)
        self.assertTrue(all(job["status"] == "succeeded" for job in report["jobs"]))
        self.assertEqual(len(report["runner"]["scoped_job_ids"]), 2)
        self.assertEqual(self.store.get_job(unrelated["id"])["status"], "pending")
        child = next(job for job in report["jobs"] if job["id"] != parent["id"])
        self.assertEqual(child["options"]["root_job_id"], parent["id"])
        self.assertEqual(self.plugin.calls, ["B000000099"])

    async def test_expired_lease_is_recovered_and_stale_result_is_rejected(self) -> None:
        job, _ = self.service.create_job(inputs=["B000000001"], marketplace_id="US")
        stale_claim = self.store.claim_next("dead-worker", 15)
        self.assertIsNotNone(stale_claim)
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE job_items SET lease_expires_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), stale_claim.id),
            )
        self.assertEqual(self.store.recover_expired_leases(), 1)
        new_claim = self.store.claim_next("dead-worker", 15)
        self.assertEqual(new_claim.id, stale_claim.id)
        self.assertEqual(new_claim.attempts, 2)
        self.assertNotEqual(new_claim.lease_token, stale_claim.lease_token)
        with self.assertRaises(ConflictError):
            self.store.complete_item(stale_claim, CrawlResult(data={"title": "stale"}))
        self.store.complete_item(new_claim, CrawlResult(data={"title": "fresh"}))
        self.assertEqual(self.store.get_job(job["id"])["status"], "succeeded")

    async def test_pause_resume_and_idempotent_creation(self) -> None:
        first, first_created = self.service.create_job(
            inputs=["B000000001", "B000000002"], marketplace_id="US"
        )
        second, second_created = self.service.create_job(
            inputs=["B000000001", "B000000002"], marketplace_id="US"
        )
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["id"], second["id"])
        paused = self.store.request_pause(first["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertIsNone(self.store.claim_next("worker", 15))
        resumed = self.store.resume(first["id"])
        self.assertEqual(resumed["status"], "pending")
        await self.worker().run_until_idle()
        self.assertEqual(self.store.get_job(first["id"])["status"], "succeeded")

    async def test_cancel_marks_unstarted_items_and_preserves_terminal_state(self) -> None:
        job, _ = self.service.create_job(
            inputs=["B000000001", "B000000002"], marketplace_id="US"
        )
        cancelled = self.store.request_cancel(job["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["cancelled_items"], 2)
        self.assertIsNone(self.store.claim_next("worker", 15))

    async def test_unexpected_worker_error_never_persists_secret_text(self) -> None:
        plugin = ExplodingProductPlugin()
        service = CrawlerService(self.store, PluginRegistry([plugin]))
        job, _ = service.create_job(
            inputs=["B000000001"],
            marketplace_id="US",
            max_attempts=1,
        )
        await self.worker(plugin=plugin).run_until_idle()
        rendered = str(self.store.get_job(job["id"]))
        self.assertIn("worker internal error (RuntimeError)", rendered)
        self.assertNotIn("session-secret", rendered)
        self.assertNotIn("user:pass", rendered)

    async def test_failure_detail_allowlist_rejects_plugin_secret_fields(self) -> None:
        details = _public_failure_details(
            {
                "error_type": "FixtureError",
                "http_status": 503,
                "cookie": "session-secret",
                "message": "proxy=http://user:pass@host",
                "evidence": {
                    "sha256": "a" * 64,
                    "bytes": 100,
                    "captured": False,
                    "artifact_ref": "job-safe/item-safe.html",
                    "path": "/Users/private/evidence.html",
                    "cookie_header": "session-secret",
                },
            }
        )
        self.assertEqual(details["error_type"], "FixtureError")
        self.assertEqual(details["http_status"], 503)
        self.assertEqual(details["evidence"]["sha256"], "a" * 64)
        self.assertEqual(
            details["evidence"]["artifact_ref"],
            "job-safe/item-safe.html",
        )
        rendered = str(details)
        self.assertNotIn("session-secret", rendered)
        self.assertNotIn("user:pass", rendered)
        self.assertNotIn("/Users/private", rendered)

    async def test_followup_job_is_committed_with_parent_result_and_runs(self) -> None:
        discovery = FakeDiscoveryPlugin()
        registry = PluginRegistry([discovery, self.plugin])
        service = CrawlerService(self.store, registry)
        parent, _ = service.create_job(
            kind="merchant_home",
            inputs=["SELLER123"],
            marketplace_id="US",
        )
        worker = Worker(
            store=self.store,
            plugins=registry,
            lease_seconds=15,
            poll_seconds=0.01,
            concurrency=1,
            worker_id="followup-worker",
        )
        self.assertEqual(await worker.run_until_idle(), 2)
        jobs = self.store.list_jobs(limit=10)
        self.assertEqual(len(jobs), 2)
        child = next(job for job in jobs if job["id"] != parent["id"])
        self.assertEqual(child["kind"], "amazon.product")
        self.assertEqual(child["status"], "succeeded")
        self.assertEqual(child["options"]["parent_job_id"], parent["id"])
        events = self.store.list_events(parent["id"], limit=20)
        followup_events = [event for event in events if event["event_type"] == "job.followup_created"]
        self.assertEqual(len(followup_events), 1)
        self.assertEqual(followup_events[0]["payload"]["child_job_id"], child["id"])


if __name__ == "__main__":
    unittest.main()
