from __future__ import annotations

import asyncio
import json
import html as html_lib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from amazon_crawler.domain.models import ClaimedItem, CrawlFailure, ExecutionMode
from amazon_crawler.domain.resources import FingerprintProfile, RequestContext
from amazon_crawler.infra.evidence import EvidenceStore
from amazon_crawler.infra.http import FetchResponse
from amazon_crawler.plugins.amazon_collections import AmazonCollectionPlugin
from amazon_crawler.plugins.amazon_merchants import AmazonMerchantPlugin
from amazon_crawler.plugins.amazon_product import AmazonProductPlugin
from amazon_crawler.plugins.marketplaces import MARKETPLACES, MARKETPLACE_LANGUAGES


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureFetcher:
    def __init__(self) -> None:
        self.product_html = (FIXTURES / "product.html").read_text(encoding="utf-8")
        self.collection_html = (FIXTURES / "search_stream.txt").read_text(encoding="utf-8")
        self.sources = {
            "search": self.collection_html,
            "search_hour": self.collection_html,
            "merchant_products": self.collection_html,
            "reviews": (FIXTURES / "reviews.html").read_text(encoding="utf-8"),
            "category_asin_list": (FIXTURES / "category.html").read_text(encoding="utf-8"),
            "rank_list": (FIXTURES / "rank.html").read_text(encoding="utf-8"),
            "merchant": (FIXTURES / "merchant_detail.html").read_text(encoding="utf-8"),
            "merchant_home": (FIXTURES / "merchant_home.html").read_text(encoding="utf-8"),
        }
        self.calls: list[dict[str, object]] = []
        self.reports: list[str] = []
        self.rank_initial_html: str | None = None
        self.rank_stream_html: str | None = None
        self.rank_stream_failure: CrawlFailure | None = None

    async def fetch(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        purpose = str(kwargs["purpose"])
        source = self.product_html if purpose in {
            "product",
            "product_hw",
            "product_time",
            "product_jp",
            "product_hw_jp",
            "product_time_jp",
        } else self.sources.get(purpose, self.collection_html)
        if purpose == "rank_list" and self.rank_initial_html:
            if kwargs.get("method") == "POST" and self.rank_stream_failure:
                return self.rank_stream_failure
            source = (
                self.rank_stream_html
                if kwargs.get("method") == "POST"
                else self.rank_initial_html
            ) or self.collection_html
        return FetchResponse(
            url=url,
            status_code=200,
            html=source,
            headers={"content-type": "text/html"},
            resource_context=RequestContext(
                cookie=None,
                proxy=None,
                fingerprint=FingerprintProfile("fixture", {}),
            ),
        )

    async def report(self, _response, outcome):
        self.reports.append(outcome.value)


def claimed(plugin, source, *, seq: int = 1) -> ClaimedItem:
    normalized = plugin.normalize(source, "US", "10001")
    return ClaimedItem(
        id=f"item-{plugin.kind}-{seq}",
        job_id=f"job-{plugin.kind}",
        seq=seq,
        kind=plugin.kind,
        execution_mode=ExecutionMode.STANDARD,
        input=normalized.as_dict(),
        options={},
        attempts=1,
        max_attempts=3,
        lease_owner="fixture-worker",
        lease_token="fixture-lease-token",
    )


class PluginContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.fetcher = FixtureFetcher()
        self.evidence = EvidenceStore(Path(self.tempdir.name), False)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_all_eleven_canonical_tasks_execute_from_offline_fixtures(self) -> None:
        cases = [
            (
                AmazonProductPlugin(self.fetcher, self.evidence, kind="product"),
                "B000000001",
                "amazon.product.v2",
            ),
            (
                AmazonProductPlugin(self.fetcher, self.evidence, kind="product_hw"),
                "B000000001",
                "amazon.product.v2",
            ),
            (
                AmazonProductPlugin(self.fetcher, self.evidence, kind="product_time"),
                "B000000001",
                "amazon.product-observation.v1",
            ),
            (
                AmazonCollectionPlugin(kind="search", fetcher=self.fetcher, evidence_store=self.evidence),
                {"keyword": "example", "market_id": "US", "turn_page": 1, "frequent": 0},
                "amazon.search-page.v1",
            ),
            (
                AmazonCollectionPlugin(kind="search_hour", fetcher=self.fetcher, evidence_store=self.evidence),
                {"keyword": "example", "market_id": "US", "turn_page": 1, "frequent": 1},
                "amazon.search-page.v1",
            ),
            (
                AmazonCollectionPlugin(kind="reviews", fetcher=self.fetcher, evidence_store=self.evidence),
                {"asin": "B000000010", "market_id": "US"},
                "amazon.reviews.v1",
            ),
            (
                AmazonCollectionPlugin(kind="category_asin_list", fetcher=self.fetcher, evidence_store=self.evidence),
                {"category_id": "100", "market_id": "US", "page": 1},
                "amazon.category-page.v1",
            ),
            (
                AmazonCollectionPlugin(kind="rank_list", fetcher=self.fetcher, evidence_store=self.evidence),
                {"url": "https://www.amazon.com/bestsellers", "url_type": "bestsellers", "category_id": "100", "market_id": "US", "page": 1},
                "amazon.rank-page.v1",
            ),
            (
                AmazonMerchantPlugin(kind="merchant", fetcher=self.fetcher, evidence_store=self.evidence),
                {"seller_id": "SELLER123", "market_id": "US"},
                "amazon.merchant.v1",
            ),
            (
                AmazonMerchantPlugin(kind="merchant_home", fetcher=self.fetcher, evidence_store=self.evidence),
                {"seller_id": "SELLER123", "market_id": "US", "source_task_id": "origin-1"},
                "amazon.merchant-child-tasks.v1",
            ),
            (
                AmazonMerchantPlugin(kind="merchant_products", fetcher=self.fetcher, evidence_store=self.evidence),
                {"seller_id": "SELLER123", "market_id": "US", "page": 1, "source_task_id": "origin-1"},
                "amazon.merchant-products.v1",
            ),
        ]
        outcomes = {}
        for plugin, source, expected_schema in cases:
            outcome = await plugin.execute(claimed(plugin, source))
            self.assertTrue(outcome.ok, (plugin.kind, outcome.failure))
            self.assertEqual(outcome.result.schema_version, expected_schema)
            self.assertTrue(outcome.result.evidence["sha256"])
            outcomes[plugin.kind] = outcome.result

        self.assertEqual(set(outcomes), {
            "product", "product_hw", "product_time", "search", "search_hour",
            "reviews", "category_asin_list", "rank_list", "merchant",
            "merchant_home", "merchant_products",
        })
        self.assertEqual(outcomes["search"].data["row_count"], 1)
        self.assertEqual(len(outcomes["merchant_home"].followup_jobs), 1)
        self.assertEqual(outcomes["merchant_home"].followup_jobs[0].kind, "merchant_products")
        self.assertEqual(len(outcomes["merchant_products"].followup_jobs), 1)
        self.assertEqual(outcomes["merchant_products"].followup_jobs[0].kind, "product")
        self.assertNotIn("dimension_items", outcomes["product_time"].data)

        legacy_product = AmazonProductPlugin(
            self.fetcher, self.evidence, kind="product_jp"
        )
        legacy_outcome = await legacy_product.execute(
            claimed(
                legacy_product,
                {
                    "id": "legacy-product-42",
                    "market_id": "ATVPDKIKX0DER",
                    "asin": "B000000001",
                    "post_code": "10001",
                },
            )
        )
        self.assertTrue(legacy_outcome.ok, legacy_outcome.failure)
        self.assertEqual(legacy_outcome.result.data["task_id"], "legacy-product-42")
        self.assertTrue(
            all(
                row["task_id"] == "legacy-product-42"
                for row in legacy_outcome.result.data["dimension_items"]
            )
        )

    async def test_merchant_missing_name_keeps_legacy_success_semantics(self) -> None:
        self.fetcher.sources["merchant"] = "<html><body>seller page shell</body></html>"
        plugin = AmazonMerchantPlugin(
            kind="merchant",
            fetcher=self.fetcher,
            evidence_store=self.evidence,
        )

        outcome = await plugin.execute(
            claimed(plugin, {"seller_id": "SELLER123", "market_id": "US"})
        )

        self.assertTrue(outcome.ok, outcome.failure)
        self.assertEqual(outcome.result.data["item"]["seller_name"], "")
        self.assertEqual(outcome.result.data["item"]["business_name"], "")

    async def test_unexpected_parser_failures_keep_legacy_retry_semantics(self) -> None:
        cases = [
            (
                AmazonProductPlugin(
                    self.fetcher,
                    self.evidence,
                    kind="product",
                ),
                "B000000001",
                "amazon_crawler.plugins.amazon_product.parse_product_html",
            ),
            (
                AmazonCollectionPlugin(
                    kind="search",
                    fetcher=self.fetcher,
                    evidence_store=self.evidence,
                ),
                {
                    "keyword": "example",
                    "market_id": "US",
                    "turn_page": 1,
                },
                "amazon_crawler.plugins.amazon_collections.parse_search_html",
            ),
            (
                AmazonMerchantPlugin(
                    kind="merchant",
                    fetcher=self.fetcher,
                    evidence_store=self.evidence,
                ),
                {"seller_id": "SELLER123", "market_id": "US"},
                "amazon_crawler.plugins.amazon_merchants.parse_merchant_html",
            ),
        ]
        for plugin, source, target in cases:
            with self.subTest(kind=plugin.kind), patch(
                target,
                side_effect=ValueError("fixture parser failure"),
            ):
                outcome = await plugin.execute(claimed(plugin, source))

            self.assertEqual(outcome.failure.code, "parse_error")
            self.assertTrue(outcome.failure.retryable)
            self.assertEqual(
                outcome.failure.details["error_type"],
                "ValueError",
            )

    async def test_successful_response_resources_are_released_before_evidence_io(self) -> None:
        class ExplodingEvidenceStore(EvidenceStore):
            def save_html(self, **_kwargs):
                raise OSError("fixture evidence disk failure")

        evidence = ExplodingEvidenceStore(Path(self.tempdir.name), True)
        cases = [
            (
                AmazonProductPlugin(self.fetcher, evidence, kind="product"),
                "B000000001",
            ),
            (
                AmazonCollectionPlugin(
                    kind="search",
                    fetcher=self.fetcher,
                    evidence_store=evidence,
                ),
                {"keyword": "example", "market_id": "US", "turn_page": 1},
            ),
            (
                AmazonMerchantPlugin(
                    kind="merchant",
                    fetcher=self.fetcher,
                    evidence_store=evidence,
                ),
                {"seller_id": "SELLER123", "market_id": "US"},
            ),
        ]
        for plugin, source in cases:
            with self.subTest(kind=plugin.kind):
                before = len(self.fetcher.reports)
                with self.assertRaisesRegex(OSError, "evidence disk failure"):
                    await plugin.execute(claimed(plugin, source))
                self.assertEqual(self.fetcher.reports[before:], ["success"])

    async def test_streaming_search_fragment_is_replayed(self) -> None:
        node = (
            '<div data-component-type="s-search-result" role="listitem" data-asin="B000000077">'
            '<div data-cy="title-recipe"><a href="/dp/B000000077"><h2><span>Streamed Product</span></h2></a></div>'
            '</div>'
        )
        self.fetcher.collection_html = (
            json.dumps(
                ["append", "data-search-metadata", {"metadata": {"totalResultCount": 1}, "html": "<span>1-1 of 1 results for</span>"}],
                ensure_ascii=False,
            )
            + "&&&"
            + json.dumps(
                ["append", "data-main-slot:search-result", {"html": node, "asin": "B000000077"}],
                ensure_ascii=False,
            )
        )
        self.fetcher.sources["search"] = self.fetcher.collection_html
        plugin = AmazonCollectionPlugin(
            kind="search",
            fetcher=self.fetcher,
            evidence_store=self.evidence,
        )
        outcome = await plugin.execute(
            claimed(plugin, {"keyword": "stream", "market_id": "US", "turn_page": 2})
        )
        self.assertTrue(outcome.ok, outcome.failure)
        self.assertEqual(outcome.result.data["items"][0]["data_asin"], "B000000077")
        self.assertEqual(outcome.result.data["items"][0]["page"], 2)

    async def test_legacy_terminal_no_data_reasons_remain_distinct(self) -> None:
        cases = [
            (
                "search",
                {"keyword": "example", "market_id": "US", "turn_page": 2},
                '<span>49-19 of 19 results for</span> \\"listitem\\" data-asin=\\"B000000010\\"',
                "one_page_only",
            ),
            (
                "reviews",
                {"asin": "B000000010", "market_id": "US"},
                '<div id="cm_cr_dp_d_rating_histogram"></div><script>jQuery.parseJSON(\'{}\')</script><a id="acrCustomerReviewLink"><span id="acrCustomerReviewText">0 ratings</span></a>',
                "no_reviews",
            ),
            (
                "category_asin_list",
                {"category_id": "100", "market_id": "US", "page": 1},
                "<html><body>No results for this category</body></html>",
                "no_category_results",
            ),
            (
                "rank_list",
                {
                    "url": "https://www.amazon.com/bestsellers",
                    "url_type": "bestsellers",
                    "category_id": "100",
                    "market_id": "US",
                    "page": 1,
                },
                (
                    "<html><body>Sorry, there are no Best Sellers available in "
                    "this category. Please check back later.</body></html>"
                ),
                "no_rank_results",
            ),
        ]
        for kind, source, html, expected_code in cases:
            self.fetcher.collection_html = html
            self.fetcher.sources[kind] = html
            plugin = AmazonCollectionPlugin(
                kind=kind,
                fetcher=self.fetcher,
                evidence_store=self.evidence,
            )
            outcome = await plugin.execute(claimed(plugin, source))
            self.assertFalse(outcome.ok, kind)
            self.assertEqual(outcome.failure.code, expected_code, kind)
            self.assertTrue(outcome.failure.details["evidence"]["sha256"], kind)
            self.assertFalse(outcome.failure.details["evidence"]["captured"], kind)

    async def test_request_shapes_preserve_legacy_locale_and_pagination(self) -> None:
        self.assertEqual(set(MARKETPLACES), set(MARKETPLACE_LANGUAGES))

        product = AmazonProductPlugin(self.fetcher, self.evidence, kind="product")
        product_outcome = await product.execute(claimed(product, "B000000001"))
        self.assertTrue(product_outcome.ok)
        product_query = parse_qs(urlparse(self.fetcher.calls[-1]["url"]).query)
        self.assertEqual(product_query, {"th": ["1"], "psc": ["1"], "language": ["en_US"]})

        search = AmazonCollectionPlugin(
            kind="search", fetcher=self.fetcher, evidence_store=self.evidence
        )
        search_outcome = await search.execute(
            claimed(
                search,
                {"keyword": "wireless mouse", "market_id": "US", "turn_page": 2},
            )
        )
        self.assertTrue(search_outcome.ok)
        search_call = self.fetcher.calls[-1]
        search_query = parse_qs(urlparse(search_call["url"]).query)
        self.assertEqual(search_query["k"], ["wireless mouse"])
        self.assertEqual(search_query["page"], ["2"])
        self.assertEqual(search_query["language"], ["en_US"])
        self.assertIn("qid", search_query)
        self.assertEqual(search_call["extra_headers"], {"Referer": search_call["url"]})

        reviews = AmazonCollectionPlugin(
            kind="reviews", fetcher=self.fetcher, evidence_store=self.evidence
        )
        review_url, _, _ = reviews._request(
            reviews.normalize(
                {"asin": "B000000010", "market_id": "JP"}, None, None
            ).as_dict()
        )
        self.assertEqual(
            parse_qs(urlparse(review_url).query),
            {"th": ["1"], "psc": ["1"], "language": ["en_US"]},
        )

        category = AmazonCollectionPlugin(
            kind="category_asin_list",
            fetcher=self.fetcher,
            evidence_store=self.evidence,
        )
        category_url, _, _ = category._request(
            category.normalize(
                {"category_id": "100", "market_id": "US", "page": 3},
                None,
                None,
            ).as_dict()
        )
        self.assertIn("ref=sr_pg_3", category_url)

        merchant_products = AmazonMerchantPlugin(
            kind="merchant_products",
            fetcher=self.fetcher,
            evidence_store=self.evidence,
        )
        merchant_outcome = await merchant_products.execute(
            claimed(
                merchant_products,
                {"seller_id": "SELLER123", "market_id": "US", "page": 2},
            )
        )
        self.assertTrue(merchant_outcome.ok)
        merchant_call = self.fetcher.calls[-1]
        merchant_query = parse_qs(urlparse(merchant_call["url"]).query)
        self.assertEqual(merchant_query["language"], ["en"])
        self.assertIn("qid", merchant_query)
        self.assertEqual(
            merchant_call["extra_headers"], {"Referer": merchant_call["url"]}
        )

    async def test_jp_merchant_products_expand_to_hardware_product_route(self) -> None:
        plugin = AmazonMerchantPlugin(
            kind="merchant_products",
            fetcher=self.fetcher,
            evidence_store=self.evidence,
        )
        outcome = await plugin.execute(
            claimed(
                plugin,
                {
                    "seller_id": "SELLER123",
                    "market_id": "JP",
                    "page": 1,
                    "source_task_id": "origin-jp",
                },
            )
        )
        self.assertTrue(outcome.ok, outcome.failure)
        self.assertEqual(outcome.result.followup_jobs[0].kind, "product_hw")

    async def test_source_linked_merchant_products_preserve_legacy_dedup_rule(self) -> None:
        original = self.fetcher.sources["merchant_products"]
        self.fetcher.sources["merchant_products"] = f"{original}&&&{original}"
        plugin = AmazonMerchantPlugin(
            kind="merchant_products",
            fetcher=self.fetcher,
            evidence_store=self.evidence,
        )
        linked = await plugin.execute(
            claimed(
                plugin,
                {
                    "seller_id": "SELLER123",
                    "market_id": "US",
                    "page": 1,
                    "source_task_id": "origin-1",
                },
            )
        )
        self.assertTrue(linked.ok, linked.failure)
        self.assertEqual(linked.result.data["row_count"], 1)
        self.assertEqual(len(linked.result.data["product_detail_tasks"]), 1)

        unlinked = await plugin.execute(
            claimed(
                plugin,
                {"seller_id": "SELLER123", "market_id": "US", "page": 1},
                seq=2,
            )
        )
        self.assertTrue(unlinked.ok, unlinked.failure)
        self.assertEqual(unlinked.result.data["row_count"], 2)
        self.assertEqual(unlinked.result.data["product_detail_tasks"], [])

    async def test_rank_continuation_keeps_metadata_ranking(self) -> None:
        metadata = html_lib.escape(
            json.dumps(
                [
                    {"id": "B000000020", "metadataMap": {"render.zg.rank": "1"}},
                    {"id": "B000000021", "metadataMap": {"render.zg.rank": "2"}},
                ]
            ),
            quote=True,
        )
        self.fetcher.rank_initial_html = f"""
          <html><body><div class="p13n-desktop-grid"
            data-client-recs-list="{metadata}" data-index-offset="1" data-offset="2"
            data-acp-path="/acp/" data-acp-params="fixture-token">
            <div data-asin="B000000020"><a href="/dp/B000000020"><img alt="First" /></a></div>
          </div></body></html>
        """
        self.fetcher.rank_stream_html = """
          <section><div data-asin="B000000021">
            <a href="/dp/B000000021"><img alt="Second" /></a>
          </div></section>
        """
        plugin = AmazonCollectionPlugin(
            kind="rank_list",
            fetcher=self.fetcher,
            evidence_store=self.evidence,
        )
        outcome = await plugin.execute(
            claimed(
                plugin,
                {
                    "url": "https://www.amazon.com/bestsellers",
                    "url_type": "bestsellers",
                    "category_id": "100",
                    "market_id": "US",
                    "page": 1,
                },
            )
        )
        self.assertTrue(outcome.ok, outcome.failure)
        self.assertEqual(outcome.result.data["row_count"], 2)
        ranks = {row["asin"]: row["ranking"] for row in outcome.result.data["items"]}
        self.assertEqual(ranks, {"B000000020": "1", "B000000021": "2"})
        continuation_calls = [
            call for call in self.fetcher.calls if call.get("method") == "POST"
        ]
        self.assertEqual(len(continuation_calls), 1)
        self.assertEqual(
            continuation_calls[0]["extra_headers"],
            {"x-amz-acp-params": "fixture-token"},
        )

    async def test_rank_continuations_use_legacy_concurrency_of_three(self) -> None:
        class ConcurrentRankFetcher(FixtureFetcher):
            def __init__(self) -> None:
                super().__init__()
                self.active_streams = 0
                self.max_active_streams = 0

            async def fetch(self, url: str, **kwargs):
                if kwargs.get("purpose") != "rank_list" or kwargs.get("method") != "POST":
                    return await super().fetch(url, **kwargs)
                self.calls.append({"url": url, **kwargs})
                self.active_streams += 1
                self.max_active_streams = max(
                    self.max_active_streams,
                    self.active_streams,
                )
                try:
                    await asyncio.sleep(0.02)
                    metadata = [
                        json.loads(value)
                        for value in kwargs["json_body"]["ids"]
                    ]
                    rows = "".join(
                        f'<div data-asin="{value["id"]}">'
                        f'<a href="/dp/{value["id"]}"><img alt="Item" /></a>'
                        "</div>"
                        for value in metadata
                    )
                    return FetchResponse(
                        url=url,
                        status_code=200,
                        html=f"<section>{rows}</section>",
                        headers={"content-type": "text/html"},
                        resource_context=RequestContext(
                            cookie=None,
                            proxy=None,
                            fingerprint=FingerprintProfile("fixture", {}),
                        ),
                    )
                finally:
                    self.active_streams -= 1

        fetcher = ConcurrentRankFetcher()
        metadata_rows = [
            {
                "id": f"B{i:09d}",
                "metadataMap": {"render.zg.rank": str(i + 1)},
            }
            for i in range(25)
        ]
        metadata = html_lib.escape(json.dumps(metadata_rows), quote=True)
        first_asin = metadata_rows[0]["id"]
        fetcher.rank_initial_html = f"""
          <html><body><div class="p13n-desktop-grid"
            data-client-recs-list="{metadata}" data-index-offset="1" data-offset="25"
            data-acp-path="/acp/" data-acp-params="fixture-token">
            <div data-asin="{first_asin}"><a href="/dp/{first_asin}"><img alt="First" /></a></div>
          </div></body></html>
        """
        plugin = AmazonCollectionPlugin(
            kind="rank_list",
            fetcher=fetcher,
            evidence_store=self.evidence,
        )

        outcome = await plugin.execute(
            claimed(
                plugin,
                {
                    "url": "https://www.amazon.com/bestsellers",
                    "url_type": "bestsellers",
                    "category_id": "100",
                    "market_id": "US",
                    "page": 1,
                },
            )
        )

        self.assertTrue(outcome.ok, outcome.failure)
        self.assertEqual(outcome.result.data["row_count"], 25)
        continuation_calls = [
            call for call in fetcher.calls if call.get("method") == "POST"
        ]
        self.assertEqual(len(continuation_calls), 3)
        self.assertEqual(fetcher.max_active_streams, 3)

    async def test_rank_continuation_failure_releases_initial_page_resources(self) -> None:
        metadata = html_lib.escape(
            json.dumps(
                [
                    {"id": "B000000020", "metadataMap": {"render.zg.rank": "1"}},
                    {"id": "B000000021", "metadataMap": {"render.zg.rank": "2"}},
                ]
            ),
            quote=True,
        )
        self.fetcher.rank_initial_html = f"""
          <html><body><div class="p13n-desktop-grid"
            data-client-recs-list="{metadata}" data-index-offset="1" data-offset="2"
            data-acp-path="/acp/" data-acp-params="fixture-token">
            <div data-asin="B000000020"><a href="/dp/B000000020"><img alt="First" /></a></div>
          </div></body></html>
        """
        self.fetcher.rank_stream_failure = CrawlFailure(
            "upstream_retryable",
            "fixture continuation failure",
            True,
        )
        plugin = AmazonCollectionPlugin(
            kind="rank_list",
            fetcher=self.fetcher,
            evidence_store=self.evidence,
        )

        outcome = await plugin.execute(
            claimed(
                plugin,
                {
                    "url": "https://www.amazon.com/bestsellers",
                    "url_type": "bestsellers",
                    "category_id": "100",
                    "market_id": "US",
                    "page": 1,
                },
            )
        )

        self.assertEqual(outcome.failure.code, "upstream_retryable")
        self.assertEqual(self.fetcher.reports, ["success"])

    async def test_empty_rank_continuation_is_retryable_and_uses_its_evidence(self) -> None:
        metadata = html_lib.escape(
            json.dumps(
                [
                    {"id": "B000000020", "metadataMap": {"render.zg.rank": "1"}},
                    {"id": "B000000021", "metadataMap": {"render.zg.rank": "2"}},
                ]
            ),
            quote=True,
        )
        self.fetcher.rank_initial_html = f"""
          <html><body><div class="p13n-desktop-grid"
            data-client-recs-list="{metadata}" data-index-offset="1" data-offset="2"
            data-acp-path="/acp/" data-acp-params="fixture-token">
            <div data-asin="B000000020"><a href="/dp/B000000020"><img alt="First" /></a></div>
          </div></body></html>
        """
        self.fetcher.rank_stream_html = "<html><body>empty continuation</body></html>"
        plugin = AmazonCollectionPlugin(
            kind="rank_list",
            fetcher=self.fetcher,
            evidence_store=self.evidence,
        )

        outcome = await plugin.execute(
            claimed(
                plugin,
                {
                    "url": "https://www.amazon.com/bestsellers",
                    "url_type": "bestsellers",
                    "category_id": "100",
                    "market_id": "US",
                    "page": 1,
                },
            )
        )

        self.assertEqual(outcome.failure.code, "upstream_incomplete")
        self.assertTrue(outcome.failure.retryable)
        self.assertEqual(self.fetcher.reports, ["parse_error", "success"])
        self.assertEqual(
            outcome.failure.details["evidence"]["bytes"],
            len(self.fetcher.rank_stream_html.encode("utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
