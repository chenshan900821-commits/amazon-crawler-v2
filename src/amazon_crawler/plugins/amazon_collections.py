from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus, urlencode, urljoin, urlparse

from amazon_crawler.domain.errors import ValidationError
from amazon_crawler.domain.models import (
    ClaimedItem,
    CrawlFailure,
    CrawlResult,
    NormalizedInput,
    PluginOutcome,
)
from amazon_crawler.domain.resources import ResourceOutcome
from amazon_crawler.infra.evidence import EvidenceStore, public_response_metadata
from amazon_crawler.infra.http import HttpFetcher
from amazon_crawler.plugins.response_policy import (
    classify_amazon_page,
    classify_collection_completeness,
    classify_product_completeness,
)
from amazon_crawler.plugins.collection_parser import (
    parse_category_html,
    parse_rank_html,
    parse_reviews_html,
    parse_search_html,
)
from amazon_crawler.plugins.marketplaces import (
    AMAZON_ID_TO_MARKETPLACE,
    MARKETPLACES,
    Marketplace,
    marketplace_language,
    url_contains_sensitive_material,
)


ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.I)
KIND_ALIASES = {
    "search_jp": "search",
    "search_hour_jp": "search_hour",
    "asin_list_jp": "category_asin_list",
    "rank_list_jp": "rank_list",
}
RANK_STREAM_CONCURRENCY = 3


def _marketplace(value: str | None) -> Marketplace:
    selected = (value or "").strip().upper()
    if selected in MARKETPLACES:
        return MARKETPLACES[selected]
    if selected in AMAZON_ID_TO_MARKETPLACE:
        return AMAZON_ID_TO_MARKETPLACE[selected]
    raise ValidationError("a supported marketplace code or Amazon marketplace ID is required")


def _integer(value: Any, name: str, *, minimum: int = 1, maximum: int = 1000) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValidationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


class AmazonCollectionPlugin:
    def __init__(
        self,
        *,
        kind: str,
        fetcher: HttpFetcher,
        evidence_store: EvidenceStore,
        require_cookie: bool = True,
    ) -> None:
        self.kind = kind
        self.base_kind = KIND_ALIASES.get(kind, kind)
        if self.base_kind not in {
            "search",
            "search_hour",
            "reviews",
            "category_asin_list",
            "rank_list",
        }:
            raise ValueError("unsupported collection task kind")
        self._fetcher = fetcher
        self._evidence_store = evidence_store
        self._require_cookie = require_cookie

    def normalize(
        self,
        source: Any,
        marketplace_id: str | None,
        postal_code: str | None,
    ) -> NormalizedInput:
        if isinstance(source, str):
            if self.base_kind in {"search", "search_hour"}:
                raw: dict[str, Any] = {"keyword": source}
            elif self.base_kind == "reviews":
                raw = {"asin": source}
            else:
                raise ValidationError(f"{self.base_kind} input must be an object")
        elif isinstance(source, dict):
            raw = dict(source)
        else:
            raise ValidationError("task input must be a string or object")

        market = _marketplace(str(raw.get("market_id") or marketplace_id or ""))
        legacy_id = market.amazon_marketplace_id
        post = str(raw.get("post_code") or postal_code or "").strip() or None
        common = {
            "id": str(raw.get("id") or ""),
            "market_id": legacy_id,
            "marketplace_code": market.id,
            "post_code": post,
            "add_date": raw.get("add_date"),
        }
        if self.base_kind in {"search", "search_hour"}:
            keyword = str(raw.get("keyword") or "").strip()
            if not keyword or len(keyword) > 500:
                raise ValidationError("keyword is required and must be at most 500 characters")
            page = _integer(raw.get("turn_page", raw.get("page", 1)), "turn_page", maximum=400)
            common.update(
                {
                    "keyword": keyword,
                    "turn_page": page,
                    "frequent": _integer(raw.get("frequent", 0), "frequent", minimum=0, maximum=1),
                }
            )
            if self.base_kind == "search_hour":
                common["data_hour"] = raw.get("data_hour") or datetime.now(UTC).strftime("%Y-%m-%d %H:00:00")
            key = f"{self.base_kind}:{legacy_id}:{post or '-'}:{keyword}:{page}"
            if self.base_kind == "search_hour":
                key += f":{common['data_hour']}"
        elif self.base_kind == "reviews":
            asin = str(raw.get("asin") or "").strip().upper()
            if not ASIN_RE.fullmatch(asin):
                raise ValidationError("reviews input requires a valid ASIN")
            common["asin"] = asin
            key = f"reviews:{legacy_id}:{post or '-'}:{asin}"
        elif self.base_kind == "category_asin_list":
            category_id = str(raw.get("category_id") or "").strip()
            if not category_id or len(category_id) > 100:
                raise ValidationError("category_id is required")
            page = _integer(raw.get("page", 1), "page", maximum=400)
            common.update({"category_id": category_id, "page": page})
            key = f"category:{legacy_id}:{post or '-'}:{category_id}:{page}"
        else:
            category_id = str(raw.get("category_id") or "").strip()
            url_type = str(raw.get("url_type") or "bestsellers").strip()
            page = _integer(raw.get("page", 1), "page", maximum=20)
            target_url = str(raw.get("url") or "").strip()
            parsed = urlparse(target_url)
            if parsed.scheme != "https" or (parsed.hostname or "").lower().removeprefix("www.") != market.domain.removeprefix("www."):
                raise ValidationError("rank URL must be an HTTPS URL on the selected Amazon marketplace")
            if url_contains_sensitive_material(target_url):
                raise ValidationError("rank URL must not contain credential-like query data or a fragment")
            if not category_id:
                raise ValidationError("category_id is required")
            if url_type not in {"bestsellers", "new-releases"}:
                raise ValidationError("url_type must be bestsellers or new-releases")
            common.update(
                {
                    "url": target_url,
                    "url_type": url_type,
                    "category_id": category_id,
                    "page": page,
                }
            )
            key = f"rank:{legacy_id}:{url_type}:{category_id}:{page}"
        return NormalizedInput.from_payload(
            common,
            input_key=key,
            marketplace_id=market.id,
            postal_code=post,
            source=source,
        )

    def _request(self, task: dict[str, Any]) -> tuple[str, str, dict[str, object] | None]:
        market = MARKETPLACES[task["marketplace_code"]]
        base = f"https://{market.domain}"
        if self.base_kind in {"search", "search_hour"}:
            query = urlencode(
                {
                    "k": task["keyword"],
                    "page": task["turn_page"],
                    "qid": int(time.time()),
                    "language": marketplace_language(task["marketplace_code"]),
                }
            )
            return f"{base}/s/query?{query}", "POST", {"customer-action": "pagination"}
        if self.base_kind == "reviews":
            query = urlencode(
                {
                    "th": 1,
                    "psc": 1,
                    "language": marketplace_language(task["marketplace_code"]),
                }
            )
            return f"{base}/dp/{task['asin']}?{query}", "GET", None
        if self.base_kind == "category_asin_list":
            category = quote_plus(task["category_id"])
            return (
                f"{base}/-/en/s?rh=n%3A{category}&fs=true&page={task['page']}"
                f"&ref=sr_pg_{task['page']}"
            ), "GET", None
        separator = "&" if "?" in task["url"] else "?"
        query = urlencode(
            {
                "ie": "UTF8",
                "pg": task["page"],
                "language": marketplace_language(task["marketplace_code"]),
            }
        )
        return f"{task['url']}{separator}{query}", "GET", None

    async def execute(self, item: ClaimedItem) -> PluginOutcome:
        task = item.input
        url, method, json_body = self._request(task)
        fetched = await self._fetcher.fetch(
            url,
            marketplace_id=task["marketplace_code"],
            postal_code=task.get("post_code"),
            purpose=self.base_kind,
            require_cookie=self._require_cookie,
            method=method,
            json_body=json_body,
            extra_headers={"Referer": url}
            if self.base_kind in {"search", "search_hour"}
            else None,
        )
        if isinstance(fetched, CrawlFailure):
            return PluginOutcome(failure=fetched)
        not_found_codes = {
            "reviews": "no_reviews",
            "category_asin_list": "no_category_results",
            "rank_list": "no_rank_results",
        }
        issue = classify_amazon_page(
            fetched.html,
            not_found_code=not_found_codes.get(self.base_kind, "no_results"),
            purpose=self.base_kind,
        )
        if not issue and self.base_kind == "reviews":
            issue = classify_product_completeness(fetched.html)
        if not issue and self.base_kind != "reviews":
            issue = classify_collection_completeness(
                self.base_kind,
                fetched.html,
                page=int(task.get("turn_page") or task.get("page") or 1),
            )
        if issue:
            await self._fetcher.report(
                fetched,
                ResourceOutcome.BLOCKED
                if issue.code == "blocked_page"
                else ResourceOutcome.PARSE_ERROR,
            )
            issue = self._evidence_store.attach_failure(
                issue,
                job_id=item.job_id,
                item_id=item.id,
                html=fetched.html,
                http_status=fetched.status_code,
            )
            return PluginOutcome(failure=issue)
        market = MARKETPLACES[task["marketplace_code"]]
        base_url = f"https://{market.domain}"
        try:
            if self.base_kind in {"search", "search_hour"}:
                rows = parse_search_html(
                    fetched.html,
                    task,
                    base_url=base_url,
                    include_deal_badge=self.base_kind == "search",
                )
                schema_version = "amazon.search-page.v1"
            elif self.base_kind == "reviews":
                rows = parse_reviews_html(fetched.html, task)
                schema_version = "amazon.reviews.v1"
            elif self.base_kind == "category_asin_list":
                rows = parse_category_html(fetched.html, task, base_url=base_url)
                schema_version = "amazon.category-page.v1"
            else:
                rows, continuation = parse_rank_html(fetched.html, task, base_url=base_url)
                if continuation:
                    remaining = list(continuation.remaining)
                    category_name = rows[0].get("category_name") if rows else None
                    semaphore = asyncio.Semaphore(RANK_STREAM_CONCURRENCY)

                    async def load_chunk(
                        position: int,
                    ) -> tuple[int, list[dict[str, Any]], CrawlFailure | None]:
                        chunk = remaining[position : position + 8]
                        stream_url = urljoin(base_url, continuation.acp_path + "nextPage")
                        start = continuation.start_offset + position
                        body: dict[str, object] = {
                            "faceoutkataname": "GeneralFaceout",
                            "ids": [json.dumps(value, separators=(",", ":")) for value in chunk],
                            "indexes": list(range(start, start + len(chunk))),
                            "linkparameters": "",
                            "offset": str(start),
                            "reftagprefix": (
                                f"zg_{'bs' if task['url_type'] == 'bestsellers' else 'bsnr'}"
                                f"_g_{task['category_id']}"
                            ),
                        }
                        async with semaphore:
                            streamed = await self._fetcher.fetch(
                                stream_url,
                                marketplace_id=task["marketplace_code"],
                                postal_code=task.get("post_code"),
                                purpose="rank_list",
                                require_cookie=self._require_cookie,
                                method="POST",
                                json_body=body,
                                extra_headers={
                                    "x-amz-acp-params": continuation.acp_params
                                },
                            )
                        if isinstance(streamed, CrawlFailure):
                            return position, [], streamed
                        try:
                            extra, _ = parse_rank_html(
                                streamed.html,
                                task,
                                base_url=base_url,
                            )
                        except Exception as exc:
                            await self._fetcher.report(
                                streamed,
                                ResourceOutcome.PARSE_ERROR,
                            )
                            failure = CrawlFailure(
                                "parse_error",
                                "the rank continuation response could not be parsed",
                                True,
                                {"error_type": type(exc).__name__},
                            )
                            return (
                                position,
                                [],
                                self._evidence_store.attach_failure(
                                    failure,
                                    job_id=item.job_id,
                                    item_id=item.id,
                                    html=streamed.html,
                                    http_status=streamed.status_code,
                                ),
                            )
                        if chunk and not extra:
                            await self._fetcher.report(
                                streamed,
                                ResourceOutcome.PARSE_ERROR,
                            )
                            failure = CrawlFailure(
                                "upstream_incomplete",
                                "the rank continuation response contained no requested rows",
                                True,
                            )
                            return (
                                position,
                                [],
                                self._evidence_store.attach_failure(
                                    failure,
                                    job_id=item.job_id,
                                    item_id=item.id,
                                    html=streamed.html,
                                    http_status=streamed.status_code,
                                ),
                            )
                        rank_map = {
                            str(value.get("id")): str(value.get("metadataMap", {}).get("render.zg.rank"))
                            for value in chunk
                        }
                        for row in extra:
                            row["ranking"] = rank_map.get(row["asin"], row.get("ranking"))
                            row["category_name"] = category_name
                        await self._fetcher.report(streamed, ResourceOutcome.SUCCESS)
                        return position, extra, None

                    chunks = await asyncio.gather(
                        *(
                            load_chunk(position)
                            for position in range(0, len(remaining), 8)
                        )
                    )
                    failures = [
                        (position, failure)
                        for position, _, failure in chunks
                        if failure is not None
                    ]
                    if failures:
                        # The initial rank page was healthy. Continuation
                        # failures already reported their own resource outcome.
                        await self._fetcher.report(
                            fetched,
                            ResourceOutcome.SUCCESS,
                        )
                        first_failure = min(failures, key=lambda value: value[0])[1]
                        return PluginOutcome(failure=first_failure)
                    for _, extra, _ in sorted(chunks, key=lambda value: value[0]):
                        rows.extend(extra)
                unique = {f"{row.get('asin')}:{row.get('ranking')}": row for row in rows}
                rows = list(unique.values())
                schema_version = "amazon.rank-page.v1"
        except Exception as exc:
            await self._fetcher.report(fetched, ResourceOutcome.PARSE_ERROR)
            failure = CrawlFailure(
                "parse_error",
                "the collection response could not be parsed",
                True,
                {"error_type": type(exc).__name__},
            )
            return PluginOutcome(
                failure=self._evidence_store.attach_failure(
                    failure,
                    job_id=item.job_id,
                    item_id=item.id,
                    html=fetched.html,
                    http_status=fetched.status_code,
                )
            )
        if not rows:
            await self._fetcher.report(fetched, ResourceOutcome.PARSE_ERROR)
            empty_codes = {
                "reviews": "no_reviews",
                "category_asin_list": "no_category_results",
                "rank_list": "no_rank_results",
            }
            code = empty_codes.get(self.base_kind, "no_results")
            if (
                self.base_kind in {"search", "search_hour"}
                and int(task.get("turn_page") or 1) > 1
            ):
                code = "one_page_only"
            failure = CrawlFailure(
                    code,
                    "the response contained no result rows",
                    False,
                )
            return PluginOutcome(
                failure=self._evidence_store.attach_failure(
                    failure,
                    job_id=item.job_id,
                    item_id=item.id,
                    html=fetched.html,
                    http_status=fetched.status_code,
                )
            )
        await self._fetcher.report(fetched, ResourceOutcome.SUCCESS)
        evidence = self._evidence_store.save_html(
            job_id=item.job_id,
            item_id=item.id,
            html=fetched.html,
        )
        evidence.update(
            public_response_metadata(
                url=fetched.url,
                status_code=fetched.status_code,
                headers=fetched.headers,
            )
        )
        return PluginOutcome(
            result=CrawlResult(
                data={
                    "items": rows,
                    "row_count": len(rows),
                    "task": {
                        key: value
                        for key, value in task.items()
                        if key not in {"id"}
                    },
                },
                evidence=evidence,
                schema_version=schema_version,
            )
        )
