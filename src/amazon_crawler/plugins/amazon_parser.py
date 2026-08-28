from __future__ import annotations

import json
import re
from html import unescape
from datetime import UTC, datetime
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from lxml import html as lxml_html

from amazon_crawler.plugins.marketplaces import (
    AMAZON_ID_TO_MARKETPLACE,
    DOMAIN_TO_MARKETPLACE,
    MARKETPLACES,
    Marketplace,
    marketplace_language,
)


PARSER_VERSION = "amazon-product-parser/2.1.0"
CORE_FIELDS = ("asin", "title", "price", "availability", "img")
LEGACY_PRODUCT_FIELDS = (
    "a_plus",
    "amazon_choice",
    "amazon_choice_keyword",
    "asin",
    "best_seller_flag",
    "bought",
    "bought_time_unit",
    "brand",
    "breadcrumbs_feature",
    "breadcrumbs_feature_ids",
    "bsr",
    "bullet_points",
    "buy_box_owner",
    "buy_box_seller_id",
    "buy_box_type",
    "city",
    "coupons",
    "created_at",
    "deal_badge",
    "deal_end_time",
    "delivery_info",
    "extra_saving",
    "fba",
    "fbt",
    "first_page_review_imgs",
    "generate_date",
    "img",
    "imgs",
    "inventory_num",
    "is_video",
    "last_price",
    "lighting_deal_claimed",
    "link",
    "marketplace_id",
    "offsale",
    "pasin",
    "price",
    "prime_price",
    "product_description",
    "product_details",
    "product_info",
    "rating_num",
    "redirect_to",
    "review_insight_label",
    "review_summary",
    "score",
    "seller_num",
    "ships_from",
    "sku_num",
    "skus",
    "star_percent",
    "task_id",
    "title",
    "used",
    "zipcode",
)
LEGACY_DIMENSION_FIELDS = (
    "asin",
    "created_at",
    "dimensions",
    "image_url",
    "market_place_id",
    "parent_asin",
    "task_id",
)


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.replace("\u200e", "").replace("\u200f", "").split())
    return normalized or None


def _node_text(value: Any) -> str:
    if hasattr(value, "text_content"):
        return value.text_content()
    return str(value)


def _first(tree: Any, expressions: Iterable[str]) -> str | None:
    for expression in expressions:
        for value in tree.xpath(expression):
            cleaned = _clean(_node_text(value))
            if cleaned:
                return cleaned
    return None


def _all(tree: Any, expression: str) -> list[str]:
    values: list[str] = []
    for item in tree.xpath(expression):
        value = _clean(_node_text(item))
        if value:
            values.append(value)
    return values


def _parse_json_ld(tree: Any) -> dict[str, Any]:
    for raw in tree.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            if isinstance(candidate, dict) and candidate.get("@type") == "Product":
                return candidate
    return {}


def _embedded_json(source: str, key: str) -> Any:
    marker = re.search(rf'["\']{re.escape(key)}["\']\s*:\s*', source)
    if not marker:
        return None
    start = marker.end()
    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source) or source[start] not in "[{":
        return None
    opening = source[start]
    closing = "]" if opening == "[" else "}"
    depth = 0
    in_string = False
    escaped = False
    quote = ""
    for index in range(start, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in {'"', "'"}:
            in_string = True
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                raw = source[start : index + 1]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
    return None


def _number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"[-+]?\d[\d\s.,]*", value.replace("\xa0", " "))
    if not match:
        return None
    raw = match.group(0).replace(" ", "")
    if "," in raw and "." in raw:
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        raw = raw.replace(thousands, "").replace(decimal, ".")
    elif "," in raw:
        parts = raw.split(",")
        raw = "".join(parts) if len(parts[-1]) == 3 else ".".join(parts)
    elif raw.count(".") > 1:
        parts = raw.split(".")
        raw = "".join(parts) if len(parts[-1]) == 3 else "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(raw)
    except ValueError:
        return None


def _integer(value: str | None) -> int:
    if not value:
        return 0
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else 0


def _marketplace(product_url: str, marketplace_id: str | None) -> Marketplace | None:
    selected = (marketplace_id or "").upper()
    if selected in MARKETPLACES:
        return MARKETPLACES[selected]
    if selected in AMAZON_ID_TO_MARKETPLACE:
        return AMAZON_ID_TO_MARKETPLACE[selected]
    host = (urlparse(product_url).hostname or "").lower().removeprefix("www.")
    return DOMAIN_TO_MARKETPLACE.get(host)


def _currency(price_text: str | None, offers: dict[str, Any], market: Marketplace | None) -> str | None:
    declared = _clean(str(offers.get("priceCurrency") or ""))
    if declared:
        return declared
    if price_text:
        for marker, currency in (
            ("US$", "USD"),
            ("CA$", "CAD"),
            ("AU$", "AUD"),
            ("£", "GBP"),
            ("€", "EUR"),
            ("¥", "JPY"),
            ("₹", "INR"),
        ):
            if marker in price_text:
                return currency
    defaults = {
        "US": "USD", "CA": "CAD", "MX": "MXN", "BR": "BRL", "UK": "GBP",
        "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR",
        "BE": "EUR", "IE": "EUR", "SE": "SEK", "PL": "PLN", "JP": "JPY",
        "AU": "AUD", "IN": "INR", "SG": "SGD", "AE": "AED", "SA": "SAR",
        "TR": "TRY", "ZA": "ZAR", "EG": "EGP",
    }
    return defaults.get(market.id) if market else None


def _table_mapping(tree: Any, expressions: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for expression in expressions:
        for row in tree.xpath(expression):
            fields = row.xpath("./th|./td[1]|.//*[contains(@class,'a-text-bold')][1]")
            values = row.xpath("./td[last()]|.//*[contains(@class,'po-break-word')][1]")
            field = _clean(_node_text(fields[0])) if fields else None
            value = _clean(_node_text(values[-1])) if values else None
            if field and value and field != value:
                result[field.rstrip(" :")] = value
    return result


def _detail_bullets(tree: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in tree.xpath('//*[@id="detailBullets_feature_div"]//li'):
        label = _first(item, ['.//*[contains(@class,"a-text-bold")]/text()'])
        if not label:
            continue
        full = _clean(item.text_content()) or ""
        value = _clean(full.replace(label, "", 1))
        if value:
            result[label.rstrip(" :")] = value
    return result


def _variants(source: str) -> list[dict[str, Any]]:
    dimensions = _embedded_json(source, "dimensions")
    display_data = _embedded_json(source, "dimensionValuesDisplayData")
    labels = _embedded_json(source, "variationDisplayLabels")
    if not isinstance(dimensions, list) or not isinstance(display_data, dict):
        return []
    labels = labels if isinstance(labels, dict) else {}
    variants: list[dict[str, Any]] = []
    for variant_asin, values in display_data.items():
        if not isinstance(variant_asin, str) or not isinstance(values, list):
            continue
        attributes = [
            {"name": labels.get(dimension, dimension), "value": value}
            for dimension, value in zip(dimensions, values, strict=False)
        ]
        variants.append({"asin": variant_asin, "attribute": attributes})
    return variants


def _images(tree: Any, source: str) -> list[str]:
    images: list[str] = []
    dynamic = _first(tree, ['//*[@id="landingImage"]/@data-a-dynamic-image'])
    if dynamic:
        try:
            decoded = json.loads(dynamic)
            if isinstance(decoded, dict):
                images.extend(str(url) for url in decoded if str(url).startswith("http"))
        except json.JSONDecodeError:
            pass
    for key in ("hiRes", "large"):
        images.extend(re.findall(rf'["\']{key}["\']\s*:\s*["\'](https?://[^"\']+)', source))
    landing = _first(tree, ['//*[@id="landingImage"]/@data-old-hires', '//*[@id="landingImage"]/@src'])
    if landing:
        images.insert(0, landing)
    return list(dict.fromkeys(images))


def _star_percent(tree: Any) -> dict[str, str] | None:
    result: dict[str, str] = {}
    for row in tree.xpath('//*[@id="histogramTable"]//li|//*[@id="histogramTable"]//tr'):
        text = _clean(row.text_content()) or ""
        match = re.search(r"(\d(?:[.,]\d)?\s*star).*?(\d+%)", text, re.IGNORECASE)
        if match:
            result[match.group(1)] = match.group(2)
    return result or None


def _bsr(tree: Any) -> list[dict[str, str | None]]:
    texts = _all(
        tree,
        '//*[@id="SalesRank"]//*[self::span or self::li]|//*[@id="SalesRank"]|'
        '//*[@id="detailBulletsWrapper_feature_div"]//*[contains(text(),"#")]',
    )
    result: list[dict[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for text in texts:
        for rank, category in re.findall(r"#\s*([\d.,]+)\s+(?:in|en|w|na)\s+([^#(]+)", text, re.I):
            normalized_rank = re.sub(r"\D", "", rank)
            normalized_category = category.strip()
            key = (normalized_rank, normalized_category)
            if normalized_rank and key not in seen:
                seen.add(key)
                result.append(
                    {
                        "rank": normalized_rank,
                        "category_id": None,
                        "category_name": normalized_category,
                    }
                )
    return result


def _fbt(tree: Any) -> list[dict[str, str]] | None:
    result: list[dict[str, str]] = []
    for group in tree.xpath('//*[@aria-labelledby="similarities-product-bundle-widget-title"]//*[@data-asin or contains(@aria-labelledby,"Product")]'):
        link = _first(group, ['.//a[contains(@href,"/dp/")]/@href'])
        match = re.search(r"/dp/([A-Z0-9]{10})", link or "", re.I)
        if not match:
            continue
        result.append(
            {
                "asin": match.group(1).upper(),
                # The legacy helper uses ``extract_first('')`` for all three
                # optional fields.  Preserve that wire contract: downstream
                # legacy tables distinguish an observed FBT row with a missing
                # value (empty string) from an absent FBT section (None).
                "img": _first(group, ['.//img/@src']) or "",
                "title": _first(
                    group,
                    [
                        './/*[contains(@id,"ProductTitle")]//text()',
                        './/img/@alt',
                    ],
                )
                or "",
                "price": _first(
                    group,
                    [
                        './/*[contains(@class,"a-price")]//*[contains(@class,"a-offscreen")]/text()'
                    ],
                )
                or "",
            }
        )
    return result or None


def _date_value(details: dict[str, str]) -> str | None:
    raw = details.get("Date First Available") or details.get("Release date")
    if not raw:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    iso = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    return f"{iso.group(1)}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}" if iso else None


def _xpath_strings(tree: Any, expression: str) -> list[str]:
    values: list[str] = []
    for value in tree.xpath(expression):
        if hasattr(value, "text_content"):
            rendered = value.text_content()
        else:
            rendered = str(value)
        values.append(rendered)
    return values


def _first_raw(tree: Any, expression: str, default: str = "") -> str:
    values = _xpath_strings(tree, expression)
    return values[0] if values else default


def _joined_xpath(tree: Any, expression: str) -> str:
    return " ".join(
        value.strip()
        .replace("\n", "")
        .replace("\t", "")
        .replace("\u200c", "")
        .replace("&zwnj;", "")
        for value in _xpath_strings(tree, expression)
    )


def _regex_first(pattern: str, value: str, default: str = "") -> str:
    match = re.search(pattern, value)
    return match.group(1) if match else default


def _legacy_brand(tree: Any, legacy_marketplace_id: str) -> str:
    brand_info = _first_raw(tree, '//a[@id="bylineInfo"]/text()')
    if ":" in brand_info:
        return brand_info.replace(" ", "").split(":")[-1]
    patterns = {
        "A1AM78C64UM0Y8": r"Visita la tienda de (.*?)",
        "A33AVAJ2PDY3EV": r"(.*?) Store’u ziyaret edin",
    }
    pattern = patterns.get(
        legacy_marketplace_id,
        r"(?:Visit|Visite) (?:the|a) (.*?) (?:Store|insider)",
    )
    return _regex_first(pattern, brand_info)


def _legacy_star_percent(tree: Any) -> dict[str, str] | None:
    stars = _xpath_strings(
        tree,
        "//div[@id='cm_cr_dp_d_rating_histogram']//ul[@id='histogramTable']//li//div[@class='a-section a-spacing-none a-text-left aok-nowrap']/text()",
    )
    percentages = _xpath_strings(
        tree,
        "//div[@id='cm_cr_dp_d_rating_histogram']//ul[@id='histogramTable']//li//div[@class='a-section a-spacing-none a-text-right aok-nowrap']/text()",
    )
    if not stars or not percentages:
        return None
    return {
        star.strip(): percentage.strip()
        for star, percentage in zip(stars, percentages, strict=False)
    }


def _legacy_imgs(tree: Any) -> list[str]:
    script = _first_raw(
        tree,
        '//div[@data-csa-c-slot-id="mediaBlock_feature_div"]/div/script[@type="text/javascript"]/text()',
    )
    if not script:
        return []
    match = re.search(
        r"'colorImages'\s*:\s*\{\s*'initial'\s*:\s*(\[.+\])",
        script,
        re.DOTALL,
    )
    if not match:
        return []
    try:
        values = json.loads(match.group(1).strip())
    except (TypeError, json.JSONDecodeError):
        return []
    images: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        candidate = value.get("hiRes")
        if not candidate:
            candidates = [value.get("large")]
            if isinstance(value.get("main"), dict):
                candidates.extend(value["main"].keys())
            candidate = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, str)
                    and re.search(
                        r"^https?://.+\.(jpe?g|png|webp)(?:$|[?_\.])",
                        item,
                        re.I,
                    )
                ),
                "",
            )
        if isinstance(candidate, str) and candidate:
            images.append(candidate)
    return images


def _legacy_price(value: str, legacy_marketplace_id: str) -> float | None:
    if not value:
        return None
    decimal_comma = {
        "A2Q3Y263D00KWC",
        "AE08WJ6YKNBMC",
        "A1C3SOZRARQ6R3",
        "A33AVAJ2PDY3EV",
    }
    strip_comma = {
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
    }
    normalized = value.replace("\xa0", "")
    if legacy_marketplace_id in decimal_comma:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif legacy_marketplace_id in strip_comma:
        normalized = normalized.replace(",", "")
    else:
        normalized = normalized.replace(",", ".")
    matched = _regex_first(r"(\d+.\d+)", normalized)
    return float(matched) if matched else None


def _legacy_last_price(tree: Any, legacy_marketplace_id: str) -> float | None:
    value = _first_raw(
        tree,
        "//span[@class='a-size-small a-color-secondary aok-align-center basisPrice']/span[@class='a-price a-text-price']/span[2]/text()",
    ).strip()
    if legacy_marketplace_id == "A1VC38T7YXB528" and value:
        matched = _regex_first(
            r"(\d+.\d+)", value.replace("\xa0", "").replace(",", "")
        )
        return float(matched) if matched else None
    return _legacy_price(value, legacy_marketplace_id)


def _legacy_prime_price(tree: Any, legacy_marketplace_id: str) -> float | None:
    value = _first_raw(
        tree,
        '//span[@id="primeExclusivePricingMessage"]/a/span[2]/text()',
    )
    return _legacy_price(value, legacy_marketplace_id)


def _legacy_inventory(tree: Any) -> int:
    stock = _first_raw(tree, '//div[@id="availability"]/span/text()').lower()
    if any(marker in stock for marker in ("in stock", "stokta", "dostępne")):
        if any(marker in stock for marker in ("only", "sadece", "sztuki")):
            value = _regex_first(r"(\d+)", stock)
            return int(value) if value else 0
        return 21
    return 0


def _legacy_mapping(
    tree: Any,
    field_xpath: str,
    value_xpath: str,
    *,
    normalize: bool = False,
) -> dict[str, str] | None:
    fields = [value.strip() for value in _xpath_strings(tree, field_xpath) if value.strip()]
    values = [value.strip() for value in _xpath_strings(tree, value_xpath) if value.strip()]
    if not fields or not values:
        return None
    if normalize:
        def clean(value: str) -> str:
            return (
                re.sub(r"\s+", " ", value)
                .replace("\u200f", "")
                .replace("\u200e", "")
                .replace("\n", "")
                .replace(" :", "")
                .strip()
            )

        return {
            clean(field): clean(value)
            for field, value in zip(fields[: len(values) + 1], values, strict=False)
        }
    return {
        field.strip(): value.strip()
        for field, value in zip(fields, values, strict=False)
    }


def _legacy_product_details(tree: Any) -> dict[str, str] | None:
    return _legacy_mapping(
        tree,
        '//table[@class="a-normal a-spacing-micro"]//tr//td[1]//span[@class="a-size-base a-text-bold"]/text()',
        '//table[@class="a-normal a-spacing-micro"]//tr//td[2]//span[@class="a-size-base po-break-word"]/text()',
    ) or _legacy_mapping(
        tree,
        '//div[@id="productFactsDesktopExpander"]//div[@class="a-fixed-left-grid-col a-col-left"]//span[@class="a-color-base"]/text()',
        '//div[@id="productFactsDesktopExpander"]//div[@class="a-fixed-left-grid-col a-col-right"]//span[@class="a-color-base"]/text()',
    )


def _legacy_product_info(
    tree: Any, legacy_marketplace_id: str
) -> dict[str, str] | None:
    excluded = {
        "A1C3SOZRARQ6R3": "sprzedających",
        "A2Q3Y263D00KWC": "vendidos",
        "A1AM78C64UM0Y8": "vendidos",
        "A33AVAJ2PDY3EV": "Satanlar",
    }.get(legacy_marketplace_id, "eller")
    return _legacy_mapping(
        tree,
        f'//table[contains(@id, "productDetails")]//tr/th[not(contains(text(), "{excluded}"))]/text()',
        f'//table[contains(@id, "productDetails")]//tr/th[not(contains(text(), "{excluded}"))]/following-sibling::td/text()',
        normalize=True,
    ) or _legacy_mapping(
        tree,
        '//div[@id="detailBullets_feature_div"]//li/span[@class="a-list-item"]/span[@class="a-text-bold"]/text()',
        '//div[@id="detailBullets_feature_div"]//li/span[@class="a-list-item"]/span[not(@class="a-text-bold")]/text()',
        normalize=True,
    )


def _legacy_bsr(tree: Any, legacy_marketplace_id: str) -> list[dict[str, Any]]:
    header_terms = ("eller", "vendidos", "sprzedających", "mais vendidos", "Satanlar")
    contains = " or ".join(f'contains(text(), "{term}")' for term in header_terms)
    main_candidates = [
        f'//table[@class="a-keyvalue prodDetTable"]//tr/th[{contains}]/following-sibling::td/span/span[1]',
        f'//table[@class="a-keyvalue prodDetTable"]//tr/th[{contains}]/following-sibling::td/span//li[1]//span',
        '//div[@id="detailBulletsWrapper_feature_div"]/ul[last()-1]/li/span',
        f'//div[@id="detailBulletsWrapper_feature_div"]/div[@id="detailBullets_feature_div"]/ul/li/span/span[1][{contains}]/..',
    ]
    child_candidates = [
        f'//table[@class="a-keyvalue prodDetTable"]//tr/th[{contains}]/following-sibling::td/span/span[2]',
        f'//table[@class="a-keyvalue prodDetTable"]//tr/th[{contains}]/following-sibling::td/span//li[2]//span',
        '//div[@id="detailBulletsWrapper_feature_div"]/ul[last()-1]/li/span/ul//span',
        f'//div[@id="detailBulletsWrapper_feature_div"]/div[@id="detailBullets_feature_div"]/ul/li/span/span[1][{contains}]/../ul//span',
    ]

    def nodes(expressions: list[str]) -> list[Any]:
        for expression in expressions:
            found = tree.xpath(expression)
            if found:
                return found
        return []

    def relative_values(nodes_: list[Any], expression: str) -> list[Any]:
        return [value for node in nodes_ for value in node.xpath(expression)]

    def href_category(nodes_: list[Any]) -> str | None:
        hrefs = relative_values(nodes_, './a/@href')
        try:
            value = str(hrefs[0]).split('/')[-2]
            return value.split("?", 1)[0]
        except (IndexError, AttributeError):
            return None

    main_nodes = nodes(main_candidates)
    result: list[dict[str, Any]] = []
    if main_nodes:
        names = relative_values(main_nodes, './a/text()')
        name = str(names[0]) if names else ""
        rank = "".join(
            re.findall(
                r"\d+",
                "".join(str(v) for v in relative_values(main_nodes, './text()')),
            )
        )
        if legacy_marketplace_id == "A33AVAJ2PDY3EV" and "göster" in name:
            name = _regex_first(
                r"(.*?) Şu kategorideki En Popüler 100 Ürünü göster", name
            )
        elif any(marker in name for marker in ("See", "Ver el", "Zobacz", "Conheça")):
            name = _regex_first(
                r"(?:See|Ver el|Zobacz|Conheça o) Top 100 (?:in|en|w kategorii|na categoria) (.+)",
                name,
            )
        result.append(
            {
                "rank": rank,
                "category_id": href_category(main_nodes),
                "category_name": name,
            }
        )
    child_nodes = nodes(child_candidates)
    if child_nodes:
        names = relative_values(child_nodes, './a/text()')
        text_values = relative_values(child_nodes, './text()')
        result.append(
            {
                "rank": "".join(
                    re.findall(r"\d+", str(text_values[0]) if text_values else "")
                ),
                "category_id": href_category(child_nodes),
                "category_name": str(names[0]) if names else "",
            }
        )
    return result


def _legacy_coupons(tree: Any, price: float | None) -> dict[str, Any]:
    values = [
        value
        for value in _xpath_strings(
            tree, '//span[@class="promoPriceBlockMessage"]/div/span//label/text()'
        )
        if value.strip().replace(" ", "")
    ]
    title = values[0] if values else None
    if not title:
        return {"title": None, "save_price": None, "save_percent": None}
    percent = _regex_first(r"(\d+)%", title)
    if percent:
        saving: float | str | None = price * float(percent) * 0.01 if price else None
    else:
        saving = _regex_first(r"[$|€|£|₹|S$](\d+)", title) or None
    return {
        "title": title.strip(),
        "save_price": int(saving) if saving else None,
        "save_percent": int(percent) if percent else None,
    }


def _legacy_extra_saving(
    tree: Any, last_price: float | None, price: float | None
) -> dict[str, Any]:
    values = [
        value
        for value in _xpath_strings(
            tree,
            '//span[@class="a-size-large a-color-price savingPriceOverride aok-align-center reinventPriceSavingsPercentageMargin savingsPercentage"]/text()',
        )
        if value.strip().replace(" ", "")
    ]
    title = values[0] if values else None
    percent = _regex_first(r"-(\d+)%", title or "") or None
    return {
        "title": title,
        "save_price": last_price - price if title and last_price and price else None,
        "save_percent": int(percent) if percent else None,
    }


def _legacy_localized_fields(
    tree: Any, legacy_marketplace_id: str
) -> tuple[str, str, str, bool, list[str]]:
    bought_first = _first_raw(tree, "//span[contains(@id, 'bought')]/span[1]/text()")
    bought_second = _first_raw(tree, "//span[contains(@id, 'bought')]/span[2]/text()")
    ingress = _joined_xpath(
        tree,
        '//div[@id="nav-global-location-slot"]//div[@id="glow-ingress-block"]/span/text()',
    )
    badge = _first_raw(tree, '//div[@id="zeitgeistBadge_feature_div"]//i/text()').lower()
    if legacy_marketplace_id == "A1C3SOZRARQ6R3":
        return (
            _regex_first(r"(.*?)\s*kupionych", bought_first),
            _regex_first(r"w ciągu ostatniego (.+)", bought_second),
            _regex_first(r"Adres\s*dostawy:\s*(.+)", ingress),
            "Bestseller" in badge,
            _xpath_strings(tree, "//h3[contains(text(), 'tym') or contains(text(), 'przedmiocie')]/following-sibling::ul//span/text()|//h1[contains(text(), 'tym') or contains(text(), 'przedmiocie')]/following-sibling::ul//span/text()"),
        )
    if legacy_marketplace_id == "A2Q3Y263D00KWC":
        return (
            _regex_first(r"Mais de (.*?) compras", bought_first),
            _regex_first(r"no (.*?) passado", bought_second),
            _regex_first(r"Enviar\s*para\s*(.+)", ingress),
            "vendido" in badge,
            _xpath_strings(tree, "//h3[contains(text(), 'este')]/following-sibling::ul//span/text()|//h1[contains(text(), 'este')]/following-sibling::ul//span/text()"),
        )
    if legacy_marketplace_id == "A1AM78C64UM0Y8":
        return (
            _regex_first(r"(.*?) comprados", bought_first),
            _regex_first(r"el (.*?) pasado", bought_second),
            _regex_first(r"Enviar\s*(?:a|en)\s*(.+)", ingress),
            "vendido" in badge,
            _xpath_strings(tree, "//h3[contains(text(), 'este')]/following-sibling::ul//span/text()|//h1[contains(text(), 'este')]/following-sibling::ul//span/text()"),
        )
    if legacy_marketplace_id == "A33AVAJ2PDY3EV":
        return (
            _regex_first(r"(.*?) adetten fazla satın alındı", bought_second),
            _regex_first(r"Geçen\s*(.*?) ", bought_first),
            _regex_first(r"Teslimat adresi:\s*(.+)", ingress),
            "satanlarda" in badge,
            _xpath_strings(tree, "//h3[contains(text(), 'Bu')]/following-sibling::ul//span/text()|//h1[contains(text(), 'Bu')]/following-sibling::ul//span/text()"),
        )
    return (
        _regex_first(r"(.*?) bought", bought_first),
        _regex_first(r"in past (.+)", bought_second),
        _regex_first(r"(?:Deliver|Delivering) to (.+)", ingress),
        "best seller" in badge,
        _xpath_strings(tree, "//h3[contains(text(), 'this')]/following-sibling::ul//span/text()|//h1[contains(text(), 'this')]/following-sibling::ul//span/text()"),
    )


def parse_product_html(
    html: str,
    *,
    asin: str,
    product_url: str,
    marketplace_id: str | None = None,
    postal_code: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    tree = lxml_html.fromstring(html)
    json_ld = _parse_json_ld(tree)
    offers = json_ld.get("offers") if isinstance(json_ld.get("offers"), dict) else {}
    rating = json_ld.get("aggregateRating")
    rating = rating if isinstance(rating, dict) else {}
    market = _marketplace(product_url, marketplace_id)
    legacy_marketplace_id = (
        market.amazon_marketplace_id if market else (marketplace_id or "")
    )
    market_code = market.id if market else None
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

    # Legacy-facing fields intentionally use the original crawler's selectors,
    # defaults and locale rules.  The generalized values below remain useful for
    # the V2 canonical aliases, but must not silently change the old DB contract.
    legacy_price_text = _first_raw(
        tree,
        '//div[contains(@data-feature-name, "corePrice")]//span[contains(@class, "offscreen")]/text()',
    ).strip()
    legacy_price = _legacy_price(legacy_price_text, legacy_marketplace_id)
    legacy_last_price = _legacy_last_price(tree, legacy_marketplace_id)
    legacy_prime_price = _legacy_prime_price(tree, legacy_marketplace_id)
    legacy_product_details = _legacy_product_details(tree)
    legacy_product_info = _legacy_product_info(tree, legacy_marketplace_id)
    legacy_bought, legacy_bought_time, legacy_city, legacy_best_seller, legacy_bullets = (
        _legacy_localized_fields(tree, legacy_marketplace_id)
    )
    legacy_imgs = _legacy_imgs(tree)
    legacy_img = _first_raw(tree, '//img[@id="landingImage"]/@src')
    legacy_product_description = [
        value
        for value in _xpath_strings(
            tree,
            '//div[@id="Desktop-Detailed-Evaluation-Zone"]//img/@src|//div[@id="aplus"]//img/@data-src',
        )
        if "/images/S/" in value
    ]
    legacy_amazon_choice_text = _first_raw(
        tree,
        '//div[@id="acBadge_feature_div"]/div/span/span/span/text()',
    ).lower()
    legacy_amazon_choice = any(
        value in legacy_amazon_choice_text for value in ("choice", "amazon", "seçimi")
    )
    legacy_buy_box_owner = _first_raw(
        tree,
        '//div[@offer-display-feature-name="desktop-merchant-info"]/div[1]/a/text()',
    )
    legacy_seller_href = _first_raw(
        tree,
        '//div[@offer-display-feature-name="desktop-merchant-info"]/div[1]/a/@href',
        "=",
    )
    legacy_seller_id = parse_qs(
        urlparse(legacy_seller_href).query, keep_blank_values=True
    ).get("seller", [""])[0]
    legacy_delivery_info = (
        "".join(
            _xpath_strings(
                tree,
                '//div[@id="deliveryBlockContainer"]//div[@id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"]//text()[not(ancestor::script) and not(ancestor::style) and not(ancestor::noscript) and not(ancestor::template)]',
            )
        ).strip()
        + " "
        + "".join(
            _xpath_strings(
                tree,
                '//div[@id="deliveryBlockContainer"]//div[@id="mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE"]//text()[not(ancestor::script) and not(ancestor::style) and not(ancestor::noscript) and not(ancestor::template)]',
            )
        ).split("Join Prime")[0].strip()
    )
    legacy_fba_text = _first_raw(
        tree,
        '//div[@id="offerDisplayFeatures_desktop"]/div[@id="offer-display-features"]//div[@id="fulfillerInfoFeature_feature_div"]/div[@class="offer-display-feature-text"]/div//span//text()',
    )
    legacy_rating_text = _first_raw(
        tree,
        '//a[@id="acrCustomerReviewLink"]/span[@id="acrCustomerReviewText"]/text()',
    )
    legacy_score_text = _first_raw(
        tree,
        '//div[@id="averageCustomerReviews"]//span[contains(@class, "a-color-base")]/text()',
    ).strip()
    legacy_score_match = _regex_first(
        r"(\d+.\d+)", legacy_score_text.replace(",", ".")
    )
    legacy_seller_count_text = _first_raw(
        tree,
        '//div[@id="dynamic-aod-ingress-box"]//span[@data-action="show-all-offers-display"]/span[1]/text()',
    ).replace(",", "")
    legacy_seller_count_match = _regex_first(r"(\d+)", legacy_seller_count_text)
    legacy_sku_count_match = re.search(r'"num_total_variations" : (.*?),', html)
    try:
        legacy_sku_count = int(legacy_sku_count_match.group(1)) if legacy_sku_count_match else 0
    except (TypeError, ValueError):
        legacy_sku_count = 0

    price_text = _first(
        tree,
        [
            '//*[@id="corePrice_feature_div"]//*[contains(@class,"a-offscreen")]/text()',
            '//*[contains(@data-feature-name,"corePrice")]//*[contains(@class,"a-offscreen")]/text()',
            '//*[@id="priceblock_ourprice"]/text()',
            '//*[@id="priceblock_dealprice"]/text()',
        ],
    ) or _clean(str(offers.get("price") or ""))
    title = _first(tree, ['//*[@id="productTitle"]/text()']) or _clean(str(json_ld.get("name") or ""))
    availability = _first(
        tree,
        [
            '//*[@id="availability"]//text()[not(ancestor::script) and not(ancestor::style) and not(ancestor::noscript) and not(ancestor::template)]'
        ],
    ) or _clean(
        str(offers.get("availability") or "").rsplit("/", 1)[-1]
    )
    byline = _first(tree, ['//*[@id="bylineInfo"]/text()'])
    brand = byline
    if brand and ":" in brand:
        brand = _clean(brand.split(":", 1)[1])
    elif brand:
        brand = _clean(re.sub(r"^(Visit|Visite|Visita)\s+(the\s+|la\s+)?", "", brand, flags=re.I))
        brand = _clean(re.sub(r"\s+(Store|insider).*$", "", brand or "", flags=re.I))

    buy_box_owner = _first(
        tree,
        [
            '//*[@offer-display-feature-name="desktop-merchant-info"]//a/text()',
            '//*[@id="sellerProfileTriggerId"]/text()',
            '//*[@id="merchant-info"]//a/text()',
        ],
    )
    seller_href = _first(
        tree,
        [
            '//*[@offer-display-feature-name="desktop-merchant-info"]//a/@href',
            '//*[@id="sellerProfileTriggerId"]/@href',
            '//*[@id="merchant-info"]//a/@href',
        ],
    )
    seller_id = parse_qs(urlparse(seller_href or "").query).get("seller", [None])[0]
    image_values = _images(tree, html)
    img = image_values[0] if image_values else _clean(str(json_ld.get("image") or ""))
    pasin = _first(tree, ['//*[@data-parent-asin]/@data-parent-asin'])
    if not pasin:
        match = re.search(r'["\']parentAsin["\']\s*:\s*["\']([A-Z0-9]{10})', html, re.I)
        pasin = match.group(1).upper() if match else None
    variants = _variants(html)
    # The legacy parser persists the crawler-built request URL.  Do not trust a
    # page-controlled canonical tag here: it can carry referral/session query
    # material into result storage even when the fetch redirect itself was on
    # an allowlisted Amazon host.
    canonical = product_url

    breadcrumb_nodes = tree.xpath('//ul[contains(@class,"a-horizontal") and contains(@class,"a-size-small")]//a')
    breadcrumb_names = [_clean(node.text_content()) for node in breadcrumb_nodes]
    breadcrumb_names = [name for name in breadcrumb_names if name]
    breadcrumb_ids: list[str] = []
    for node in breadcrumb_nodes:
        href = node.get("href") or ""
        query = parse_qs(urlparse(href).query)
        category_id = query.get("node", [None])[0]
        if not category_id:
            parts = [part for part in urlparse(href).path.split("/") if part]
            category_id = parts[-1] if parts else None
        if category_id:
            breadcrumb_ids.append(category_id)

    product_details = _table_mapping(
        tree,
        (
            '//*[@id="productOverview_feature_div"]//tr',
            '//*[@id="productFactsDesktopExpander"]//*[contains(@class,"a-fixed-left-grid")]',
        ),
    )
    product_info = _table_mapping(
        tree,
        ('//table[contains(@id,"productDetails")]//tr',),
    )
    product_info.update(_detail_bullets(tree))
    all_details = {**product_details, **product_info}

    rating_text = _first(
        tree,
        ['//*[@id="acrPopover"]/@title', '//*[@data-hook="rating-out-of-text"]/text()'],
    ) or _clean(str(rating.get("ratingValue") or ""))
    rating_count_text = _first(tree, ['//*[@id="acrCustomerReviewText"]/text()']) or _clean(
        str(rating.get("reviewCount") or "")
    )
    availability_lower = (availability or "").lower()
    unavailable_markers = ("unavailable", "out of stock", "currently unavailable", "no disponible")
    inventory_match = re.search(r"(?:only|sadece)?\s*(\d+)\s+(?:left|in stock|adet|sztuki)", availability_lower)
    inventory_num = int(inventory_match.group(1)) if inventory_match else (0 if any(marker in availability_lower for marker in unavailable_markers) else (21 if availability else 0))

    last_price_text = _first(
        tree,
        [
            '//*[contains(@class,"basisPrice")]//*[contains(@class,"a-offscreen")]/text()',
            '//*[@data-a-strike="true"]//*[contains(@class,"a-offscreen")]/text()',
        ],
    )
    prime_price_text = _first(tree, ['//*[@id="primeExclusivePricingMessage"]//*[contains(@class,"a-price")]/text()'])
    coupon_title = _first(
        tree,
        [
            '//*[contains(@class,"promoPriceBlockMessage")]//label/text()',
            '//*[@id="couponTextpctch"]//text()',
            '//*[@id="couponText"]//text()',
        ],
    )
    coupon_percent_match = re.search(r"(\d+)%", coupon_title or "")
    coupon_amount = _number(coupon_title) if coupon_title and not coupon_percent_match else None
    price = _number(price_text)
    coupon_percent = int(coupon_percent_match.group(1)) if coupon_percent_match else None
    coupons = {
        "title": coupon_title,
        "save_price": (
            price * coupon_percent / 100
            if price is not None and coupon_percent is not None
            else coupon_amount
        ),
        "save_percent": coupon_percent,
    }
    saving_title = _first(tree, ['//*[contains(@class,"savingsPercentage")]/text()'])
    saving_match = re.search(r"(\d+)%", saving_title or "")
    last_price = _number(last_price_text)
    extra_saving = {
        "title": saving_title,
        "save_price": last_price - price if last_price is not None and price is not None else None,
        "save_percent": int(saving_match.group(1)) if saving_match else None,
    }
    choice_text = _first(tree, ['//*[@id="acBadge_feature_div"]//text()'])
    amazon_choice = bool(choice_text and any(word in choice_text.lower() for word in ("choice", "amazon", "seçimi")))
    best_seller_text = _first(tree, ['//*[@id="zeitgeistBadge_feature_div"]//text()'])
    best_seller = bool(best_seller_text and any(word in best_seller_text.lower() for word in ("best seller", "bestseller", "vendido", "satanlarda")))
    bought_text = _first(tree, ['//*[contains(@id,"bought")]//text()'])
    bought_match = re.search(r"([\d,.]+[Kk]?)", bought_text or "")
    bought = bought_match.group(1) if bought_match else None
    bought_time = None
    if bought_text:
        time_match = re.search(r"(?:past|last|passado|pasado|ostatniego|geçen)\s+(.+)$", bought_text, re.I)
        bought_time = _clean(time_match.group(1)) if time_match else None

    ingress = _first(tree, ['//*[@id="glow-ingress-block"]//text()'])
    city = _clean(
        re.sub(
            r"^(Deliver(?:ing)?\s+to|Enviar\s+(?:a|en|para)|Adres dostawy:|Teslimat adresi:)\s*",
            "",
            ingress or "",
            flags=re.I,
        )
    )
    delivery_info = _clean(
        " ".join(
            _all(
                tree,
                '//*[@id="deliveryBlockContainer"]//*[@id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE" or @id="mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE"]//text()[not(ancestor::script) and not(ancestor::style) and not(ancestor::noscript) and not(ancestor::template)]',
            )
        )
    )
    ships_from = _first(
        tree,
        [
            '//*[@offer-display-feature-name="desktop-fulfiller-info"]//*[contains(@class,"offer-display-feature-text-message")]/text()',
            '//*[@id="fulfillerInfoFeature_feature_div"]//text()',
        ],
    )
    deal_claimed_text = _first(tree, ['//*[@id="dealsx_percent_message"]/text()'])
    redirect_match = re.search(r'["\']currentAsin["\']\s*:\s*["\']([A-Z0-9]{10})', html, re.I)
    redirect_to = redirect_match.group(1).upper() if redirect_match else None
    if redirect_to == asin.upper():
        redirect_to = ""
    seller_count_text = _first(tree, ['//*[@id="dynamic-aod-ingress-box"]//*[@data-action="show-all-offers-display"]//text()'])
    description_images = list(
        dict.fromkeys(
            _all(tree, '//*[@id="Desktop-Detailed-Evaluation-Zone"]//img/@src|//*[@id="aplus"]//img/@data-src|//*[@id="aplus"]//img/@src')
        )
    )
    review_images = _all(tree, '//*[@id="reviewsMedley"]//img/@src')
    bullet_points = _all(tree, '//*[@id="feature-bullets"]//li//*[contains(@class,"a-list-item")]')
    if not bullet_points:
        bullet_points = _all(tree, '//h3/following-sibling::ul[1]//span')
    deal_badge = _clean(" ".join(_all(tree, '//*[@id="dealBadge_feature_div"]//text()')))
    deal_end_time = _first(tree, ['//*[@id="dealBadge_feature_div"]//*[@data-target-time]/@data-target-time'])
    review_summary = _first(tree, ['//*[@id="product-summary"]//p//text()'])
    review_labels = _all(tree, '//*[@data-csa-c-action="infoPopOver"]/text()')
    fba_text = _first(tree, ['//*[@id="fulfillerInfoFeature_feature_div"]//text()']) or ""

    dimension_items: list[dict[str, Any]] = []
    variant_source = variants or [{"asin": asin, "attribute": []}]
    for variant in variant_source:
        variant_asin = str(variant.get("asin") or asin).upper()
        dimension_url = canonical
        if market:
            language = marketplace_language(market.id)
            dimension_url = (
                f"https://{market.domain}/dp/{variant_asin}"
                f"?th=1&psc=1&language={language}"
            )
        dimension_items.append(
            {
                "task_id": task_id,
                "market_place_id": legacy_marketplace_id,
                "parent_asin": pasin,
                "asin": variant_asin,
                "dimensions": variant.get("attribute") or "",
                "created_at": now,
                "image_url": dimension_url,
            }
        )

    data: dict[str, Any] = {
        "schema_version": "amazon.product.v2",
        "parser_version": PARSER_VERSION,
        "a_plus": 1 if legacy_product_description else 0,
        "amazon_choice": legacy_amazon_choice,
        "amazon_choice_keyword": "Amazon's Choice" if legacy_amazon_choice else None,
        "asin": asin.upper(),
        "best_seller_flag": legacy_best_seller,
        "bought": legacy_bought,
        "bought_time_unit": legacy_bought_time,
        "brand": _legacy_brand(tree, legacy_marketplace_id),
        "breadcrumbs_feature": ">".join(breadcrumb_names) if breadcrumb_names else None,
        "breadcrumbs_feature_ids": ">".join(breadcrumb_ids) if breadcrumb_ids else None,
        "bsr": _legacy_bsr(tree, legacy_marketplace_id),
        "bullet_points": legacy_bullets,
        "buy_box_owner": legacy_buy_box_owner,
        "buy_box_seller_id": legacy_seller_id,
        "buy_box_type": ("AMAZON" if "Amazon" in legacy_buy_box_owner else "SELLER") if legacy_buy_box_owner else None,
        "city": legacy_city,
        "coupons": _legacy_coupons(tree, legacy_price),
        "created_at": now,
        "deal_badge": " ".join(
            _xpath_strings(
                tree,
                '//div[@id="dealBadge_feature_div"]//span[@class="aok-offscreen"]/text()|//div[@id="dealBadge_feature_div"]//span[@id="dealBadgeSupportingText"]/span/text()',
            )
        ).strip(),
        "deal_end_time": _first_raw(
            tree,
            '//div[@id="apex_desktop"]//span[contains(@class, "dealBadge")]//span[@data-target-time]/text()',
        ).strip(),
        "delivery_info": legacy_delivery_info,
        "extra_saving": _legacy_extra_saving(tree, legacy_last_price, legacy_price),
        "fba": "amazon" in legacy_fba_text.lower(),
        "fbt": _fbt(tree),
        "first_page_review_imgs": _xpath_strings(
            tree, '//div[@id="reviewsMedley"]//ol/li//img/@src'
        ),
        "generate_date": _date_value(
            {**(legacy_product_details or {}), **(legacy_product_info or {})}
        ),
        "img": legacy_img,
        "imgs": legacy_imgs,
        "inventory_num": _legacy_inventory(tree),
        "last_price": legacy_last_price,
        "lighting_deal_claimed": _integer(deal_claimed_text) or None,
        "link": canonical,
        "marketplace_id": legacy_marketplace_id,
        "offsale": bool(
            tree.xpath('//div[@id="availability_feature_div"]/div[@id="availability"]')
        ),
        "pasin": pasin,
        "price": legacy_price,
        "prime_price": legacy_prime_price,
        "product_description": legacy_product_description,
        "product_details": legacy_product_details,
        "product_info": legacy_product_info,
        "rating_num": _integer(legacy_rating_text),
        "redirect_to": redirect_to,
        "review_insight_label": _xpath_strings(
            tree, '//a[@data-csa-c-action="infoPopOver"]/text()'
        ),
        "review_summary": _first_raw(
            tree, '//div[@id="product-summary"]/p[@class="a-spacing-small"]/span/text()'
        ),
        "score": float(legacy_score_match) if legacy_score_match else 0,
        "seller_num": int(legacy_seller_count_match) if legacy_seller_count_match else 0,
        "ships_from": _first_raw(
            tree,
            '//div[@offer-display-feature-name="desktop-fulfiller-info"]/div/span[@class="a-size-small offer-display-feature-text-message"]/text()',
        ),
        "sku_num": legacy_sku_count,
        "skus": variants,
        "star_percent": _legacy_star_percent(tree),
        "task_id": task_id,
        "title": title,
        "used": bool(tree.xpath('//*[@id="usedOnlyBuybox"]')),
        "zipcode": postal_code,
        "dimension_items": dimension_items,
        "marketplace_code": market_code,
        "currency": _currency(price_text, offers, market),
        "availability": availability,
        "seller": buy_box_owner,
        "rating": rating_text,
        "rating_count": rating_count_text,
        "image_url": img,
        "parent_asin": pasin,
        "best_sellers_rank": _clean(" ".join(_all(tree, '//*[@id="SalesRank"]//text()'))),
        "detail_rows": [f"{key}: {value}" for key, value in all_details.items()],
        "canonical_url": canonical,
        # Keep the legacy detector's exact quote/case semantics.  A broader
        # single-quoted ``'isVideo': true`` match produced a real false positive
        # against an archived response even though the legacy parser returned 0.
        "is_video": int(
            bool(
                any("play" in image for image in legacy_imgs)
                or tree.xpath('//li[contains(@class,"videoThumbnail")]|//span[@id="videoCount" and normalize-space()="VIDEO"]')
                or re.search(r'"isVideo"\s*:\s*true', unescape(html))
                or re.search(
                    r'"videoURL"\s*:\s*"https?://',
                    unescape(html),
                    re.IGNORECASE,
                )
                or re.search(
                    r'"url"\s*:\s*"https?://[^"]+\.m3u8"',
                    unescape(html),
                    re.IGNORECASE,
                )
            )
        ),
    }
    populated = sum(1 for field in CORE_FIELDS if data.get(field) is not None)
    data["quality"] = {
        "core_field_coverage": populated / len(CORE_FIELDS),
        "missing_core_fields": [field for field in CORE_FIELDS if data.get(field) is None],
        "legacy_field_coverage": sum(
            1 for field in LEGACY_PRODUCT_FIELDS if field in data
        )
        / len(LEGACY_PRODUCT_FIELDS),
    }
    return data


def looks_blocked(html: str) -> bool:
    sample = html[:100_000].lower()
    markers = (
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "automated access to amazon data",
        'action="/errors/validatecaptcha"',
    )
    return any(marker in sample for marker in markers)
