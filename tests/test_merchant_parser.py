from __future__ import annotations

import unittest
from pathlib import Path

from amazon_crawler.plugins.merchant_parser import (
    MERCHANT_FIELDS,
    MERCHANT_PRODUCT_FIELDS,
    parse_merchant_home,
    parse_merchant_html,
    parse_merchant_products,
)


FIXTURES = Path(__file__).parent / "fixtures"


class MerchantParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.detail_html = (FIXTURES / "merchant_detail.html").read_text(encoding="utf-8")
        self.home_html = (FIXTURES / "merchant_home.html").read_text(encoding="utf-8")
        self.products_html = (FIXTURES / "search_stream.txt").read_text(encoding="utf-8")
        self.task = {
            "market_id": "ATVPDKIKX0DER",
            "seller_id": "SELLER123",
            "post_code": "10001",
            "add_date": "2026-08-22",
            "page": 1,
            "source_task_id": "origin-1",
        }

    def test_merchant_detail_contract(self) -> None:
        item = parse_merchant_html(
            self.detail_html,
            seller_id="SELLER123",
            marketplace_id="ATVPDKIKX0DER",
            url="https://www.amazon.com/sp?seller=SELLER123",
        )
        self.assertEqual(set(item), set(MERCHANT_FIELDS))
        self.assertEqual(item["seller_name"], "Example Merchant")
        self.assertEqual(item["business_name"], "Example LLC")
        self.assertEqual(item["business_address"], "1 Example Street New York")

    def test_merchant_home_caps_legacy_pages_at_three(self) -> None:
        tasks = parse_merchant_home(self.home_html, self.task)
        self.assertEqual([task["page"] for task in tasks], [1, 2, 3])
        self.assertTrue(all(task["source_task_id"] == "origin-1" for task in tasks))

    def test_merchant_products_contract(self) -> None:
        items = parse_merchant_products(
            self.products_html,
            self.task,
            base_url="https://www.amazon.com",
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0]), set(MERCHANT_PRODUCT_FIELDS))
        self.assertEqual(items[0]["seller_id"], "SELLER123")
        self.assertEqual(items[0]["data_asin"], "B000000010")


if __name__ == "__main__":
    unittest.main()
