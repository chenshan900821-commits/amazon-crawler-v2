from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlencode, urlparse

from amazon_crawler.domain.errors import ValidationError
from amazon_crawler.domain.models import (
    ClaimedItem,
    CrawlFailure,
    CrawlResult,
    FollowupJob,
    NormalizedInput,
    PluginOutcome,
)
from amazon_crawler.domain.resources import ResourceOutcome
from amazon_crawler.infra.evidence import EvidenceStore, public_response_metadata
from amazon_crawler.infra.http import HttpFetcher
from amazon_crawler.plugins.response_policy import (
    classify_amazon_page,
    classify_collection_completeness,
)
from amazon_crawler.plugins.marketplaces import (
    AMAZON_ID_TO_MARKETPLACE,
    MARKETPLACES,
    Marketplace,
    merchant_language,
    url_contains_sensitive_material,
)
from amazon_crawler.plugins.merchant_parser import (
    parse_merchant_home,
    parse_merchant_html,
    parse_merchant_products,
)


SELLER_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")


def _marketplace(value: str | None) -> Marketplace:
    selected = (value or "").strip().upper()
    if selected in MARKETPLACES:
        return MARKETPLACES[selected]
    if selected in AMAZON_ID_TO_MARKETPLACE:
        return AMAZON_ID_TO_MARKETPLACE[selected]
    raise ValidationError("a supported marketplace code or Amazon marketplace ID is required")


class AmazonMerchantPlugin:
    def __init__(
        self,
        *,
        kind: str,
        fetcher: HttpFetcher,
        evidence_store: EvidenceStore,
        require_cookie: bool = True,
    ) -> None:
        if kind not in {"merchant", "merchant_home", "merchant_products"}:
            raise ValueError("unsupported merchant task kind")
        self.kind = kind
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
            raw: dict[str, Any] = {"seller_id": source}
        elif isinstance(source, dict):
            raw = dict(source)
        else:
            raise ValidationError("merchant input must be a seller ID or object")
        market = _marketplace(str(raw.get("market_id") or marketplace_id or ""))
        seller_id = str(raw.get("seller_id") or "").strip()
        if not SELLER_RE.fullmatch(seller_id):
            raise ValidationError("seller_id contains unsupported characters")
        post = str(raw.get("post_code") or postal_code or "").strip() or None
        task: dict[str, Any] = {
            "id": str(raw.get("id") or ""),
            "seller_id": seller_id,
            "market_id": market.amazon_marketplace_id,
            "marketplace_code": market.id,
            "post_code": post,
            "add_date": raw.get("add_date"),
        }
        if raw.get("source_task_id") is not None:
            task["source_task_id"] = str(raw["source_task_id"])
        if self.kind == "merchant_home" and raw.get("object_url"):
            target = str(raw["object_url"]).strip()
            parsed = urlparse(target)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            if parsed.scheme != "https" or host != market.domain.removeprefix("www."):
                raise ValidationError("object_url must be HTTPS on the selected Amazon marketplace")
            if url_contains_sensitive_material(target):
                raise ValidationError(
                    "object_url must not contain credential-like query data or a fragment"
                )
            task["object_url"] = target
        if self.kind == "merchant_products":
            try:
                page = int(raw.get("page", 1))
            except (TypeError, ValueError) as exc:
                raise ValidationError("page must be an integer") from exc
            if not 1 <= page <= 400:
                raise ValidationError("page must be between 1 and 400")
            task["page"] = page
        key = f"{self.kind}:{market.amazon_marketplace_id}:{seller_id}"
        if self.kind == "merchant_products":
            key += f":{task['page']}"
        return NormalizedInput.from_payload(
            task,
            input_key=key,
            marketplace_id=market.id,
            postal_code=post,
            source=source,
        )

    def _request(self, task: dict[str, Any]) -> tuple[str, str, dict[str, object] | None]:
        market = MARKETPLACES[task["marketplace_code"]]
        base = f"https://{market.domain}"
        if self.kind == "merchant":
            return f"{base}/sp?seller={task['seller_id']}", "GET", None
        if self.kind == "merchant_home":
            if task.get("object_url"):
                return task["object_url"], "GET", None
            query = urlencode(
                {
                    "ie": "UTF8",
                    "i": "merchant-items",
                    "me": task["seller_id"],
                    "s": "date-desc-rank",
                    "language": merchant_language(task["marketplace_code"]),
                    "marketplaceID": task["market_id"],
                }
            )
            return f"{base}/s?{query}", "GET", None
        query = urlencode(
            {
                "i": "merchant-items",
                "me": task["seller_id"],
                "s": "date-desc-rank",
                "page": task["page"],
                "language": merchant_language(task["marketplace_code"]),
                "marketplaceID": task["market_id"],
                "qid": int(time.time()),
            }
        )
        return f"{base}/s/query?{query}", "POST", {"customer-action": "pagination"}

    async def execute(self, item: ClaimedItem) -> PluginOutcome:
        task = item.input
        url, method, body = self._request(task)
        fetched = await self._fetcher.fetch(
            url,
            marketplace_id=task["marketplace_code"],
            postal_code=task.get("post_code"),
            purpose=self.kind,
            require_cookie=self._require_cookie,
            method=method,
            json_body=body,
            extra_headers={"Referer": url}
            if self.kind == "merchant_products"
            else None,
        )
        if isinstance(fetched, CrawlFailure):
            return PluginOutcome(failure=fetched)
        not_found_code = (
            "merchant_page_has_no_products"
            if self.kind == "merchant_products"
            else "merchant_has_no_products"
        )
        issue = classify_amazon_page(
            fetched.html,
            not_found_code=not_found_code,
            purpose=self.kind,
        )
        if not issue and self.kind in {"merchant_home", "merchant_products"}:
            issue = classify_collection_completeness(self.kind, fetched.html)
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
        followup_jobs: tuple[FollowupJob, ...] = ()
        try:
            if self.kind == "merchant":
                parsed: dict[str, Any] = parse_merchant_html(
                    fetched.html,
                    seller_id=task["seller_id"],
                    marketplace_id=task["market_id"],
                    url=url,
                )
                data = {"item": parsed}
                schema = "amazon.merchant.v1"
            elif self.kind == "merchant_home":
                rows = parse_merchant_home(fetched.html, task)
                if not rows:
                    await self._fetcher.report(fetched, ResourceOutcome.PARSE_ERROR)
                    failure = CrawlFailure(
                        "merchant_has_no_products", "merchant has no products", False
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
                data = {"child_tasks": rows, "row_count": len(rows)}
                schema = "amazon.merchant-child-tasks.v1"
                normalized_children = tuple(
                    NormalizedInput.from_payload(
                        {**row, "marketplace_code": task["marketplace_code"]},
                        input_key=(
                            f"merchant_products:{task['market_id']}:"
                            f"{task['seller_id']}:{row['page']}"
                        ),
                        marketplace_id=task["marketplace_code"],
                        postal_code=task.get("post_code"),
                        source=row,
                    )
                    for row in rows
                )
                followup_jobs = (
                    FollowupJob(
                        kind="merchant_products",
                        inputs=normalized_children,
                        execution_mode=item.execution_mode.value,
                        priority=50 if item.execution_mode.value == "realtime" else 0,
                        max_attempts=item.max_attempts,
                        reason="merchant_page_discovery",
                    ),
                )
            else:
                rows = parse_merchant_products(
                    fetched.html,
                    task,
                    base_url=f"https://{market.domain}",
                )
                if not rows:
                    await self._fetcher.report(fetched, ResourceOutcome.PARSE_ERROR)
                    failure = CrawlFailure(
                        "merchant_page_has_no_products",
                        "merchant page has no products",
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
                product_tasks = []
                if task.get("source_task_id") is not None:
                    deduplicated_rows = []
                    seen_products: set[tuple[str, str, str]] = set()
                    for row in rows:
                        asin = str(row.get("data_asin") or "")
                        dedupe_key = (
                            str(row.get("market_id") or ""),
                            str(row.get("seller_id") or ""),
                            asin,
                        )
                        if not asin or dedupe_key in seen_products:
                            continue
                        seen_products.add(dedupe_key)
                        deduplicated_rows.append(row)
                    rows = deduplicated_rows
                    product_tasks = [
                        {
                            "source_task_id": task["source_task_id"],
                            "market_id": task["market_id"],
                            "asin": row["data_asin"],
                            "post_code": task.get("post_code"),
                            "add_date": task.get("add_date"),
                        }
                        for row in rows
                    ]
                data = {
                    "items": rows,
                    "row_count": len(rows),
                    "product_detail_tasks": product_tasks,
                }
                schema = "amazon.merchant-products.v1"
                if product_tasks:
                    normalized_products = tuple(
                        NormalizedInput.from_payload(
                            {
                                **row,
                                "marketplace_id": task["marketplace_code"],
                                "marketplace_code": task["marketplace_code"],
                                "product_url": f"https://{market.domain}/dp/{row['asin']}",
                                "postal_code": task.get("post_code"),
                                "source": row,
                            },
                            input_key=(
                                f"{task['marketplace_code']}:{row['asin']}:"
                                f"{task.get('post_code') or '-'}"
                            ),
                            marketplace_id=task["marketplace_code"],
                            postal_code=task.get("post_code"),
                            source=row,
                        )
                        for row in product_tasks
                    )
                    followup_jobs = (
                        FollowupJob(
                            kind="product_hw"
                            if task["marketplace_code"] == "JP"
                            else "product",
                            inputs=normalized_products,
                            execution_mode=item.execution_mode.value,
                            priority=50 if item.execution_mode.value == "realtime" else 0,
                            max_attempts=item.max_attempts,
                            reason="merchant_product_detail_expansion",
                        ),
                    )
        except Exception as exc:
            await self._fetcher.report(fetched, ResourceOutcome.PARSE_ERROR)
            failure = CrawlFailure(
                "parse_error",
                "the merchant response could not be parsed",
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
                data=data,
                evidence=evidence,
                schema_version=schema,
                followup_jobs=followup_jobs,
            )
        )
