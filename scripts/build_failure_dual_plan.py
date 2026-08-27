#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from amazon_crawler.plugins.marketplaces import MARKETPLACES
from scripts.collect_controlled_v2_shadow import _atomic_write_outside_project
from scripts.collect_failure_dual_shadow import (
    ALL_KINDS,
    FAILURE_CASES,
    PLAN_SCHEMA,
    validate_failure_dual_plan,
)
from scripts.collect_product_dual_shadow import validate_product_dual_plan
from scripts.compile_controlled_evidence import ControlledEvidenceError


def _input_for(
    kind: str,
    case: str,
    *,
    source_input: dict[str, Any],
    market_code: str,
    postal_code: str,
    seller_id: str,
    asin: str | None,
    keyword: str | None,
    search_page: int,
    category_id: str | None,
    category_page: int,
    rank_url: str | None,
    rank_url_type: str,
    merchant_products_seller_id: str | None,
    merchant_products_page: int,
) -> dict[str, Any]:
    market = MARKETPLACES[market_code]
    common = {
        "id": f"failure-{kind}-{case}",
        "market_id": market.amazon_marketplace_id,
        "post_code": postal_code,
        "add_date": source_input.get("add_date"),
    }
    missing_asin = "B000000000"
    if kind in {"product", "product_hw", "product_time"}:
        return {
            **common,
            "asin": asin
            or (missing_asin if case == "no_result" else source_input["asin"]),
        }
    if kind in {"search", "search_hour"}:
        result = {
            **common,
            "keyword": (
                keyword
                or "zzzz-codex-no-result-20260823-9f3b7c"
                if case == "no_result"
                else keyword or "wireless mouse"
            ),
            "turn_page": search_page,
            "frequent": 0,
        }
        if kind == "search_hour":
            result["data_hour"] = "2026-08-23 08:00:00"
        return result
    if kind == "reviews":
        return {
            **common,
            "asin": asin
            or (missing_asin if case == "no_result" else source_input["asin"]),
        }
    if kind == "category_asin_list":
        return {
            **common,
            "category_id": category_id
            or ("999999999999" if case == "no_result" else "172282"),
            "page": category_page,
        }
    if kind == "rank_list":
        category = category_id or (
            "999999999999" if case == "no_result" else "172282"
        )
        path = (
            f"Best-Sellers/zgbs/{category}"
            if case == "no_result"
            else "Best-Sellers-Electronics/zgbs/electronics/172282"
        )
        return {
            **common,
            "url": rank_url or f"https://{market.domain}/{path}",
            "url_type": rank_url_type,
            "category_id": category,
            "page": 1,
        }
    selected_seller = "A0000000000000" if case == "no_result" else seller_id
    if kind == "merchant_products" and merchant_products_seller_id:
        selected_seller = merchant_products_seller_id
    result = {**common, "seller_id": selected_seller}
    if kind == "merchant_products":
        result["page"] = merchant_products_page
    return result


def build_plan(
    source: object,
    *,
    batch_id: str,
    kinds: list[str],
    case: str,
    seller_id: str,
    postal_code: str | None,
    marketplace_code: str | None = None,
    asin: str | None = None,
    keyword: str | None = None,
    search_page: int = 1,
    category_id: str | None = None,
    category_page: int = 1,
    rank_url: str | None = None,
    rank_url_type: str = "bestsellers",
    merchant_products_seller_id: str | None = None,
    merchant_products_page: int = 1,
) -> dict[str, Any]:
    source = validate_product_dual_plan(source)
    requested = list(dict.fromkeys(kinds))
    if not requested or len(requested) != len(kinds):
        raise ControlledEvidenceError("failure dual kinds must be non-empty and unique")
    if sorted(set(requested) - ALL_KINDS) or case not in FAILURE_CASES:
        raise ControlledEvidenceError("failure dual kind or case is unsupported")
    seed = source["scenarios"][0]
    market_code = str(marketplace_code or seed["marketplace_id"]).upper()
    if market_code not in MARKETPLACES:
        raise ControlledEvidenceError("product seed uses an unsupported marketplace")
    selected_postal = str(postal_code or seed["postal_code"]).strip()
    if not 1 <= search_page <= 400:
        raise ControlledEvidenceError(
            "search page must be between one and four hundred"
        )
    if not 1 <= category_page <= 400:
        raise ControlledEvidenceError(
            "category page must be between one and four hundred"
        )
    if rank_url_type not in {"bestsellers", "new-releases"}:
        raise ControlledEvidenceError(
            "rank URL type must be bestsellers or new-releases"
        )
    if not 1 <= merchant_products_page <= 400:
        raise ControlledEvidenceError(
            "merchant products page must be between one and four hundred"
        )
    plan = {
        "schema_version": PLAN_SCHEMA,
        "batch_id": batch_id,
        "run_id": source["run_id"],
        "authorization_reference": source["authorization_reference"],
        "environment": source["environment"],
        "check_receipts": source["check_receipts"],
        "scenarios": [
            {
                "kind": kind,
                "case": case,
                "authorized": True,
                "marketplace_id": market_code,
                "postal_code": selected_postal,
                "input": _input_for(
                    kind,
                    case,
                    source_input=seed["input"],
                    market_code=market_code,
                    postal_code=selected_postal,
                    seller_id=seller_id,
                    asin=asin,
                    keyword=keyword,
                    search_page=search_page,
                    category_id=category_id,
                    category_page=category_page,
                    rank_url=rank_url,
                    rank_url_type=rank_url_type,
                    merchant_products_seller_id=merchant_products_seller_id,
                    merchant_products_page=merchant_products_page,
                ),
            }
            for kind in requested
        ],
    }
    return validate_failure_dual_plan(plan)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build response-proven no-result or natural-throttle plans. This "
            "builder never sends traffic and never attempts to induce throttling."
        )
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--kind", action="append", choices=sorted(ALL_KINDS))
    parser.add_argument("--case", required=True, choices=sorted(FAILURE_CASES))
    parser.add_argument("--seller-id", default="ACSFBZX3I4JAS")
    parser.add_argument("--marketplace-code", choices=sorted(MARKETPLACES))
    parser.add_argument("--asin")
    parser.add_argument("--keyword")
    parser.add_argument("--search-page", type=int, default=1)
    parser.add_argument("--category-id")
    parser.add_argument("--category-page", type=int, default=1)
    parser.add_argument("--rank-url")
    parser.add_argument(
        "--rank-url-type",
        choices=("bestsellers", "new-releases"),
        default="bestsellers",
    )
    parser.add_argument("--merchant-products-seller-id")
    parser.add_argument("--merchant-products-page", type=int, default=1)
    parser.add_argument("--postal-code")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        source = json.loads(args.source.read_text(encoding="utf-8"))
        plan = build_plan(
            source,
            batch_id=args.batch_id,
            kinds=args.kind or sorted(ALL_KINDS),
            case=args.case,
            seller_id=args.seller_id,
            postal_code=args.postal_code,
            marketplace_code=args.marketplace_code,
            asin=args.asin,
            keyword=args.keyword,
            search_page=args.search_page,
            category_id=args.category_id,
            category_page=args.category_page,
            rank_url=args.rank_url,
            rank_url_type=args.rank_url_type,
            merchant_products_seller_id=args.merchant_products_seller_id,
            merchant_products_page=args.merchant_products_page,
        )
        _atomic_write_outside_project(args.output, plan, project_root=project_root)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ControlledEvidenceError,
    ) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "failure dual plan could not be built"
        )
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output.resolve()),
                "case": args.case,
                "kinds": [scenario["kind"] for scenario in plan["scenarios"]],
                "network_requests": 0,
                "precomputed_legacy_results_copied": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
