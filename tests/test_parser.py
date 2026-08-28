from __future__ import annotations

import json
import unittest
from pathlib import Path

from amazon_crawler.plugins.amazon_parser import (
    LEGACY_DIMENSION_FIELDS,
    LEGACY_PRODUCT_FIELDS,
    looks_blocked,
    parse_product_html,
)
from amazon_crawler.plugins.amazon_product import AmazonProductPlugin
from amazon_crawler.domain.errors import ValidationError


class ParserTests(unittest.TestCase):
    def test_parses_core_product_fields_and_quality(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "product.html"
        data = parse_product_html(
            fixture.read_text(),
            asin="B000000001",
            product_url="https://www.amazon.com/dp/B000000001",
        )
        self.assertEqual(data["title"], "Synthetic Test Product")
        self.assertEqual(data["price"], 19.99)
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["seller"], "Example Seller")
        self.assertEqual(data["parent_asin"], "B000000000")
        self.assertEqual(data["quality"]["core_field_coverage"], 1.0)
        self.assertEqual(data["quality"]["legacy_field_coverage"], 1.0)
        self.assertTrue(set(LEGACY_PRODUCT_FIELDS).issubset(data))
        self.assertEqual(data["brand"], "Example")
        self.assertEqual(data["buy_box_seller_id"], "SELLER123")
        self.assertEqual(data["inventory_num"], 21)
        self.assertEqual(data["last_price"], 29.99)
        self.assertEqual(data["coupons"]["save_percent"], 10)
        self.assertEqual(data["generate_date"], "2025-01-02")
        self.assertEqual(len(data["skus"]), 2)
        self.assertEqual(data["skus"][0]["attribute"], [{"name": "Color", "value": "Red"}])
        self.assertEqual(len(data["dimension_items"]), 2)
        self.assertEqual(
            set(data["dimension_items"][0]), set(LEGACY_DIMENSION_FIELDS)
        )
        self.assertEqual(data["redirect_to"], "")

    def test_detects_verification_page(self) -> None:
        self.assertTrue(looks_blocked("Enter the characters you see below"))

    def test_page_canonical_cannot_override_the_safe_request_url(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "product.html"
        source = fixture.read_text().replace(
            "https://www.amazon.com/dp/B000000001",
            "https://www.amazon.com/dp/B000000001?token=page-secret",
            1,
        )
        safe_request = (
            "https://www.amazon.com/dp/B000000001?th=1&psc=1&language=en_US"
        )

        data = parse_product_html(
            source,
            asin="B000000001",
            product_url=safe_request,
            marketplace_id="US",
        )

        self.assertEqual(data["link"], safe_request)
        self.assertEqual(data["canonical_url"], safe_request)
        self.assertNotIn("page-secret", json.dumps(data, sort_keys=True))

    def test_video_detection_keeps_legacy_quote_and_stream_semantics(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "product.html"
        source = fixture.read_text()

        single_quoted = parse_product_html(
            source + "<script>{'isVideo': true}</script>",
            asin="B000000001",
            product_url="https://www.amazon.com/dp/B000000001",
        )
        stream = parse_product_html(
            source + '<script>{"url": "https://media.invalid/video.m3u8"}</script>',
            asin="B000000001",
            product_url="https://www.amazon.com/dp/B000000001",
        )

        self.assertEqual(single_quoted["is_video"], 0)
        self.assertEqual(stream["is_video"], 1)

    def test_availability_and_delivery_exclude_embedded_script_text(self) -> None:
        data = parse_product_html(
            """
            <html><body>
              <span id="productTitle">Synthetic Product</span>
              <div id="availability">
                <style>.availability { color: green; }</style>
                <span>In Stock</span>
                <script>P.when('A').execute(function () { logNoise(); });</script>
              </div>
              <div id="deliveryBlockContainer">
                <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">
                  <style>.delivery { color: blue; }</style>
                  <span>Delivery tomorrow</span>
                  <script>window.deliveryNoise = true;</script>
                </div>
              </div>
            </body></html>
            """,
            asin="B000000001",
            product_url="https://www.amazon.com/dp/B000000001",
        )

        self.assertEqual(data["availability"], "In Stock")
        self.assertIn("Delivery tomorrow", data["delivery_info"])
        self.assertNotIn("logNoise", data["availability"])
        self.assertNotIn("deliveryNoise", data["delivery_info"])

    def test_fbt_missing_optional_values_keep_legacy_empty_strings(self) -> None:
        data = parse_product_html(
            """
            <html>
              <span id="productTitle">Synthetic Product</span>
              <div aria-labelledby="similarities-product-bundle-widget-title">
                <div aria-labelledby="Product 2">
                  <a class="a-link-normal" href="/dp/B000000002/"></a>
                  <div id="ProductTitle-2">Accessory without price</div>
                </div>
              </div>
            </html>
            """,
            asin="B000000001",
            product_url="https://www.amazon.com/dp/B000000001",
        )

        self.assertEqual(
            data["fbt"],
            [
                {
                    "asin": "B000000002",
                    "img": "",
                    "title": "Accessory without price",
                    "price": "",
                }
            ],
        )

    def test_normalizes_url_and_rejects_arbitrary_host(self) -> None:
        plugin = object.__new__(AmazonProductPlugin)
        normalized = plugin.normalize(
            "https://www.amazon.co.uk/gp/product/B000000001?tag=test", None, "SW1A 1AA"
        )
        self.assertEqual(normalized.marketplace_id, "UK")
        self.assertEqual(normalized.asin, "B000000001")
        self.assertEqual(normalized.postal_code, "SW1A 1AA")
        legacy_id = plugin.normalize("B012345678", "ATVPDKIKX0DER", "10001")
        self.assertEqual(legacy_id.marketplace_id, "US")
        with self.assertRaises(ValidationError):
            plugin.normalize("https://example.com/dp/B000000001", None, None)


if __name__ == "__main__":
    unittest.main()
