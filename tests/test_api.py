from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from starlette.testclient import TestClient

from amazon_crawler.bootstrap import build_application
from amazon_crawler.config import Settings
from amazon_crawler.domain.resources import HarvestReport
from amazon_crawler.interfaces.api import create_app


class FakeCookieHarvester:
    backend = "curl_cffi"
    tls_impersonation = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def ensure_capacity(
        self, marketplace_id: str, postal_code: str, target_count: int
    ) -> HarvestReport:
        self.calls.append((marketplace_id, postal_code, target_count))
        return HarvestReport(
            marketplace_id="ATVPDKIKX0DER",
            postal_code=postal_code,
            requested=target_count,
            created=2,
            rejected=1,
            available_after=target_count,
            failure_codes=("blocked_page",),
        )


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = replace(
            Settings.from_env(root),
            db_path=root / "api.db",
            worker_enabled=False,
        )
        self.application = build_application(self.settings)
        self.client_context = TestClient(create_app(self.application))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tempdir.cleanup()

    def test_create_is_idempotent_and_controls_are_exposed(self) -> None:
        payload = {
            "inputs": ["B000000001", "B000000002"],
            "marketplace_id": "US",
            "execution_mode": "realtime",
        }
        first = self.client.post("/api/v1/jobs", json=payload)
        self.assertEqual(first.status_code, 201)
        first_body = first.json()
        self.assertTrue(first_body["created"])
        self.assertEqual(first_body["job"]["priority"], 50)
        self.assertTrue(
            all(item["max_attempts"] == 5 for item in first_body["job"]["items"])
        )

        second = self.client.post("/api/v1/jobs", json=payload)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertEqual(second.json()["job"]["id"], first_body["job"]["id"])

        job_id = first_body["job"]["id"]
        paused = self.client.post(f"/api/v1/jobs/{job_id}/pause")
        self.assertEqual(paused.json()["job"]["status"], "paused")
        resumed = self.client.post(f"/api/v1/jobs/{job_id}/resume")
        self.assertEqual(resumed.json()["job"]["status"], "pending")

    def test_product_time_creates_new_observations_unless_retry_key_is_given(self) -> None:
        payload = {
            "kind": "product_time",
            "inputs": ["B000000001"],
            "marketplace_id": "US",
            "execution_mode": "realtime",
        }
        first = self.client.post("/api/v1/jobs", json=payload)
        second = self.client.post("/api/v1/jobs", json=payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.json()["job"]["id"], second.json()["job"]["id"])

        keyed = {**payload, "idempotency_key": "product-time-retry-1"}
        keyed_first = self.client.post("/api/v1/jobs", json=keyed)
        keyed_second = self.client.post("/api/v1/jobs", json=keyed)
        self.assertEqual(keyed_first.status_code, 201)
        self.assertEqual(keyed_second.status_code, 200)
        self.assertEqual(
            keyed_first.json()["job"]["id"], keyed_second.json()["job"]["id"]
        )

    def test_search_hour_does_not_collapse_different_observation_hours(self) -> None:
        response = self.client.post(
            "/api/v1/jobs",
            json={
                "kind": "search_hour",
                "marketplace_id": "US",
                "inputs": [
                    {
                        "keyword": "wireless mouse",
                        "market_id": "US",
                        "turn_page": 1,
                        "data_hour": "2026-08-22 09:00:00",
                    },
                    {
                        "keyword": "wireless mouse",
                        "market_id": "US",
                        "turn_page": 1,
                        "data_hour": "2026-08-22 10:00:00",
                    },
                ],
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["job"]["total_items"], 2)

    def test_api_applies_legacy_retry_defaults_and_accepts_override(self) -> None:
        response = self.client.post(
            "/api/v1/jobs",
            json={
                "kind": "search_hour",
                "marketplace_id": "US",
                "inputs": [
                    {
                        "keyword": "wireless mouse",
                        "market_id": "US",
                        "turn_page": 1,
                        "data_hour": "2026-08-22 09:00:00",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["job"]["items"][0]["max_attempts"], 11)

        overridden = self.client.post(
            "/api/v1/jobs",
            json={
                "kind": "search",
                "marketplace_id": "US",
                "max_attempts": 2,
                "inputs": [
                    {
                        "keyword": "explicit retry override",
                        "market_id": "US",
                        "turn_page": 1,
                    }
                ],
            },
        )
        self.assertEqual(overridden.status_code, 201)
        self.assertEqual(
            overridden.json()["job"]["items"][0]["max_attempts"], 2
        )

    def test_rejects_non_amazon_urls_and_unknown_fields(self) -> None:
        bad_url = self.client.post(
            "/api/v1/jobs",
            json={"inputs": ["https://example.com/dp/B000000001"]},
        )
        self.assertEqual(bad_url.status_code, 422)
        with_secret = self.client.post(
            "/api/v1/jobs",
            json={"inputs": ["B000000001"], "marketplace_id": "US", "cookie": "secret"},
        )
        self.assertEqual(with_secret.status_code, 422)
        self.assertNotIn("secret", with_secret.text.lower())
        self.assertEqual(
            with_secret.json()["error"]["message"],
            "request validation failed",
        )

        product_with_query_secret = self.client.post(
            "/api/v1/jobs",
            json={
                "kind": "product",
                "marketplace_id": "US",
                "inputs": [
                    "https://www.amazon.com/dp/B000000001?session_token=do-not-store"
                ],
            },
        )
        self.assertEqual(product_with_query_secret.status_code, 201)
        product_job = product_with_query_secret.json()["job"]
        self.assertNotIn("do-not-store", str(product_job))
        self.assertEqual(
            product_job["items"][0]["input"]["source"],
            "https://www.amazon.com/dp/B000000001",
        )

    def test_persisted_rank_and_merchant_urls_reject_sensitive_query_keys(self) -> None:
        cases = [
            {
                "kind": "rank_list",
                "inputs": [
                    {
                        "market_id": "US",
                        "category_id": "100",
                        "url_type": "bestsellers",
                        "url": "https://www.amazon.com/bestsellers?session_token=do-not-store",
                    }
                ],
            },
            {
                "kind": "merchant_home",
                "inputs": [
                    {
                        "market_id": "US",
                        "seller_id": "SELLER123",
                        "object_url": "https://www.amazon.com/s?credential=do-not-store",
                    }
                ],
            },
        ]
        for payload in cases:
            with self.subTest(kind=payload["kind"]):
                response = self.client.post("/api/v1/jobs", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("do-not-store", response.text)

    def test_health_exposes_counts_but_no_runtime_secrets(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("cookie", payload["resources"])
        self.assertIn("available", payload["resources"]["cookie"])
        self.assertIn("cookie_routes", payload["resources"])
        self.assertIn("default", payload["resources"]["cookie_routes"])
        self.assertIn("backend", payload["resources"]["transport"])
        self.assertIn("tls_impersonation", payload["resources"]["transport"])
        rendered = response.text.lower()
        self.assertNotIn("cookie_header", rendered)
        self.assertNotIn("proxy_url", rendered)

    def test_cookie_pool_discovery_is_redacted_and_disabled_by_default(self) -> None:
        response = self.client.get("/api/v1/cookie-pools")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["feature"]["api_enabled"])
        self.assertFalse(payload["feature"]["accepts_runtime_secrets"])
        self.assertTrue(payload["feature"]["requires_confirmation"])
        self.assertEqual(payload["feature"]["max_target_count"], 50)
        self.assertEqual(
            {pool["id"] for pool in payload["pools"]}, {"default", "overseas"}
        )
        self.assertTrue(all(not pool["configured"] for pool in payload["pools"]))
        self.assertNotIn("redis_url", response.text.lower())
        self.assertNotIn("cookie_header", response.text.lower())

    def test_cookie_fill_requires_deployment_enablement_and_configured_pool(self) -> None:
        payload = {
            "pool": "default",
            "marketplace_id": "US",
            "target_count": 2,
            "confirm_external_write": True,
        }
        disabled = self.client.post("/api/v1/cookie-pools/fill", json=payload)
        self.assertEqual(disabled.status_code, 409)

        self.application.settings = replace(
            self.application.settings, cookie_operations_api_enabled=True
        )
        unconfigured = self.client.post("/api/v1/cookie-pools/fill", json=payload)
        self.assertEqual(unconfigured.status_code, 409)
        self.assertIn("not configured", unconfigured.json()["error"]["message"])

    def test_cookie_fill_requires_confirmation_and_returns_only_safe_report(self) -> None:
        harvester = FakeCookieHarvester()
        self.application.settings = replace(
            self.application.settings, cookie_operations_api_enabled=True
        )
        self.application.cookie_harvesters["default"] = harvester
        payload = {
            "pool": "default",
            "marketplace_id": " us ",
            "target_count": 3,
            "confirm_external_write": False,
        }

        rejected = self.client.post("/api/v1/cookie-pools/fill", json=payload)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(harvester.calls, [])

        accepted = self.client.post(
            "/api/v1/cookie-pools/fill",
            json={**payload, "confirm_external_write": True},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["satisfied"])
        self.assertEqual(harvester.calls, [("US", "10001", 3)])
        self.assertEqual(
            accepted.json()["postal_selection"],
            {"postal_code": "10001", "source": "marketplace_default"},
        )
        self.assertEqual(accepted.json()["report"]["created"], 2)
        self.assertEqual(accepted.json()["report"]["failure_codes"], ["blocked_page"])
        rendered = accepted.text.lower()
        self.assertNotIn("cookie_header", rendered)
        self.assertNotIn("proxy_url", rendered)

    def test_cookie_fill_rejects_credentials_and_oversized_targets(self) -> None:
        self.application.settings = replace(
            self.application.settings, cookie_operations_api_enabled=True
        )
        self.application.cookie_harvesters["default"] = FakeCookieHarvester()
        base = {
            "pool": "default",
            "marketplace_id": "US",
            "target_count": 1,
            "confirm_external_write": True,
        }
        with_secret = self.client.post(
            "/api/v1/cookie-pools/fill",
            json={**base, "cookie": "do-not-echo-this-cookie"},
        )
        self.assertEqual(with_secret.status_code, 422)
        self.assertNotIn("do-not-echo-this-cookie", with_secret.text)
        self.assertEqual(
            with_secret.json()["error"]["message"], "request validation failed"
        )

        oversized = self.client.post(
            "/api/v1/cookie-pools/fill", json={**base, "target_count": 51}
        )
        self.assertEqual(oversized.status_code, 422)

    def test_cookie_fill_allows_operator_override_but_rejects_missing_default(self) -> None:
        harvester = FakeCookieHarvester()
        self.application.settings = replace(
            self.application.settings, cookie_operations_api_enabled=True
        )
        self.application.cookie_harvesters["default"] = harvester

        overridden = self.client.post(
            "/api/v1/cookie-pools/fill",
            json={
                "pool": "default",
                "marketplace_id": "US",
                "postal_code": " 94105 ",
                "target_count": 1,
                "confirm_external_write": True,
            },
        )
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(harvester.calls, [("US", "94105", 1)])
        self.assertEqual(overridden.json()["postal_selection"]["source"], "explicit")

        unsupported = self.client.post(
            "/api/v1/cookie-pools/fill",
            json={
                "pool": "default",
                "marketplace_id": "EG",
                "target_count": 1,
                "confirm_external_write": True,
            },
        )
        self.assertEqual(unsupported.status_code, 422)
        self.assertIn("no configured default", unsupported.json()["error"]["message"])
        self.assertEqual(harvester.calls, [("US", "94105", 1)])

    def test_all_canonical_kinds_are_registered(self) -> None:
        payload = self.client.get("/api/v1/capabilities").json()
        kinds = {plugin["kind"] for plugin in payload["plugins"]}
        self.assertTrue({
            "product",
            "product_hw",
            "product_time",
            "search",
            "search_hour",
            "reviews",
            "category_asin_list",
            "rank_list",
            "merchant",
            "merchant_home",
            "merchant_products",
        }.issubset(kinds))
        marketplaces = {market["id"]: market for market in payload["marketplaces"]}
        self.assertEqual(marketplaces["US"]["default_postal_code"], "10001")
        self.assertEqual(marketplaces["JP"]["default_postal_code"], "140-0001")
        self.assertIsNone(marketplaces["EG"]["default_postal_code"])
        self.assertEqual(
            sum(bool(market["default_postal_code"]) for market in marketplaces.values()),
            21,
        )
        self.assertEqual(payload["result_storage"]["canonical_sink"], "sqlite")
        self.assertIn("jsonl", payload["result_storage"]["configured_sinks"])
        self.assertEqual(payload["retry_defaults"]["ordinary_total_attempts"], 5)
        self.assertEqual(payload["retry_defaults"]["search_hour_total_attempts"], 11)

    def test_result_sink_selection_is_named_and_delivery_status_is_queryable(self) -> None:
        response = self.client.post(
            "/api/v1/jobs",
            json={
                "inputs": ["B000000001"],
                "marketplace_id": "US",
                "options": {"result_sinks": ["jsonl"]},
            },
        )
        self.assertEqual(response.status_code, 201)
        job = response.json()["job"]
        self.assertEqual(job["options"]["result_sinks"], ["jsonl", "sqlite"])
        deliveries = self.client.get(f"/api/v1/jobs/{job['id']}/deliveries")
        self.assertEqual(deliveries.status_code, 200)
        self.assertEqual(deliveries.json()["deliveries"], [])

        rejected = self.client.post(
            "/api/v1/jobs",
            json={
                "inputs": ["B000000002"],
                "marketplace_id": "US",
                "options": {"result_sinks": ["mysql://user:secret@host/db"]},
            },
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn("secret", rejected.text)

    def test_management_ui_exposes_all_canonical_kinds(self) -> None:
        index = self.client.get("/")
        script = self.client.get("/assets/app.js")
        self.assertEqual(index.status_code, 200)
        self.assertEqual(script.status_code, 200)
        rendered = index.text
        javascript = script.text
        for kind in (
            "product",
            "product_hw",
            "product_time",
            "search",
            "search_hour",
            "reviews",
            "category_asin_list",
            "rank_list",
            "merchant",
            "merchant_home",
            "merchant_products",
        ):
            self.assertIn(f'value="{kind}"', rendered)
            self.assertIn(f"{kind}:", javascript)
        self.assertIn("eventSummary", javascript)
        self.assertIn("details?.evidence?.sha256", javascript)
        self.assertIn("查看本条完整结构化 JSON", javascript)
        self.assertIn("Amazon 响应 SHA-256", javascript)
        self.assertIn("terminalStatuses.has(job.status)", javascript)
        self.assertIn("resultSinkOptions", rendered)
        self.assertIn("/deliveries?limit=30", javascript)
        self.assertIn("自动：普通 5 / 小时 11", rendered)
        self.assertIn("...retryOptions()", javascript)
        self.assertIn('id="cookieFillForm"', rendered)
        self.assertIn("Amazon Cookie 资源", rendered)
        self.assertIn("/api/v1/cookie-pools/fill", javascript)
        self.assertIn("confirm_external_write: true", javascript)
        self.assertIn('id="runbook"', rendered)
        self.assertIn("Cookie Gate", rendered)
        self.assertIn("Result Gate", rendered)
        self.assertIn("available_after", rendered)
        self.assertIn("amazon-crawler serve --host 127.0.0.1 --port 3000", rendered)
        self.assertIn('health.resources.cookie.stale ? "待刷新"', javascript)
        self.assertIn('id="cookiePostalPreview"', rendered)
        self.assertNotIn('id="cookiePostalCode"', rendered)
        self.assertNotIn('$("#cookiePostalCode")', javascript)
        self.assertIn("market.default_postal_code", javascript)
        self.assertIn('button.classList.toggle("running", state.cookieRunning)', javascript)
        self.assertIn('button.setAttribute("aria-busy", String(state.cookieRunning))', javascript)
        self.assertIn("确认授权后获取 Cookie", javascript)
        self.assertIn("Promise.allSettled", javascript)
        self.assertIn("cursor: not-allowed", self.client.get("/assets/styles.css").text)
        self.assertIn("cursor: progress", self.client.get("/assets/styles.css").text)

    def test_structured_search_input_preserves_legacy_fields(self) -> None:
        response = self.client.post(
            "/api/v1/jobs",
            json={
                "kind": "search",
                "inputs": [
                    {
                        "id": "legacy-1",
                        "keyword": "wireless mouse",
                        "market_id": "ATVPDKIKX0DER",
                        "post_code": "10001",
                        "turn_page": 2,
                        "frequent": 1,
                        "add_date": "2026-08-22",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 201)
        item = response.json()["job"]["items"][0]["input"]
        self.assertEqual(item["marketplace_code"], "US")
        self.assertEqual(item["market_id"], "ATVPDKIKX0DER")
        self.assertEqual(item["turn_page"], 2)

    def test_rank_input_rejects_cross_market_url(self) -> None:
        response = self.client.post(
            "/api/v1/jobs",
            json={
                "kind": "rank_list",
                "inputs": [
                    {
                        "market_id": "US",
                        "url": "https://example.com/bestsellers",
                        "url_type": "bestsellers",
                        "category_id": "100",
                        "page": 1,
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_merchant_home_and_products_preserve_parent_relation(self) -> None:
        home = self.client.post(
            "/api/v1/jobs",
            json={
                "kind": "merchant_home",
                "inputs": [
                    {
                        "id": "origin-1",
                        "seller_id": "SELLER123",
                        "market_id": "US",
                        "post_code": "10001",
                        "add_date": "2026-08-22",
                        "source_task_id": "origin-1",
                    }
                ],
            },
        )
        self.assertEqual(home.status_code, 201)
        product = self.client.post(
            "/api/v1/jobs",
            json={
                "kind": "merchant_products",
                "inputs": [
                    {
                        "id": "page-1",
                        "source_task_id": "origin-1",
                        "seller_id": "SELLER123",
                        "market_id": "US",
                        "post_code": "10001",
                        "page": 1,
                        "add_date": "2026-08-22",
                    }
                ],
            },
        )
        self.assertEqual(product.status_code, 201)
        task = product.json()["job"]["items"][0]["input"]
        self.assertEqual(task["source_task_id"], "origin-1")


if __name__ == "__main__":
    unittest.main()
