#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from amazon_crawler.plugins.marketplaces import MARKETPLACES
from scripts.collect_controlled_v2_shadow import _atomic_write_outside_project
from scripts.collect_merchant_dual_shadow import (
    MERCHANT_KINDS,
    PLAN_SCHEMA,
    validate_merchant_dual_plan,
)
from scripts.collect_product_dual_shadow import validate_product_dual_plan
from scripts.compile_controlled_evidence import ControlledEvidenceError


def build_plan(
    source: object,
    *,
    batch_id: str,
    kinds: list[str],
    seller_id: str,
    postal_code: str | None,
    add_date: str | None,
) -> dict[str, Any]:
    source = validate_product_dual_plan(source)
    requested = list(dict.fromkeys(kinds))
    if not requested or len(requested) != len(kinds):
        raise ControlledEvidenceError(
            "merchant dual kinds must be non-empty and unique"
        )
    if sorted(set(requested) - MERCHANT_KINDS):
        raise ControlledEvidenceError("merchant dual kinds are unsupported")
    seed = source["scenarios"][0]
    market_code = str(seed["marketplace_id"])
    market = MARKETPLACES.get(market_code)
    if market is None:
        raise ControlledEvidenceError("product seed uses an unsupported marketplace")
    selected_postal = str(postal_code or seed["postal_code"]).strip()
    selected_date = add_date or seed["input"].get("add_date")
    common = {
        "id": f"merchant-dual-{source['run_id']}",
        "seller_id": seller_id,
        "market_id": market.amazon_marketplace_id,
        "post_code": selected_postal,
        "add_date": selected_date,
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
                "postal_code": selected_postal,
                "input": {
                    **common,
                    **({"page": 1} if kind == "merchant_products" else {}),
                },
            }
            for kind in requested
        ],
    }
    return validate_merchant_dual_plan(plan)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build an authorized merchant same-response plan from an existing "
            "product plan without copying precomputed legacy results."
        )
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--kind", action="append", choices=sorted(MERCHANT_KINDS))
    parser.add_argument("--seller-id", required=True)
    parser.add_argument("--postal-code")
    parser.add_argument("--add-date")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        source = json.loads(args.source.read_text(encoding="utf-8"))
        plan = build_plan(
            source,
            batch_id=args.batch_id,
            kinds=args.kind or sorted(MERCHANT_KINDS),
            seller_id=args.seller_id,
            postal_code=args.postal_code,
            add_date=args.add_date,
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
            else "merchant dual plan could not be built"
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
