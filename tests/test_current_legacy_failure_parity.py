from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from amazon_crawler.domain.models import ClaimedItem, ExecutionMode
from amazon_crawler.domain.resources import FingerprintProfile, RequestContext
from amazon_crawler.infra.evidence import EvidenceStore
from amazon_crawler.infra.http import (
    LEGACY_NON_SUCCESS_NOT_FOUND_MARKERS,
    FetchResponse,
)
from amazon_crawler.infra.legacy_compat import LEGACY_MAX_ATTEMPTS, legacy_state_for
from amazon_crawler.plugins.amazon_collections import AmazonCollectionPlugin
from amazon_crawler.plugins.amazon_merchants import AmazonMerchantPlugin
from amazon_crawler.plugins.amazon_product import AmazonProductPlugin
from amazon_crawler.plugins.response_policy import PRODUCT_PAGE_NOT_FOUND_MARKERS
from scripts.audit_archived_product_parity import (
    _literal_legacy_config,
    _load_current_legacy_collection_parser,
    _load_current_legacy_merchant_parser,
    _load_current_legacy_parser,
)
from scripts.collect_failure_dual_shadow import (
    KIND_TO_LEGACY_TASK,
    _legacy_failure_state,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT = PROJECT_ROOT.parent
MARKET_ID = "ATVPDKIKX0DER"
NO_PAGE = (
    "Sorry! We couldn't find that page. Try searching or go to Amazon's home page."
)
NO_SEARCH_RESULTS = "<html><body>No results for this query</body></html>"
NO_RANK_RESULTS = (
    "<html><body>Sorry, there are no Best Sellers available in this category. "
    "Please check back later.</body></html>"
)
NO_REVIEWS = (
    '<div id="cm_cr_dp_d_rating_histogram"><ul id="histogramTable"><li>'
    '<div class="a-section a-spacing-none a-text-left aok-nowrap">5 star</div>'
    '<div class="a-section a-spacing-none a-text-right aok-nowrap">0%</div>'
    "</li></ul></div>"
    "<script>jQuery.parseJSON('{}')</script>"
    '<a id="acrCustomerReviewLink">'
    '<span id="acrCustomerReviewText">0 ratings</span></a>'
)
THROTTLED = (
    "<html><body>Request was throttled. Please wait a moment and refresh the page"
    "</body></html>"
)


class SameResponseFetcher:
    def __init__(self, html: str) -> None:
        self.html = html
        self.reports: list[str] = []

    async def fetch(self, url: str, **_kwargs: Any) -> FetchResponse:
        return FetchResponse(
            url=url,
            status_code=200,
            html=self.html,
            headers={"content-type": "text/html"},
            resource_context=RequestContext(
                cookie=None,
                proxy=None,
                fingerprint=FingerprintProfile("fixture", {}),
            ),
        )

    async def report(self, _response: FetchResponse, outcome: Any) -> None:
        self.reports.append(str(outcome.value))


def task_for(kind: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "id": f"parity-{kind}",
        "market_id": MARKET_ID,
        "post_code": "10001",
        "add_date": "20260823",
    }
    if kind in {"product", "product_hw", "product_time", "reviews"}:
        common["asin"] = "B000000010"
    elif kind in {"search", "search_hour"}:
        common.update({"keyword": "no result", "turn_page": 1, "frequent": 0})
        if kind == "search_hour":
            common["data_hour"] = "2026-08-23 08:00:00"
    elif kind == "category_asin_list":
        common.update({"category_id": "999999999999", "page": 1})
    elif kind == "rank_list":
        common.update(
            {
                "url": "https://www.amazon.com/Best-Sellers/zgbs/999999999999",
                "url_type": "bestsellers",
                "category_id": "999999999999",
                "page": 1,
            }
        )
    else:
        common["seller_id"] = "A0000000000000"
        if kind == "merchant_products":
            common["page"] = 1
    return common


def no_result_html(kind: str) -> str:
    if kind in {"product", "product_hw", "product_time"}:
        return NO_PAGE
    if kind == "reviews":
        return NO_REVIEWS
    if kind == "rank_list":
        return NO_RANK_RESULTS
    if kind == "merchant":
        return "<html><body>Welcome to Amazon Customer Service</body></html>"
    return NO_SEARCH_RESULTS


def expected_no_result_code(kind: str) -> str:
    return {
        "product": "product_not_found",
        "product_hw": "product_not_found",
        "product_time": "product_not_found",
        "search": "no_results",
        "search_hour": "no_results",
        "reviews": "no_reviews",
        "category_asin_list": "no_category_results",
        "rank_list": "no_rank_results",
        "merchant": "merchant_has_no_products",
        "merchant_home": "merchant_has_no_products",
        "merchant_products": "merchant_page_has_no_products",
    }[kind]


class CurrentLegacyFailureParityTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.product_parser = _load_current_legacy_parser(LEGACY_ROOT)
        cls.collection_parser = _load_current_legacy_collection_parser(LEGACY_ROOT)
        cls.merchant_parser = _load_current_legacy_merchant_parser(LEGACY_ROOT)

    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.evidence = EvidenceStore(Path(self.tempdir.name), False)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def execute_legacy(self, kind: str, html: str, task: dict[str, Any]) -> int:
        try:
            if kind in {"product", "product_hw", "product_time"}:
                type(self).product_parser(html, task, 200)
            elif kind in {
                "search",
                "search_hour",
                "reviews",
                "category_asin_list",
                "rank_list",
            }:
                type(self).collection_parser(kind, [(html, 200)], task)
            else:
                type(self).merchant_parser(
                    kind,
                    html,
                    200,
                    task,
                    "https://www.amazon.com/controlled-parity",
                )
        except Exception as exc:
            return _legacy_failure_state(exc)
        self.fail(f"current legacy parser unexpectedly accepted {kind} failure fixture")

    async def execute_v2(self, kind: str, html: str, task: dict[str, Any]):
        fetcher = SameResponseFetcher(html)
        if kind in {"product", "product_hw", "product_time"}:
            plugin: Any = AmazonProductPlugin(
                fetcher=fetcher,
                evidence_store=self.evidence,
                require_cookie=False,
                kind=kind,
            )
        elif kind in {
            "search",
            "search_hour",
            "reviews",
            "category_asin_list",
            "rank_list",
        }:
            plugin = AmazonCollectionPlugin(
                kind=kind,
                fetcher=fetcher,
                evidence_store=self.evidence,
                require_cookie=False,
            )
        else:
            plugin = AmazonMerchantPlugin(
                kind=kind,
                fetcher=fetcher,
                evidence_store=self.evidence,
                require_cookie=False,
            )
        normalized = plugin.normalize(task, "US", "10001")
        legacy_task = KIND_TO_LEGACY_TASK[kind]
        item = ClaimedItem(
            id=f"item-{kind}",
            job_id=f"job-{kind}",
            seq=1,
            kind=kind,
            execution_mode=ExecutionMode.STANDARD,
            input=normalized.as_dict(),
            options={},
            attempts=1,
            max_attempts=LEGACY_MAX_ATTEMPTS.get(legacy_task, 5),
            lease_owner="same-response-test",
            lease_token="same-response-lease-token",
        )
        return await plugin.execute(item)

    async def test_all_eleven_no_result_states_match_current_legacy_source(self) -> None:
        for kind in KIND_TO_LEGACY_TASK:
            with self.subTest(kind=kind):
                task = task_for(kind)
                html = no_result_html(kind)
                legacy_state = self.execute_legacy(kind, html, task)
                outcome = await self.execute_v2(kind, html, task)
                self.assertFalse(outcome.ok)
                self.assertIsNotNone(outcome.failure)
                self.assertEqual(outcome.failure.code, expected_no_result_code(kind))
                self.assertFalse(outcome.failure.retryable)
                self.assertEqual(
                    legacy_state_for(
                        "failed",
                        outcome.failure.code,
                        KIND_TO_LEGACY_TASK[kind],
                    ),
                    legacy_state,
                )

    async def test_all_eleven_throttle_states_match_current_legacy_source(self) -> None:
        for kind in KIND_TO_LEGACY_TASK:
            with self.subTest(kind=kind):
                task = task_for(kind)
                legacy_state = self.execute_legacy(kind, THROTTLED, task)
                outcome = await self.execute_v2(kind, THROTTLED, task)
                self.assertFalse(outcome.ok)
                self.assertIsNotNone(outcome.failure)
                self.assertEqual(outcome.failure.code, "upstream_incomplete")
                self.assertTrue(outcome.failure.retryable)
                self.assertEqual(legacy_state, -1)
                self.assertEqual(
                    legacy_state_for(
                        "failed",
                        outcome.failure.code,
                        KIND_TO_LEGACY_TASK[kind],
                    ),
                    legacy_state,
                )

    def test_non_success_page_markers_equal_current_legacy_literals(self) -> None:
        values = _literal_legacy_config(LEGACY_ROOT / "settings/config.py")
        names = (
            "NO_LIST_PAGE_TEXT",
            "NO_LIST_PAGE_TEXT_BR",
            "NO_LIST_PAGE_TEXT_MX",
            "ADDRESS_ERROR",
            "ADDRESS_ERROR2",
            "ADDRESS_ERROR_TR",
            "ADDRESS_ERROR_PL",
            "ADDRESS_ERROR_FR",
            "ADDRESS_ERROR_IT",
            "ADDRESS_ERROR_SE",
            "ADDRESS_ERROR_ES",
            "UNAVA_UK",
        )
        expected = {str(values[name]).lower() for name in names}

        self.assertEqual(set(LEGACY_NON_SUCCESS_NOT_FOUND_MARKERS), expected)
        self.assertEqual(set(PRODUCT_PAGE_NOT_FOUND_MARKERS), expected)


if __name__ == "__main__":
    unittest.main()
