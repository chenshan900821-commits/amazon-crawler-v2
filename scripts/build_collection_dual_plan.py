#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from amazon_crawler.plugins.marketplaces import MARKETPLACES
from scripts.collect_collection_dual_shadow import (
    COLLECTION_KINDS,
    PLAN_SCHEMA,
    validate_collection_dual_plan,
)
from scripts.collect_controlled_v2_shadow import _atomic_write_outside_project
from scripts.collect_product_dual_shadow import validate_product_dual_plan
from scripts.compile_controlled_evidence import ControlledEvidenceError


DEFAULT_CATEGORY_IDS = {
    "US": "172282",
}
DEFAULT_RANK_PATHS = {
    ("US", "172282"): "Best-Sellers-Electronics/zgbs/electronics/172282",
}


def build_plan(
    source: object,
    *,
    batch_id: str,
    kinds: list[str],
    keyword: str,
    category_id: str | None,
) -> dict[str, Any]:
    source = validate_product_dual_plan(source)
    requested = list(dict.fromkeys(kinds))
    if not requested or len(requested) != len(kinds):
        raise ControlledEvidenceError(
            "collection dual kinds must be non-empty and unique"
        )
    unsupported = sorted(set(requested) - COLLECTION_KINDS)
    if unsupported:
        raise ControlledEvidenceError("collection dual kinds are unsupported")
    seed = source["scenarios"][0]
    market_code = str(seed["marketplace_id"])
    market = MARKETPLACES.get(market_code)
    if market is None:
        raise ControlledEvidenceError("product seed uses an unsupported marketplace")
    selected_category = str(
        category_id or DEFAULT_CATEGORY_IDS.get(market_code) or ""
    ).strip()
    if any(kind in {"category_asin_list", "rank_list"} for kind in requested):
        if not selected_category:
            raise ControlledEvidenceError(
                "this marketplace requires an explicit category ID"
            )
    seed_input = seed["input"]
    common = {
        "id": f"dual-{source['run_id']}",
        "market_id": market.amazon_marketplace_id,
        "post_code": seed["postal_code"],
        "add_date": seed_input.get("add_date"),
    }
    inputs: dict[str, dict[str, Any]] = {
        "search": {
            **common,
            "keyword": keyword,
            "turn_page": 1,
            "frequent": 0,
        },
        "search_hour": {
            **common,
            "keyword": keyword,
            "turn_page": 1,
            "frequent": 0,
            "data_hour": "2026-08-23 08:00:00",
        },
        "reviews": {
            **common,
            "asin": seed_input.get("asin"),
        },
        "category_asin_list": {
            **common,
            "category_id": selected_category,
            "page": 1,
        },
        "rank_list": {
            **common,
            "url": "https://" + market.domain + "/" + DEFAULT_RANK_PATHS.get(
                (market_code, selected_category),
                f"Best-Sellers/zgbs/{selected_category}",
            ),
            "url_type": "bestsellers",
            "category_id": selected_category,
            "page": 1,
        },
    }
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
                "case": "success",
                "authorized": True,
                "marketplace_id": market_code,
                "postal_code": seed["postal_code"],
                "input": inputs[kind],
            }
            for kind in requested
        ],
    }
    return validate_collection_dual_plan(plan)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build an authorized collection same-response plan from a product "
            "dual plan without copying any precomputed legacy result."
        )
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--kind", action="append", choices=sorted(COLLECTION_KINDS))
    parser.add_argument("--keyword", default="wireless mouse")
    parser.add_argument("--category-id")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        source = json.loads(args.source.read_text(encoding="utf-8"))
        plan = build_plan(
            source,
            batch_id=args.batch_id,
            kinds=args.kind or sorted(COLLECTION_KINDS),
            keyword=args.keyword,
            category_id=args.category_id,
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
            else "collection dual plan could not be built"
        )
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output.resolve()),
                "kinds": [scenario["kind"] for scenario in plan["scenarios"]],
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
