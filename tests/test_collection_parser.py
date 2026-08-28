from __future__ import annotations

import unittest
from pathlib import Path

from amazon_crawler.plugins.collection_parser import (
    CATEGORY_FIELDS,
    RANK_FIELDS,
    REVIEW_FIELDS,
    SEARCH_FIELDS,
    parse_category_html,
    parse_rank_html,
    parse_reviews_html,
    parse_search_html,
)


FIXTURES = Path(__file__).parent / "fixtures"


class CollectionParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.search_html = (FIXTURES / "search_stream.txt").read_text(encoding="utf-8")
        self.category_html = (FIXTURES / "category.html").read_text(encoding="utf-8")
        self.reviews_html = (FIXTURES / "reviews.html").read_text(encoding="utf-8")
        self.rank_html = (FIXTURES / "rank.html").read_text(encoding="utf-8")
        self.task = {
            "market_id": "ATVPDKIKX0DER",
            "post_code": "10001",
            "keyword": "example",
            "turn_page": 2,
            "frequent": 1,
            "add_date": "2026-08-22",
        }

    def test_search_and_category_contracts(self) -> None:
        rows = parse_search_html(self.search_html, self.task, base_url="https://www.amazon.com")
        self.assertEqual(len(rows), 1)
        first = rows[0]
        self.assertEqual(set(first), set(SEARCH_FIELDS))
        self.assertEqual(first["data_asin"], "B000000010")
        self.assertEqual(first["reviews_ratings"], "123")
        self.assertEqual(first["page"], 2)
        self.assertTrue(first["is_sponsored"])
        self.assertEqual(first["link"], "/dp/B000000010")

        category_task = {**self.task, "category_id": "100", "page": 2}
        category = parse_category_html(
            self.category_html, category_task, base_url="https://www.amazon.com"
        )
        self.assertEqual(category[0]["category_id"], "100")
        self.assertEqual(set(category[0]), set(CATEGORY_FIELDS))
        self.assertNotIn("keyword", category[0])

    def test_search_preserves_legacy_quote_rewrite_inside_html_text(self) -> None:
        source = self.search_html.replace(
            "Delivery tomorrow",
            r"Say \"hello\" true false null",
        )

        rows = parse_search_html(
            source,
            self.task,
            base_url="https://www.amazon.com",
        )

        self.assertEqual(
            rows[0]["delivery_information"],
            "Say 'hello' 1 0 ''",
        )

    def test_search_delivery_excludes_embedded_script_and_style_text(self) -> None:
        source = self.search_html.replace(
            "Delivery tomorrow",
            "<style>.delivery { color: blue; }</style>"
            "<span>Delivery tomorrow</span>"
            "<script>window.deliveryNoise = true;</script>",
        )

        rows = parse_search_html(
            source,
            self.task,
            base_url="https://www.amazon.com",
        )

        self.assertEqual(rows[0]["delivery_information"], "Delivery tomorrow")

    def test_category_rank_counts_only_rows_with_an_asin(self) -> None:
        source = self.category_html.replace(
            '<div role="listitem"',
            '<div role="listitem"></div><div role="listitem"',
            1,
        )

        rows = parse_category_html(
            source,
            {**self.task, "category_id": "100", "page": 1},
            base_url="https://www.amazon.com",
        )

        self.assertEqual(rows[0]["search_rank"], 1)

    def test_review_contract_and_video(self) -> None:
        rows = parse_reviews_html(
            self.reviews_html,
            {**self.task, "asin": "B000000010"},
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(set(REVIEW_FIELDS).issubset(rows[0]))
        self.assertEqual(rows[0]["review_title"], "Excellent")
        self.assertEqual(rows[0]["video"], "https://example.test/video")

    def test_rank_contract_uses_metadata_rank(self) -> None:
        rows, continuation = parse_rank_html(
            self.rank_html,
            {
                "url_type": "bestsellers",
                "market_id": "ATVPDKIKX0DER",
                "category_id": "100",
            },
            base_url="https://www.amazon.com",
        )
        self.assertIsNone(continuation)
        self.assertEqual(len(rows), 1)
        self.assertTrue(set(RANK_FIELDS).issubset(rows[0]))
        self.assertEqual(rows[0]["asin"], "B000000020")
        self.assertEqual(rows[0]["ranking"], "1")

    def test_legacy_decimal_comma_price_and_rating_are_normalized(self) -> None:
        source = self.search_html.replace("$18.50", "R$ 1.234,56").replace(
            "4.5 out of 5 stars", "4,5 de 5 estrelas"
        )
        rows = parse_search_html(
            source,
            {**self.task, "market_id": "A2Q3Y263D00KWC"},
            base_url="https://www.amazon.com.br",
        )
        self.assertEqual(rows[0]["selling_price"], "R$ 1234.56")
        self.assertEqual(rows[0]["reviews_stars"], "4.5 de 5 estrelas")


if __name__ == "__main__":
    unittest.main()
