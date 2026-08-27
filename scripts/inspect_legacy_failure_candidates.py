from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from amazon_crawler.infra.legacy_compat import LEGACY_TASKS, SQLAlchemyLegacyGateway
from amazon_crawler.infra.legacy_runtime_config import load_legacy_runtime_config
from scripts.collect_controlled_v2_shadow import _atomic_write_outside_project


SCHEMA_VERSION = "legacy-failure-candidates.v1"
SAFE_INPUT_FIELDS = {
    "search_jp": ("id", "keyword", "market_id", "post_code", "turn_page", "frequent", "add_date"),
    "search_hour_jp": ("id", "keyword", "market_id", "post_code", "turn_page", "frequent", "data_hour", "add_date"),
    "product_jp": ("id", "asin", "market_id", "post_code", "add_date"),
    "product_hw_jp": ("id", "asin", "market_id", "post_code", "add_date"),
    "product_time_jp": ("id", "asin", "market_id", "post_code", "add_date"),
    "reviews": ("id", "asin", "market_id", "post_code", "add_date"),
    "merchant": ("id", "seller_id", "market_id"),
    "merchant_home": ("id", "seller_id", "market_id", "post_code", "object_url", "add_date"),
    "merchant_products": ("id", "seller_id", "market_id", "post_code", "page", "source_task_id", "add_date"),
    "asin_list_jp": ("id", "category_id", "market_id", "post_code", "page", "add_date"),
    "rank_list_jp": ("id", "url", "url_type", "category_id", "market_id", "post_code", "page", "add_date"),
}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def safe_candidates(task_name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = SAFE_INPUT_FIELDS[task_name]
    return [
        {field: _json_value(row[field]) for field in fields if field in row}
        for row in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read bounded historical legacy task states for candidate discovery."
    )
    parser.add_argument("--legacy-config", required=True, type=Path)
    parser.add_argument("--task-name", required=True, choices=sorted(LEGACY_TASKS))
    parser.add_argument("--state", action="append", type=int, required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        runtime = load_legacy_runtime_config(args.legacy_config)
        if not runtime.mysql_url:
            raise RuntimeError("legacy config does not expose a loopback MySQL target")
        gateway = SQLAlchemyLegacyGateway(runtime.mysql_url)
        rows = gateway.fetch_by_states(
            args.task_name,
            args.state,
            limit=args.limit,
        )
        report = {
            "schema_version": SCHEMA_VERSION,
            "task_name": args.task_name,
            "v2_kind": LEGACY_TASKS[args.task_name][2],
            "states": sorted(set(args.state)),
            "candidate_count": len(rows),
            "candidates": safe_candidates(args.task_name, rows),
            "read_only": True,
        }
        _atomic_write_outside_project(args.output, report, project_root=project_root)
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "candidate_count": len(rows),
                "output": str(args.output.resolve()),
                "read_only": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
