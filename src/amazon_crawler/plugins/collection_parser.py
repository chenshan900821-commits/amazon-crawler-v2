from __future__ import annotations

import ast
import html as html_lib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lxml import html as lxml_html


SEARCH_FIELDS = (
    "brand", "coupon_info", "crawl_date", "data_asin", "data_hour",
    "delivery_information", "frequent", "is_sponsored", "keyword", "link",
    "main_img_url", "market_id", "number_of_sub_asin", "page", "post_code",
    "reviews_ratings", "reviews_stars", "sales_volume", "scribing_price",
    "search_rank", "selling_price", "small_business", "task_date", "title",
    "deal_badge",
)
SEARCH_HOUR_FIELDS = tuple(field for field in SEARCH_FIELDS if field != "deal_badge")
CATEGORY_FIELDS = (
    "data_asin", "market_id", "post_code", "category_id", "title",
    "reviews_stars", "reviews_ratings", "sales_volume", "search_rank", "page",
    "selling_price", "scribing_price", "delivery_information", "main_img_url",
    "link", "coupon_info", "brand", "number_of_sub_asin", "is_sponsored",
    "small_business", "crawl_date", "task_date",
)
REVIEW_FIELDS = (
    "market_id", "asin", "review_title", "review_text", "imgs", "review_stars",
    "review_date", "attributes", "video", "crawl_date", "task_date",
)
RANK_FIELDS = (
    "list_type", "category_name", "market_place_id", "category_id", "asin",
    "main_image_url", "href", "title", "reviews_stars", "reviews_ratings",
    "selling_price", "created_at", "ranking",
)


def _clean(value: str | None) -> str | None:
    normalized = " ".join((value or "").replace("\u200e", "").replace("\u200f", "").split())
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


def _all(node: Any, expression: str) -> list[str]:
    result: list[str] = []
    for value in node.xpath(expression):
        if hasattr(value, "text_content"):
            value = value.text_content()
        cleaned = _clean(str(value))
        if cleaned:
            result.append(cleaned)
    return result


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _int_text(value: str | None) -> str:
    match = re.search(r"[\d.,]+", value or "")
    return re.sub(r"\D", "", match.group(0)) if match else ""


def _legacy_price(value: str | None, marketplace_id: str) -> str:
    rendered = (value or "").replace("\xa0", "")
    decimal_comma_markets = {
        "A2Q3Y263D00KWC",  # Brazil
        "AE08WJ6YKNBMC",  # South Africa
        "A1C3SOZRARQ6R3",  # Poland
        "A33AVAJ2PDY3EV",  # Turkey
    }
    if marketplace_id in decimal_comma_markets:
        return rendered.replace(".", "").replace(",", ".")
    return rendered.replace(",", "")


def _xpath_string(node: Any, expression: str) -> str:
    value = node.xpath(f"string({expression})")
    return str(value) if value is not None else ""


def _stream_search_records(source: str) -> list[tuple[Any, str]]:
    records: list[tuple[Any, str]] = []
    for chunk in source.split("&&&"):
        rendered = chunk.strip().strip(",")
        if not rendered:
            continue
        try:
            record = json.loads(rendered)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(record, list)
            or len(record) <= 2
            or not isinstance(record[1], str)
            or "data-main-slot:search-result" not in record[1]
            or not isinstance(record[2], dict)
            or not isinstance(record[2].get("html"), str)
        ):
            continue
        # The legacy parser performs these global text replacements before
        # evaluating the stream. Preserve their observable effect inside HTML
        # attributes/text too (for example nsdOptOutParam=true becomes =1).
        legacy_html = (
            record[2]["html"]
            .replace("null", '""')
            .replace("false", "0")
            .replace("true", "1")
            .replace('"', "'")
        )
        records.append(
            (
                lxml_html.fromstring(legacy_html),
                str(record[2].get("asin") or ""),
            )
        )
    return records


def parse_search_html(
    source: str,
    task: dict[str, Any],
    *,
    base_url: str,
    include_deal_badge: bool = True,
) -> list[dict[str, Any]]:
    del base_url  # Legacy rows deliberately keep Amazon's relative product href.
    items: list[dict[str, Any]] = []
    for rank, (node, record_asin) in enumerate(_stream_search_records(source), start=1):
        reviews_label = _xpath_string(
            node,
            '//div[@data-cy="reviews-block"]//a[contains(@href, "Reviews")]/@aria-label',
        )
        remaining = _xpath_string(
            node,
            '//div[@class="a-section a-spacing-none a-text-center"]//*[@data-csa-c-swatch-remaining-count]/@data-csa-c-swatch-remaining-count',
        )
        if not remaining:
            remaining = _xpath_string(
                node,
                'count(//div[@class="a-section a-spacing-none a-text-center"]//div[contains(@class, "s-color-swatch-outer-circle")])',
            )
        remaining_match = re.search(r"(\d+)", remaining)
        selling_price = _xpath_string(
            node,
            '//div[@data-cy="price-recipe"]//span[@class="a-price"]/span[@class="a-offscreen"]/text()',
        ).replace("\xa0", "")
        scribing_price = _xpath_string(
            node,
            '//div[@data-cy="price-recipe"]//span[@class="a-price"]/following-sibling::div/span[2]/span[@class="a-offscreen"]/text()',
        ).replace("\xa0", "")
        item = {
            "data_hour": task.get("data_hour") or _now(),
            "frequent": task.get("frequent", 0),
            "data_asin": record_asin,
            "market_id": task["market_id"],
            "post_code": task.get("post_code"),
            "keyword": task.get("keyword"),
            "title": _xpath_string(node, '//div[@data-cy="title-recipe"]/a/h2/span/text()'),
            "reviews_stars": _xpath_string(node, '//i[@data-cy="reviews-ratings-slot"]/span/text()').replace(",", "."),
            "reviews_ratings": reviews_label.split(" ")[0].replace(",", "").replace(".", "") if reviews_label else "",
            "sales_volume": _xpath_string(node, '//div[@data-cy="reviews-block"]/div[2]/span/text()'),
            "search_rank": rank,
            "page": int(task.get("turn_page") or task.get("page") or 1),
            "selling_price": _legacy_price(selling_price, task["market_id"]),
            "scribing_price": _legacy_price(scribing_price, task["market_id"]),
            "delivery_information": " ".join(node.xpath('//div[@data-cy="delivery-block"]/div[contains(@class, "delivery-message")]//text()')),
            "main_img_url": _xpath_string(node, '//span[@data-component-type="s-product-image"]//img/@src'),
            "link": _xpath_string(node, '//div[@data-cy="title-recipe"]/a/@href'),
            "coupon_info": _xpath_string(node, '//div[@data-cy="price-recipe"]/div[2]/span/span[2]/span[1]/text()|//div[@data-cy="price-recipe"]/div[1]/span//text()'),
            "brand": _xpath_string(node, '//div[@data-cy="title-recipe"]//span[@class="a-size-base-plus a-color-base"]/text()|//div[@data-cy="title-recipe"]//div[@class="a-row a-color-base"]/span[contains(@class, "a-size-base")]/text()|//div[@data-cy="title-recipe"]//span[@class="a-size-base a-color-secondary puis-normal-weight-text"]/text()'),
            "number_of_sub_asin": remaining_match.group(1) if remaining_match else "",
            "is_sponsored": "AdHolder" in _xpath_string(node, '//div[@data-component-type="s-search-result"]/@class'),
            "small_business": "Small Business" in _xpath_string(node, '//div[@data-cy="certification-recipe"]//span[@class="a-size-base a-color-base"]/text()'),
            "crawl_date": _now(),
            "task_date": task.get("add_date"),
        }
        if include_deal_badge:
            item["deal_badge"] = _xpath_string(
                node,
                '//div[@data-cy="price-recipe"]//span[@class="a-badge-text"]/text()',
            )
        items.append(item)
    return items


def parse_category_html(source: str, task: dict[str, Any], *, base_url: str) -> list[dict[str, Any]]:
    del base_url
    tree = lxml_html.fromstring(source)
    rows: list[dict[str, Any]] = []
    for node in tree.xpath('//div[@role="listitem"]'):
        asin = _xpath_string(node, './@data-asin')
        if not asin:
            continue
        rank = len(rows) + 1
        if task["market_id"] == "ATVPDKIKX0DER":
            reviews_ratings = _xpath_string(
                node,
                './/div[@data-cy="reviews-block"]//span[@class="rush-component"]//a/@aria-label',
            ).replace(",", "").replace(".", "").replace(" ratings", "")
        else:
            reviews_ratings = (
                _xpath_string(
                    node,
                    './/div[@data-cy="reviews-block"]/div/a/@aria-label',
                )
                .replace(",", "")
                .replace(".", "")
                .replace("\xa0", "")
                .split(" ")[0]
            )
        selling_price = _xpath_string(
            node,
            './/div[@data-cy="price-recipe"]//span[@class="a-price"]/span[@class="a-offscreen"]/text()',
        ).replace("\xa0", "")
        scribing_price = _xpath_string(
            node,
            './/div[@data-cy="price-recipe"]//span[@class="a-price"]/following-sibling::div/span[2]/span[@class="a-offscreen"]/text()',
        ).replace("\xa0", "")
        rows.append(
            {
                "data_asin": asin,
                "market_id": task["market_id"],
                "post_code": task.get("post_code"),
                "category_id": task["category_id"],
                "title": _xpath_string(
                    node, './/div[@data-cy="title-recipe"]/a/h2/span/text()'
                ),
                "reviews_stars": _xpath_string(
                    node, './/i[@data-cy="reviews-ratings-slot"]/span/text()'
                ).replace(",", "."),
                "reviews_ratings": reviews_ratings,
                "sales_volume": _xpath_string(
                    node,
                    './/div[@data-cy="reviews-block"]/div/span[@class="a-size-base a-color-secondary"]/text()',
                ),
                "search_rank": rank,
                "page": task["page"],
                "selling_price": _legacy_price(selling_price, task["market_id"]),
                "scribing_price": _legacy_price(scribing_price, task["market_id"]),
                "delivery_information": " ".join(
                    node.xpath(
                        './/div[@data-cy="delivery-recipe"]//div[contains(@class, "delivery-message")]//text()'
                    )
                ),
                "main_img_url": _xpath_string(
                    node,
                    './/span[@data-component-type="s-product-image"]//img/@src',
                ),
                "link": _xpath_string(
                    node, './/div[@data-cy="title-recipe"]/a/@href'
                ),
                "coupon_info": _xpath_string(
                    node,
                    './/div[@data-cy="price-recipe"]/div[2]/span/span[2]/span[1]/text()|//div[@data-cy="price-recipe"]/div[1]/span//text()',
                ),
                "brand": _xpath_string(
                    node,
                    './/div[@data-cy="title-recipe"]//span[@class="a-size-base-plus a-color-base"]/text()|.//div[@data-cy="title-recipe"]//div[@class="a-row a-color-base"]/span[contains(@class, "a-size-base")]/text()|.//div[@data-cy="title-recipe"]//span[@class="a-size-base a-color-secondary puis-normal-weight-text"]/text()',
                ),
                "number_of_sub_asin": _xpath_string(
                    node,
                    './/div[@class="a-section a-spacing-none a-text-center"]//*[@data-csa-c-swatch-remaining-count]/@data-csa-c-swatch-remaining-count',
                ),
                "is_sponsored": "AdHolder"
                in _xpath_string(
                    node,
                    './div/parent::div[@data-component-type="s-search-result"]/@class',
                ),
                "small_business": "Small Business"
                in _xpath_string(
                    node,
                    '//div[@data-cy="certification-recipe"]//span[@class="a-size-base a-color-base"]/text()',
                ),
                "crawl_date": _now(),
                "task_date": task.get("add_date"),
            }
        )
    return rows


def parse_reviews_html(source: str, task: dict[str, Any]) -> list[dict[str, Any]]:
    tree = lxml_html.fromstring(source)
    rating_text = _xpath_string(
        tree,
        '//a[@id="acrCustomerReviewLink"]/span[@id="acrCustomerReviewText"]/text()',
    )
    if not _int_text(rating_text):
        return []
    items: list[dict[str, Any]] = []
    for review in tree.xpath('//*[@data-hook="review"]'):
        text_values = review.xpath(
            './div/div[@data-hook="reviewText"]//div[@data-hook="reviewRichContentContainer"]//span/text()'
        )
        raw_video = _xpath_string(
            review, './/div[@data-hook="reviewVideo"]/@data-widget-model'
        )
        video = ""
        if raw_video:
            video = str(json.loads(raw_video)["initialVideo"]["shareUrl"])
        items.append(
            {
                "market_id": task["market_id"],
                "asin": task["asin"],
                "review_title": _xpath_string(
                    review, './a/h5[@data-hook="reviewTitle"]/text()'
                ),
                "review_text": "".join(str(value) for value in text_values),
                "imgs": [
                    str(value)
                    for value in review.xpath(
                        './/img[@data-hook="review-image-tile"]/@src'
                    )
                ],
                "review_stars": _xpath_string(
                    review,
                    './div/i[@data-hook="review-star-rating"]/span/text()',
                ),
                "review_date": _xpath_string(
                    review, './div/span[@data-hook="review-date"]/text()'
                ),
                "attributes": _xpath_string(
                    review,
                    './div[@data-hook="product-variation-attributes"]/a/span/text()',
                ),
                "video": video,
                "crawl_date": _now(),
                "task_date": task.get("add_date"),
            }
        )
    return items


@dataclass(frozen=True, slots=True)
class RankContinuation:
    acp_path: str
    acp_params: str
    remaining: tuple[dict[str, Any], ...]
    start_offset: int


def _metadata(source: str) -> list[dict[str, Any]]:
    if not source:
        return []
    decoded = html_lib.unescape(source)
    try:
        value = json.loads(decoded)
    except json.JSONDecodeError:
        try:
            value = ast.literal_eval(decoded)
        except (ValueError, SyntaxError):
            return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _legacy_rank_category_tree(tree: Any) -> str | None:
    current = tree.xpath(
        '//span[contains(@class, "selected") and @aria-current="page"]'
    )
    if not current:
        return None
    current_values = current[0].xpath('text()')
    if not current_values:
        return None
    current_category = str(current_values[0]).strip()
    middle_categories: list[str] = []
    parent_rows = current[0].xpath(
        './ancestor::ul[1]/parent::li/preceding-sibling::li'
    )
    if parent_rows:
        parent_values = parent_rows[0].xpath('.//a/text()')
        if parent_values:
            parent = str(parent_values[0]).strip()
            if parent and parent != current_category:
                middle_categories.append(parent)
    parent_categories = [
        str(value).strip()
        for value in tree.xpath(
            '//li[contains(@class, "browse-up")]//a[preceding-sibling::span[text()="‹"]]/text()'
        )
        if str(value).strip()
    ]
    if not parent_categories:
        return None
    parent_categories = parent_categories[1:]
    ordered: list[str] = []
    for category in [*parent_categories, *middle_categories, current_category]:
        if category and category not in ordered:
            ordered.append(category)
    return " > ".join(ordered).replace("&amp;", "&") if ordered else None


def _legacy_rank_price(value: str, marketplace_id: str) -> float | None:
    if not value:
        return None
    normalized = value.replace("\xa0", "")
    if marketplace_id in {
        "A2Q3Y263D00KWC",
        "AE08WJ6YKNBMC",
        "A1C3SOZRARQ6R3",
        "A33AVAJ2PDY3EV",
    }:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif marketplace_id in {
        "A1VC38T7YXB528",
        "A28R8C7NBKEWEA",
        "A1AM78C64UM0Y8",
        "A17E79C6D8DWNP",
        "A1RKKUPIHCS9HS",
        "APJ6JRA9NG5V4",
        "A13V1IB3VIYZZH",
        "ATVPDKIKX0DER",
        "A2NODRKZP88ZB9",
        "A39IBJ37TRP1C6",
        "A2VIGQ35RCS4UG",
        "A2EUQ1WTGCTBG2",
        "A1805IZSGTT6HS",
        "AMEN7PMS3EDWL",
        "A1PA6795UKMFR9",
        "A1F83G8C2ARO7P",
    }:
        normalized = normalized.replace(",", "")
    else:
        normalized = normalized.replace(",", ".")
    match = re.search(r"(\d+.\d+)", normalized)
    return float(match.group(1)) if match else None


def parse_rank_html(
    source: str,
    task: dict[str, Any],
    *,
    base_url: str,
) -> tuple[list[dict[str, Any]], RankContinuation | None]:
    del base_url  # Legacy rank rows preserve relative hrefs.
    tree = lxml_html.fromstring(source)
    grid = tree.xpath('//*[@class="p13n-desktop-grid"]')
    grid_node = grid[0] if grid else tree
    metadata = _metadata(grid_node.get("data-client-recs-list") or "") if grid else []
    rank_by_asin = {
        str(item.get("id")): str(item.get("metadataMap", {}).get("render.zg.rank"))
        for item in metadata
        if item.get("id")
    }
    category_name = _legacy_rank_category_tree(tree)
    items: list[dict[str, Any]] = []
    nodes = tree.xpath('//div[@data-asin]')
    for node in nodes:
        asin = _xpath_string(node, './@data-asin')
        if not asin:
            continue
        star_text = _xpath_string(
            node, './div[2]/span/div/div/div/div[1]//a/i/span/text()'
        )
        star_match = re.search(r"(\d+[,.]\d+)", star_text)
        ratings_text = _xpath_string(
            node, './div[2]/span/div/div/div/div[1]//a/span/text()'
        )
        items.append(
            {
                "list_type": task["url_type"],
                "category_name": category_name,
                "market_place_id": task["market_id"],
                "category_id": task["category_id"],
                "asin": asin,
                "main_image_url": _xpath_string(node, './div[2]//img/@src'),
                "href": _xpath_string(node, './div[2]/span/div/a/@href'),
                "title": _xpath_string(
                    node, './div[2]//a[@role="link"]/span/div/text()'
                ),
                "reviews_stars": star_match.group(1) if star_match else None,
                "reviews_ratings": int(_int_text(ratings_text) or 0),
                "selling_price": _legacy_rank_price(
                    _xpath_string(
                        node, './div[2]//span[contains(@class,"price")]//text()'
                    ),
                    task["market_id"],
                ),
                "created_at": _now(),
                "ranking": rank_by_asin.get(asin, ""),
            }
        )
    offset = int(grid_node.get("data-index-offset") or len(items)) if grid else len(items)
    total = int(grid_node.get("data-offset") or len(metadata)) if grid else len(metadata)
    acp_path = grid_node.get("data-acp-path") if grid else None
    acp_params = grid_node.get("data-acp-params") if grid else None
    continuation = None
    if metadata and total > offset and acp_path and acp_params:
        continuation = RankContinuation(acp_path, acp_params, tuple(metadata[offset:total]), offset)
    return items, continuation
