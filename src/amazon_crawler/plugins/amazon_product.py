from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

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
from amazon_crawler.plugins.amazon_parser import parse_product_html
from amazon_crawler.plugins.response_policy import (
    classify_amazon_page,
    classify_product_completeness,
)
from amazon_crawler.plugins.marketplaces import (
    AMAZON_ID_TO_MARKETPLACE,
    DOMAIN_TO_MARKETPLACE,
    MARKETPLACES,
    marketplace_language,
)


ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
PATH_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)


class AmazonProductPlugin:
    kind = "product"

    def __init__(
        self,
        fetcher: HttpFetcher,
        evidence_store: EvidenceStore,
        *,
        require_cookie: bool = True,
        kind: str = "product",
    ) -> None:
        aliases = {
            "amazon.product": "product",
            "product_jp": "product",
            "product_hw_jp": "product_hw",
            "product_time_jp": "product_time",
        }
        self.base_kind = aliases.get(kind, kind)
        if self.base_kind not in {"product", "product_hw", "product_time"}:
            raise ValueError("unsupported product task kind")
        self.kind = kind
        self._fetcher = fetcher
        self._evidence_store = evidence_store
        self._require_cookie = require_cookie

    def normalize(
        self, source: Any, marketplace_id: str | None, postal_code: str | None
    ) -> NormalizedInput:
        if isinstance(source, dict):
            raw = dict(source)
            value = raw.get("asin") or raw.get("url") or raw.get("product_url")
            if not isinstance(value, str):
                raise ValidationError("product object requires asin, url, or product_url")
            normalized = self.normalize(
                value,
                str(raw.get("market_id") or raw.get("marketplace_id") or marketplace_id or ""),
                str(raw.get("post_code") or raw.get("postal_code") or postal_code or "") or None,
            )
            market = MARKETPLACES[normalized.marketplace_id]
            payload = normalized.as_dict()
            payload.update(
                {
                    "id": str(raw.get("id") or ""),
                    "add_date": raw.get("add_date"),
                    "market_id": market.amazon_marketplace_id,
                    "marketplace_code": market.id,
                }
            )
            return NormalizedInput.from_payload(
                payload,
                input_key=normalized.input_key,
                marketplace_id=market.id,
                postal_code=normalized.postal_code,
                source=source,
            )
        if not isinstance(source, str):
            raise ValidationError("product input must be an ASIN, Amazon URL, or legacy task object")
        source = source.strip()
        inferred_marketplace: str | None = None
        asin: str | None = None

        if ASIN_RE.fullmatch(source):
            asin = source.upper()
        else:
            parsed = urlparse(source)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            market = DOMAIN_TO_MARKETPLACE.get(host)
            if not market:
                raise ValidationError("only recognized Amazon product URLs are allowed")
            if parsed.username or parsed.password:
                raise ValidationError("product URL must not contain credentials")
            inferred_marketplace = market.id
            match = PATH_ASIN_RE.search(parsed.path)
            asin = match.group(1).upper() if match else None
            if not asin:
                query_asin = parse_qs(parsed.query).get("asin", [None])[0]
                asin = query_asin.upper() if query_asin and ASIN_RE.fullmatch(query_asin) else None

        selected_marketplace = (marketplace_id or inferred_marketplace or "").upper()
        if selected_marketplace in AMAZON_ID_TO_MARKETPLACE:
            selected_marketplace = AMAZON_ID_TO_MARKETPLACE[selected_marketplace].id
        if selected_marketplace not in MARKETPLACES:
            raise ValidationError("marketplace_id is required for an ASIN and must be supported")
        if inferred_marketplace and marketplace_id and selected_marketplace != inferred_marketplace:
            raise ValidationError("marketplace_id conflicts with the Amazon URL domain")
        if not asin or not ASIN_RE.fullmatch(asin):
            raise ValidationError("input must contain a valid 10-character ASIN")
        market = MARKETPLACES[selected_marketplace]
        url = f"https://{market.domain}/dp/{asin}"
        return NormalizedInput(
            asin=asin,
            marketplace_id=selected_marketplace,
            # Persist the canonical URL, never the caller's query string or
            # fragment, which may contain referral or credential material.
            source=url if inferred_marketplace else source,
            product_url=url,
            postal_code=postal_code.strip() if postal_code and postal_code.strip() else None,
        )

    async def execute(self, item: ClaimedItem) -> PluginOutcome:
        market_code = str(item.input["marketplace_id"])
        query = urlencode(
            {
                "th": 1,
                "psc": 1,
                "language": marketplace_language(market_code),
            }
        )
        url = f"{item.input['product_url']}?{query}"
        fetched = await self._fetcher.fetch(
            url,
            marketplace_id=market_code,
            postal_code=item.input.get("postal_code"),
            purpose=item.kind,
            require_cookie=self._require_cookie,
        )
        if isinstance(fetched, CrawlFailure):
            return PluginOutcome(failure=fetched)
        issue = classify_amazon_page(
            fetched.html,
            not_found_code="product_not_found",
            purpose="product",
        )
        if not issue:
            issue = classify_product_completeness(fetched.html)
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
        try:
            data = parse_product_html(
                fetched.html,
                asin=str(item.input["asin"]),
                product_url=url,
                marketplace_id=str(item.input["marketplace_id"]),
                postal_code=item.input.get("postal_code"),
                task_id=str(item.input.get("id") or item.id),
            )
        except Exception as exc:
            await self._fetcher.report(fetched, ResourceOutcome.PARSE_ERROR)
            failure = CrawlFailure(
                "parse_error",
                "the product response could not be parsed",
                retryable=True,
                details={"error_type": type(exc).__name__},
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
        if not data.get("title"):
            await self._fetcher.report(fetched, ResourceOutcome.PARSE_ERROR)
            failure = CrawlFailure(
                    "missing_product_identity",
                    "response did not contain a product title",
                    retryable=True,
                    details={"missing": data.get("quality", {}).get("missing_core_fields", [])},
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
        data.update(
            {
                "requested_postal_code": item.input.get("postal_code"),
                "execution_mode": item.execution_mode.value,
            }
        )
        schema_version = "amazon.product.v2"
        if self.base_kind == "product_time":
            data.pop("dimension_items", None)
            data["observed_at"] = datetime.now(UTC).isoformat()
            data["observation_id"] = item.id
            schema_version = "amazon.product-observation.v1"
        return PluginOutcome(
            result=CrawlResult(
                data=data,
                evidence=evidence,
                schema_version=schema_version,
            )
        )
