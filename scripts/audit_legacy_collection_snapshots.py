from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from amazon_crawler.plugins.collection_parser import (
    CATEGORY_FIELDS,
    RANK_FIELDS,
    SEARCH_FIELDS,
    parse_category_html,
    parse_rank_html,
    parse_search_html,
)


MARKET_ID = "A28R8C7NBKEWEA"
BASE_URL = "https://www.amazon.ie"


def _check(
    name: str,
    rows: list[dict[str, Any]],
    expected: dict[str, Any],
    *,
    asin_field: str,
    expected_fields: tuple[str, ...],
    category_tree: str | None = None,
) -> dict[str, Any]:
    mismatches: list[str] = []
    if len(rows) != int(expected["items"]):
        mismatches.append("row_count")
    if not rows or rows[0].get(asin_field) != expected["first_asin"]:
        mismatches.append("first_asin")
    if rows and set(rows[0]) != set(expected_fields):
        mismatches.append("field_set")
    if category_tree is not None and category_tree != expected.get("category_tree"):
        mismatches.append("category_tree")
    return {
        "task": name,
        "row_count": len(rows),
        "first_asin": rows[0].get(asin_field) if rows else None,
        "field_count": len(rows[0]) if rows else 0,
        "mismatches": mismatches,
    }


def audit(snapshot_root: Path) -> dict[str, Any]:
    expected = json.loads(
        (snapshot_root / "list-task-results.json").read_text(encoding="utf-8")
    )
    search = parse_search_html(
        (snapshot_root / "search-attempt-1.html").read_text(
            encoding="utf-8", errors="replace"
        ),
        {
            "market_id": MARKET_ID,
            "post_code": "D02 R5Y3",
            "keyword": "ankle socks",
            "turn_page": 1,
            "frequent": 0,
            "add_date": None,
        },
        base_url=BASE_URL,
    )
    category = parse_category_html(
        (snapshot_root / "asin_list-attempt-2.html").read_text(
            encoding="utf-8", errors="replace"
        ),
        {
            "market_id": MARKET_ID,
            "post_code": "D02 R5Y3",
            "category_id": "snapshot-category",
            "page": 1,
            "add_date": None,
        },
        base_url=BASE_URL,
    )
    rank, continuation = parse_rank_html(
        (snapshot_root / "rank_list-attempt-1.html").read_text(
            encoding="utf-8", errors="replace"
        ),
        {
            "market_id": MARKET_ID,
            "category_id": "snapshot-category",
            "url_type": "bestsellers",
        },
        base_url=BASE_URL,
    )
    checks = [
        _check(
            "search",
            search,
            expected["search"],
            asin_field="data_asin",
            expected_fields=SEARCH_FIELDS,
        ),
        _check(
            "asin_list",
            category,
            expected["asin_list"],
            asin_field="data_asin",
            expected_fields=CATEGORY_FIELDS,
        ),
        _check(
            "rank_list",
            rank,
            expected["rank_list"],
            asin_field="asin",
            expected_fields=RANK_FIELDS,
            category_tree=rank[0].get("category_name") if rank else None,
        ),
    ]
    if bool(continuation) != bool(expected["rank_list"].get("has_next")):
        checks[-1]["mismatches"].append("has_next")
    return {
        "ok": all(not check["mismatches"] for check in checks),
        "source": str(snapshot_root),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=(
            Path(__file__).resolve().parents[2]
            / "debug_html"
            / "ie_smoke_20260723"
        ),
    )
    args = parser.parse_args()
    report = audit(args.snapshot_root.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
