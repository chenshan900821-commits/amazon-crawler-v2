from __future__ import annotations

import unittest

from amazon_crawler.plugins.response_policy import (
    classify_amazon_page,
    classify_collection_completeness,
    classify_product_completeness,
)


class ResponsePolicyTests(unittest.TestCase):
    def test_multilingual_verification_is_retryable(self) -> None:
        issue = classify_amazon_page(
            "Désolés, il faut que nous nous assurions que vous n'êtes pas un robot.",
            not_found_code="no_results",
            purpose="search",
        )
        self.assertEqual(issue.code, "blocked_page")
        self.assertTrue(issue.retryable)

    def test_throttle_is_retryable_but_missing_page_is_terminal(self) -> None:
        throttled = classify_amazon_page(
            "Request was throttled. Please wait a moment and refresh the page",
            not_found_code="product_not_found",
            purpose="product",
        )
        self.assertEqual(throttled.code, "upstream_incomplete")
        self.assertTrue(throttled.retryable)

        missing = classify_amazon_page(
            "Przepraszamy. Wyszukiwana strona nie istnieje.",
            not_found_code="product_not_found",
            purpose="product",
        )
        self.assertEqual(missing.code, "product_not_found")
        self.assertFalse(missing.retryable)

    def test_no_result_markers_follow_each_legacy_parser_order(self) -> None:
        customer_service = "Welcome to Amazon Customer Service"
        merchant = classify_amazon_page(
            customer_service,
            not_found_code="merchant_has_no_products",
            purpose="merchant",
        )
        merchant_products = classify_amazon_page(
            customer_service,
            not_found_code="merchant_page_has_no_products",
            purpose="merchant_products",
        )
        self.assertEqual(merchant.code, "merchant_has_no_products")
        self.assertIsNone(merchant_products)

        mixed = '{"isEmpty": true} No results for seller'
        retry = classify_amazon_page(
            mixed,
            not_found_code="merchant_page_has_no_products",
            purpose="merchant_products",
        )
        self.assertEqual(retry.code, "upstream_incomplete")
        self.assertTrue(retry.retryable)

        rank = classify_amazon_page(
            '{"isEmpty": true} Sorry, there are no Best Sellers available in this category.',
            not_found_code="no_rank_results",
            purpose="rank_list",
        )
        self.assertEqual(rank.code, "no_rank_results")
        self.assertFalse(rank.retryable)

    def test_legacy_product_completeness_retries_partial_documents(self) -> None:
        no_histogram = classify_product_completeness(
            "<html><script>jQuery.parseJSON('{}')</script></html>"
        )
        self.assertEqual(no_histogram.code, "upstream_incomplete")
        self.assertTrue(no_histogram.retryable)

        no_state = classify_product_completeness(
            '<div id="cm_cr_dp_d_rating_histogram"></div>'
        )
        self.assertEqual(no_state.code, "upstream_incomplete")

        unsupported = classify_product_completeness(
            '<div id="cm_cr_dp_d_rating_histogram"></div>'
            '<script>jQuery.parseJSON(\'{}\')</script>'
            'page:{pageType: "Search", subPageType:'
        )
        self.assertEqual(unsupported.code, "unsupported_product_type")
        self.assertFalse(unsupported.retryable)

    def test_collection_completeness_preserves_one_page_and_zero_merchant(self) -> None:
        one_page = classify_collection_completeness(
            "search",
            '<span>49-19 of 19 results for</span> \\"listitem\\" data-asin=\\"B000000001\\"',
            page=2,
        )
        self.assertEqual(one_page.code, "one_page_only")
        self.assertFalse(one_page.retryable)

        partial = classify_collection_completeness(
            "search", "<html></html>", page=1
        )
        self.assertEqual(partial.code, "upstream_incomplete")
        self.assertTrue(partial.retryable)

        empty_merchant = classify_collection_completeness(
            "merchant_products", '{"totalResultCount":0}'
        )
        self.assertEqual(empty_merchant.code, "merchant_page_has_no_products")


if __name__ == "__main__":
    unittest.main()
