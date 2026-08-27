from __future__ import annotations

import json
import tempfile
import unittest
import zlib
from pathlib import Path

from amazon_crawler.application.service import CrawlerService
from amazon_crawler.infra.legacy_compat import (
    LEGACY_TASKS,
    LegacyRedisResultBuffer,
    LegacyMySQLResultWriter,
    LegacyTaskImporter,
    SQLAlchemyLegacyGateway,
    assert_legacy_job_compatibility,
    legacy_state_for,
    project_legacy_result,
)
from amazon_crawler.infra.sqlite_store import SQLiteStore
from amazon_crawler.plugins.amazon_parser import LEGACY_PRODUCT_FIELDS
from amazon_crawler.plugins.amazon_collections import AmazonCollectionPlugin
from amazon_crawler.plugins.amazon_product import AmazonProductPlugin
from amazon_crawler.plugins.registry import PluginRegistry
from scripts.inspect_legacy_failure_candidates import safe_candidates


class FakeTaskSource:
    def __init__(self, rows):
        self.rows = rows

    def fetch_pending(self, task_name: str, limit: int):
        return self.rows[:limit]


class FakeRedisBuffer:
    def __init__(self) -> None:
        self.values: dict[str, list[bytes]] = {}

    def rpush(self, key: str, *values: bytes) -> int:
        self.values.setdefault(key, []).extend(values)
        return len(self.values[key])


class FakeLegacyGateway:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {}

    def insert_rows(self, table_name: str, rows: list[dict]) -> int:
        self.rows.setdefault(table_name, []).extend(rows)
        return len(rows)


class LegacyCompatTests(unittest.TestCase):
    def test_historical_candidates_export_only_allowlisted_task_inputs(self) -> None:
        rendered = json.dumps(
            safe_candidates(
                "asin_list_jp",
                [
                    {
                        "id": 7,
                        "state": -4,
                        "category_id": "123",
                        "market_id": "ATVPDKIKX0DER",
                        "post_code": "10001",
                        "page": 2,
                        "cookie": "secret-cookie",
                        "proxy_url": "http://secret-proxy",
                        "password": "secret-password",
                    }
                ],
            )
        )
        self.assertIn('"category_id": "123"', rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn('"state"', rendered)

    def test_mysql_legacy_writes_preserve_insert_ignore_semantics(self) -> None:
        import sqlalchemy
        from sqlalchemy.dialects import mysql

        metadata = sqlalchemy.MetaData()
        table = sqlalchemy.Table(
            "amazon_search_product",
            metadata,
            sqlalchemy.Column("data_asin", sqlalchemy.String(10), primary_key=True),
        )
        statement = SQLAlchemyLegacyGateway._insert_statement(table, "mysql")
        rendered = str(statement.compile(dialect=mysql.dialect()))

        self.assertIn("INSERT IGNORE INTO amazon_search_product", rendered)

    def test_historical_candidate_query_is_read_only_bounded_and_state_filtered(self) -> None:
        import sqlalchemy

        with tempfile.TemporaryDirectory() as tempdir:
            gateway = SQLAlchemyLegacyGateway(
                f"sqlite:///{Path(tempdir) / 'legacy.db'}"
            )
            try:
                with gateway._engine.begin() as connection:
                    connection.execute(
                        sqlalchemy.text(
                            "CREATE TABLE amazon_category_asin_list_task ("
                            "id INTEGER PRIMARY KEY, state INTEGER, category_id TEXT, "
                            "market_id TEXT, post_code TEXT, page INTEGER)"
                        )
                    )
                    connection.execute(
                        sqlalchemy.text(
                            "INSERT INTO amazon_category_asin_list_task "
                            "(id, state, category_id, market_id, post_code, page) VALUES "
                            "(1, -4, 'empty-old', 'ATVPDKIKX0DER', '10001', 1), "
                            "(2, 1, 'successful', 'ATVPDKIKX0DER', '10001', 1), "
                            "(3, -4, 'empty-new', 'ATVPDKIKX0DER', '10001', 2)"
                        )
                    )
                rows = gateway.fetch_by_states("asin_list_jp", [-4], limit=1)
            finally:
                gateway.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 3)
        self.assertEqual(rows[0]["category_id"], "empty-new")

    def test_all_legacy_task_tables_and_v2_kinds_are_explicit(self) -> None:
        self.assertEqual(
            LEGACY_TASKS,
            {
                "search_jp": ("amazon_search_product_jp_task_boost", "amazon_search_product", "search"),
                "search_hour_jp": ("amazon_search_product_task_hour", "amazon_search_product", "search_hour"),
                "product_jp": ("amazon_product_details_jp_task_boost", "amazon_product_details", "product"),
                "product_hw_jp": ("amazon_product_details_jp_task_boost_hw", "amazon_product_details", "product_hw"),
                "merchant": ("amazon_merchant_detail_task_boost", "amazon_merchant_detail", "merchant"),
                "asin_list_jp": ("amazon_category_asin_list_task", "amazon_category_asin_list", "category_asin_list"),
                "rank_list_jp": ("amazon_rank_list_product_jp_task_boost", "amazon_rank_list_product", "rank_list"),
                "product_time_jp": ("amazon_product_details_time_task", "amazon_product_details_time", "product_time"),
                "reviews": ("amazon_reviews_task", "amazon_reviews", "reviews"),
                "merchant_home": ("amazon_merchant_products_origin_task", "amazon_merchant_products_task", "merchant_home"),
                "merchant_products": ("amazon_merchant_products_task", "amazon_merchant_products", "merchant_products"),
            },
        )

    def test_every_result_shape_has_a_legacy_projection(self) -> None:
        row = {"data_asin": "B000000001"}
        for task_name in {
            "search_jp",
            "search_hour_jp",
            "asin_list_jp",
            "rank_list_jp",
            "reviews",
        }:
            projection = project_legacy_result(task_name, {"data": {"items": [row]}})
            self.assertEqual(projection.result_rows, (row,), task_name)
        merchant = project_legacy_result(
            "merchant", {"data": {"item": {"seller_id": "SELLER123"}}}
        )
        self.assertEqual(merchant.result_rows[0]["seller_id"], "SELLER123")
        home = project_legacy_result(
            "merchant_home", {"data": {"child_tasks": [{"page": 1}]}}
        )
        self.assertEqual(home.child_task_rows, ({"page": 1},))
        products = project_legacy_result(
            "merchant_products",
            {
                "data": {
                    "items": [row],
                    "product_detail_tasks": [{"asin": "B000000001"}],
                }
            },
        )
        self.assertEqual(products.result_rows, (row,))
        self.assertEqual(products.product_task_rows[0]["asin"], "B000000001")

    def test_explicit_status_mapping(self) -> None:
        self.assertEqual(legacy_state_for("succeeded"), 1)
        self.assertEqual(legacy_state_for("running"), 0)
        self.assertEqual(legacy_state_for("failed", "product_not_found"), -3)
        self.assertEqual(legacy_state_for("failed", "missing_product_identity"), -2)
        self.assertEqual(legacy_state_for("failed", "cookie_unavailable"), -2)
        self.assertEqual(legacy_state_for("failed", "one_page_only"), -4)
        self.assertEqual(legacy_state_for("failed", "no_reviews"), -4)
        self.assertEqual(legacy_state_for("failed", "no_category_results"), -4)
        self.assertEqual(legacy_state_for("failed", "no_rank_results"), -4)
        self.assertEqual(legacy_state_for("cancelled"), -5)
        self.assertEqual(legacy_state_for("failed", "network_error"), -1)

    def test_product_projection_splits_main_and_dimensions(self) -> None:
        data = {field: f"value-{field}" for field in LEGACY_PRODUCT_FIELDS}
        data["dimension_items"] = [
            {
                "id": "dimension-1",
                "task_id": "task-1",
                "market_place_id": "ATVPDKIKX0DER",
                "parent_asin": "B000000000",
                "asin": "B000000001",
                "dimensions": [{"name": "Color", "value": "Red"}],
                "created_at": "2026-08-22 00:00:00",
                "image_url": "https://www.amazon.com/dp/B000000001",
            }
        ]
        projection = project_legacy_result("product_jp", {"data": data})
        self.assertEqual(len(projection.result_rows), 1)
        self.assertEqual(set(projection.result_rows[0]), set(LEGACY_PRODUCT_FIELDS))
        self.assertEqual(projection.result_rows[0]["is_video"], "value-is_video")
        self.assertEqual(len(projection.dimension_rows), 1)
        self.assertNotIn("id", projection.dimension_rows[0])

    def test_legacy_writes_require_matching_job_kind_and_imported_id(self) -> None:
        job = {
            "kind": "product_jp",
            "items": [{"input": {"id": "42", "asin": "B000000001"}}],
        }
        assert_legacy_job_compatibility("product_jp", job)
        assert_legacy_job_compatibility("product_jp", job, legacy_task_id=42)
        with self.assertRaisesRegex(ValueError, "requires a 'search' V2 job"):
            assert_legacy_job_compatibility("search_jp", job)
        with self.assertRaisesRegex(ValueError, "does not match"):
            assert_legacy_job_compatibility(
                "product_jp", job, legacy_task_id="different"
            )

    def test_redis_buffer_matches_legacy_keys_and_compression(self) -> None:
        client = FakeRedisBuffer()
        buffer = LegacyRedisResultBuffer(client)
        result = {
            "data": {
                "items": [
                    {"data_asin": "B000000001", "keyword": "mouse"},
                    {"data_asin": "B000000002", "keyword": "mouse"},
                ]
            }
        }
        counts = buffer.publish("search_jp", result)
        self.assertEqual(counts["result_rows"], 2)
        payload = json.loads(
            zlib.decompress(client.values["amazon_search_jp_items_buffer"][0])
        )
        self.assertEqual(payload["data_asin"], "B000000001")

    def test_merchant_product_tasks_route_jp_to_hardware_table(self) -> None:
        gateway = FakeLegacyGateway()
        writer = LegacyMySQLResultWriter(gateway)
        result = {
            "data": {
                "items": [],
                "product_detail_tasks": [
                    {"market_id": "ATVPDKIKX0DER", "asin": "B000000001"},
                    {"market_id": "A1VC38T7YXB528", "asin": "B000000002"},
                ],
            }
        }
        counts = writer.publish("merchant_products", result)
        self.assertEqual(counts["product_task_rows"], 1)
        self.assertEqual(counts["product_hw_task_rows"], 1)
        self.assertEqual(
            gateway.rows["amazon_product_details_jp_task_boost"][0]["asin"],
            "B000000001",
        )
        self.assertEqual(
            gateway.rows["amazon_product_details_jp_task_boost_hw"][0]["asin"],
            "B000000002",
        )

    def test_read_only_import_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = SQLiteStore(Path(tempdir) / "state.db")
            store.initialize()
            plugin = object.__new__(AmazonProductPlugin)
            plugin.kind = "product"
            plugin.base_kind = "product"
            service = CrawlerService(store, PluginRegistry([plugin]))
            source = FakeTaskSource(
                [
                    {
                        "id": 10,
                        "market_id": "ATVPDKIKX0DER",
                        "asin": "B000000001",
                        "post_code": "10001",
                        "add_date": "2026-08-22",
                    }
                ]
            )
            importer = LegacyTaskImporter(source, service)
            first = importer.import_pending("product_jp")
            second = importer.import_pending("product_jp")
            self.assertEqual(first.created, 1)
            self.assertEqual(second.existing, 1)
            self.assertEqual(first.job_ids, second.job_ids)

    def test_legacy_specialized_product_routes_keep_execution_mode(self) -> None:
        cases = {
            "product_hw_jp": ("product_hw", "overseas", 0),
            "product_time_jp": ("product_time", "realtime", 50),
        }
        for task_name, (kind, mode, priority) in cases.items():
            with self.subTest(task_name=task_name), tempfile.TemporaryDirectory() as tempdir:
                store = SQLiteStore(Path(tempdir) / "state.db")
                store.initialize()
                plugin = object.__new__(AmazonProductPlugin)
                plugin.kind = kind
                plugin.base_kind = kind
                service = CrawlerService(store, PluginRegistry([plugin]))
                source = FakeTaskSource(
                    [
                        {
                            "id": 10,
                            "market_id": "A1VC38T7YXB528",
                            "asin": "B000000001",
                            "post_code": "100-0001",
                            "add_date": "2026-08-22",
                        }
                    ]
                )

                report = LegacyTaskImporter(source, service).import_pending(task_name)
                job = store.get_job(report.job_ids[0])

                self.assertEqual(job["execution_mode"], mode)
                self.assertEqual(job["priority"], priority)
                self.assertEqual(job["items"][0]["max_attempts"], 5)

    def test_hourly_legacy_import_keeps_eleven_total_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = SQLiteStore(Path(tempdir) / "state.db")
            store.initialize()
            plugin = object.__new__(AmazonCollectionPlugin)
            plugin.kind = "search_hour"
            plugin.base_kind = "search_hour"
            service = CrawlerService(store, PluginRegistry([plugin]))
            source = FakeTaskSource(
                [
                    {
                        "id": 10,
                        "market_id": "ATVPDKIKX0DER",
                        "keyword": "wireless mouse",
                        "post_code": "10001",
                        "turn_page": 1,
                        "frequent": 0,
                        "data_hour": "2026-08-22 09:00:00",
                    }
                ]
            )

            report = LegacyTaskImporter(source, service).import_pending(
                "search_hour_jp"
            )
            job = store.get_job(report.job_ids[0])

            self.assertEqual(job["items"][0]["max_attempts"], 11)


if __name__ == "__main__":
    unittest.main()
