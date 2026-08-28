from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from lxml import html as lxml_html

from amazon_crawler.plugins.collection_parser import (
    _legacy_price,
    _stream_search_records,
    _xpath_string,
)


MERCHANT_FIELDS = (
    "seller_id",
    "market_id",
    "seller_name",
    "url",
    "business_name",
    "business_address",
    "crawl_date",
)
MERCHANT_PRODUCT_FIELDS = (
    "market_id", "seller_id", "data_asin", "page", "product_rank", "title",
    "link", "main_img_url", "reviews_stars", "reviews_ratings", "coupon_info",
    "scribing_price", "selling_price", "sales_volume", "delivery_information",
    "crawl_date", "task_date", "deal_badge", "number_of_sub_asin",
)


def _clean(value: str | None) -> str | None:
    normalized = " ".join((value or "").split())
    return normalized or None


def _first(node: Any, *expressions: str) -> str | None:
    for expression in expressions:
        for value in node.xpath(expression):
            if hasattr(value, "text_content"):
                value = value.text_content()
            cleaned = _clean(str(value))
            if cleaned:
                return cleaned
    return None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def parse_merchant_html(
    source: str,
    *,
    seller_id: str,
    marketplace_id: str,
    url: str,
) -> dict[str, Any]:
    tree = lxml_html.fromstring(source)
    return {
        "seller_id": seller_id,
        "market_id": marketplace_id,
        "seller_name": _xpath_string(
            tree,
            '//div[@class="a-box-inner a-padding-medium"]/div[@id="seller-info-card"]//div[@id="seller-desc-column"]/div[@class="a-row a-spacing-small"][1]/h1[@id="seller-name"]/text()',
        ),
        "url": url,
        "business_name": _xpath_string(
            tree,
            '//div[@id="page-section-detail-seller-info"]/div[@class="a-column a-span12 a-spacing-none"]/div[@class="a-box a-spacing-none a-color-base-background box-section"]/div[@class="a-box-inner a-padding-medium"]/div[@class="a-row a-spacing-none"][1]/span[last()]/text()',
        ),
        "business_address": " ".join(
            str(value)
            for value in tree.xpath(
                '//div[@id="page-section-detail-seller-info"]/div[@class="a-column a-span12 a-spacing-none"]/div[@class="a-box a-spacing-none a-color-base-background box-section"]/div[@class="a-box-inner a-padding-medium"]/div[@class="a-row a-spacing-none indent-left"]/span/text()'
            )
        ),
        "crawl_date": _now(),
    }


def parse_merchant_home(source: str, task: dict[str, Any]) -> list[dict[str, Any]]:
    match = re.search(r'["\']totalResultCount["\']\s*:\s*(\d+)', source)
    if not match:
        raise ValueError("merchant totalResultCount is missing")
    total = int(match.group(1))
    if total <= 0:
        return []
    pages = 3 if total > 32 else (total + 15) // 16
    return [
        {
            "market_id": task["market_id"],
            "seller_id": task["seller_id"],
            "page": page,
            "post_code": task.get("post_code"),
            "add_date": task.get("add_date"),
            "state": 0,
            **(
                {"source_task_id": task["source_task_id"]}
                if task.get("source_task_id") is not None
                else {}
            ),
        }
        for page in range(1, pages + 1)
    ]


def parse_merchant_products(
    source: str,
    task: dict[str, Any],
    *,
    base_url: str,
) -> list[dict[str, Any]]:
    del base_url
    result: list[dict[str, Any]] = []
    for rank, (node, record_asin) in enumerate(
        _stream_search_records(source), start=1
    ):
        reviews_label = _xpath_string(
            node,
            '//div[@data-cy="reviews-block"]//a[contains(@href, "Reviews")]/@aria-label',
        )
        selling_price = _xpath_string(
            node,
            '//div[@data-cy="price-recipe"]//span[@class="a-price"]/span[@class="a-offscreen"]/text()',
        ).replace("\xa0", "")
        scribing_price = _xpath_string(
            node,
            '//div[@data-cy="price-recipe"]//span[@class="a-price"]/following-sibling::div/span[2]/span[@class="a-offscreen"]/text()',
        ).replace("\xa0", "")
        page_color = node.xpath(
            'count(//div[@class="s-color-swatch-internal-container"]/div[contains(@class, "s-color-swatch-outer-circle")])'
        )
        other_color = _xpath_string(
            node, '//*[@data-csa-c-swatch-remaining-count]/@data-csa-c-swatch-remaining-count'
        )
        other_match = re.search(r"(\d+)", other_color)
        number_of_sub_asin = str(
            (int(page_color) if page_color else 0)
            + (int(other_match.group(1)) if other_match else 0)
        )
        result.append(
            {
                "market_id": task["market_id"],
                "seller_id": task["seller_id"],
                "data_asin": record_asin,
                "page": task["page"],
                "product_rank": rank,
                "title": _xpath_string(
                    node, '//div[@data-cy="title-recipe"]/a/h2/span/text()'
                ),
                "link": _xpath_string(
                    node, '//div[@data-cy="title-recipe"]/a/@href'
                ),
                "main_img_url": _xpath_string(
                    node, '//span[@data-component-type="s-product-image"]//img/@src'
                ),
                "reviews_stars": _xpath_string(
                    node, '//i[@data-cy="reviews-ratings-slot"]/span/text()'
                ).replace(",", "."),
                "reviews_ratings": reviews_label.split(" ")[0]
                .replace(",", "")
                .replace(".", "")
                if reviews_label
                else "",
                "coupon_info": _xpath_string(
                    node,
                    '//div[@data-cy="price-recipe"]/div[2]/span/span[2]/span[1]/text()|//div[@data-cy="price-recipe"]/div[1]/span//text()',
                ),
                "scribing_price": _legacy_price(
                    scribing_price, task["market_id"]
                ),
                "selling_price": _legacy_price(selling_price, task["market_id"]),
                "sales_volume": _xpath_string(
                    node, '//div[@data-cy="reviews-block"]/div[2]/span/text()'
                ),
                "delivery_information": " ".join(
                    node.xpath(
                        '//div[@data-cy="delivery-block"]/div[contains(@class, "delivery-message")]//text()[not(ancestor::script) and not(ancestor::style) and not(ancestor::noscript) and not(ancestor::template)]'
                    )
                ),
                "crawl_date": _now(),
                "task_date": task.get("add_date"),
                "deal_badge": _xpath_string(
                    node,
                    '//div[@data-cy="price-recipe"]//span[@class="a-badge-text"]/text()',
                ),
                "number_of_sub_asin": number_of_sub_asin,
            }
        )
    return result
