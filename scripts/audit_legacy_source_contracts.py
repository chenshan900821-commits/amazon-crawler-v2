from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any

from amazon_crawler.infra.legacy_compat import (
    LEGACY_MAX_ATTEMPTS,
    LEGACY_TASKS,
)
from amazon_crawler.plugins.amazon_collections import RANK_STREAM_CONCURRENCY


SPIDER_FILES = {
    "search_jp": "amazon_search.py",
    "search_hour_jp": "amazon_search_hour.py",
    "product_jp": "amazon_product_details.py",
    "product_hw_jp": "amazon_product_details_hw.py",
    "product_time_jp": "amazon_product_details_time.py",
    "reviews": "amazon_reviews.py",
    "merchant": "amazon_merchant.py",
    "merchant_home": "amazon_merchant_home.py",
    "merchant_products": "amazon_merchant_products.py",
    "asin_list_jp": "amazon_category_asin_list.py",
    "rank_list_jp": "amazon_rank_list.py",
}
BUSINESS_STATE_EXCEPTIONS = {
    "search_jp": {"FatalError": -2, "NoPageError": -3, "OnePageError": -4},
    "search_hour_jp": {"FatalError": -2, "NoPageError": -3, "OnePageError": -4},
    "product_jp": {"FatalError": -2, "NoPageError": -3},
    "product_hw_jp": {"FatalError": -2, "NoPageError": -3},
    "product_time_jp": {"FatalError": -2, "NoPageError": -3},
    "reviews": {"FatalError": -2, "NoPageError": -3, "NoReviewError": -4},
    "merchant": {"FatalError": -2, "NoPageError": -3},
    "merchant_home": {"FatalError": -2, "NoPageError": -3},
    "merchant_products": {"FatalError": -2, "NoPageError": -3},
    "asin_list_jp": {"FatalError": -2, "NoPageError": -3, "NoResultError": -4},
    "rank_list_jp": {"FatalError": -2, "NoPageError": -3, "NoResultError": -4},
}
STATIC_COOKIE_TASKS = {"merchant", "rank_list_jp"}
HARDWARE_ONLY_COOKIE_TASKS = {"product_hw_jp"}


def _literal_assignment(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            value = node.value
            if value is None:
                break
            return ast.literal_eval(value)
    raise ValueError(f"{path.name}: literal assignment {name} was not found")


def _business_state_matches(text: str, exception_name: str, state: int) -> bool:
    pattern = re.compile(
        rf"err_type\s*==\s*['\"]{re.escape(exception_name)}['\"]\s*:\s*state\s*=\s*{state}",
        re.MULTILINE,
    )
    return pattern.search(text) is not None


def audit(old_root: Path) -> dict[str, Any]:
    old_root = old_root.resolve()
    settings_path = old_root / "settings" / "config.py"
    spider_root = old_root / "spider"
    spider_table = _literal_assignment(settings_path, "SPIDER_TABLE")
    default_retries = int(_literal_assignment(settings_path, "MAX_RETRY_NUM"))
    task_reports: list[dict[str, Any]] = []

    for task_name, spider_file in SPIDER_FILES.items():
        path = spider_root / spider_file
        text = path.read_text(encoding="utf-8")
        source_task_name = _literal_assignment(path, "task_name")
        local_retries = (
            int(_literal_assignment(path, "MAX_RETRY_NUM"))
            if task_name == "search_hour_jp"
            else default_retries
        )
        old_mapping = spider_table.get(task_name)
        v2_mapping = LEGACY_TASKS[task_name]
        mapping_matches = bool(
            isinstance(old_mapping, list)
            and len(old_mapping) == 2
            and tuple(old_mapping) == v2_mapping[:2]
        )
        retry_matches = LEGACY_MAX_ATTEMPTS.get(
            task_name,
            default_retries + 1,
        ) == local_retries + 1
        states_match = all(
            _business_state_matches(text, exception_name, state)
            for exception_name, state in BUSINESS_STATE_EXCEPTIONS[task_name].items()
        ) and "update_task_state_async(task_id, -1, task_name)" in text
        result_buffer_matches = (
            'f"amazon_{task_name}_items_buffer"' in text
            or "f'amazon_{task_name}_items_buffer'" in text
        )

        if task_name in STATIC_COOKIE_TASKS:
            cookie_route_matches = "cookies = MERCHANT_COOKIE" in text
            expected_cookie_route = "static_merchant_cookie"
        elif task_name in HARDWARE_ONLY_COOKIE_TASKS:
            cookie_route_matches = (
                "cookie_pool_hw = CookiePool(4)" in text
                and "target_pool = cookie_pool_hw" in text
            )
            expected_cookie_route = "overseas_pool"
        else:
            cookie_route_matches = (
                "cookie_pool_hw = CookiePool(4)" in text
                and 'market_id == "A1VC38T7YXB528"' in text
            )
            expected_cookie_route = "jp_overseas_else_default"

        checks = {
            "task_name": source_task_name == task_name,
            "table_mapping": mapping_matches,
            "retry_budget": retry_matches,
            "business_states": states_match,
            "result_buffer": result_buffer_matches,
            "cookie_route": cookie_route_matches,
            "dynamic_proxy": "proxy_pool.get_proxy(" in text,
        }
        if task_name == "rank_list_jp":
            checks["stream_concurrency"] = (
                int(_literal_assignment(path, "STREAM_CONCURRENCY"))
                == RANK_STREAM_CONCURRENCY
            )
        task_reports.append(
            {
                "task_name": task_name,
                "source": f"spider/{spider_file}",
                "v2_kind": v2_mapping[2],
                "total_attempts": local_retries + 1,
                "cookie_route": expected_cookie_route,
                "checks": checks,
                "ok": all(checks.values()),
            }
        )

    return {
        "schema_version": "legacy-source-audit.v1",
        "ok": len(task_reports) == len(LEGACY_TASKS)
        and all(report["ok"] for report in task_reports),
        "legacy_task_count": len(task_reports),
        "tasks": task_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--old-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    args = parser.parse_args()
    try:
        report = audit(args.old_root)
    except (OSError, SyntaxError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": "legacy source audit could not be completed",
                    },
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
