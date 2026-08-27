#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

from amazon_crawler.infra.legacy_compat import LEGACY_TASKS


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_dict_keys(path: Path, function: str, name: str) -> set[str]:
    tree = ast.parse(_read(path), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != function:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Assign):
                continue
            if any(isinstance(target, ast.Name) and target.id == name for target in child.targets):
                if not isinstance(child.value, ast.Dict):
                    break
                keys: set[str] = set()
                for key in child.value.keys:
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        raise ValueError(
                            f"{path.name}: {function}.{name} has a non-literal key"
                        )
                    keys.add(key.value)
                return keys
    raise ValueError(f"{path.name}: {function}.{name} dictionary assignment was not found")


def _contains_all(text: str, fragments: tuple[str, ...]) -> bool:
    return all(fragment in text for fragment in fragments)


def audit(old_root: Path, project_root: Path) -> dict[str, Any]:
    old_root = old_root.resolve()
    project_root = project_root.resolve()
    main = _read(old_root / "main.py")
    publisher = _read(old_root / "services" / "publisher.py")
    saver = _read(old_root / "services" / "data_saver.py")
    monitor = _read(old_root / "tools" / "monitor.py")
    db_utils = _read(old_root / "tools" / "db_utils.py")
    cookies = _read(old_root / "pool" / "cookies_pool.py")
    proxies = _read(old_root / "pool" / "fast_proxypool.py")

    publisher_routes = _function_dict_keys(
        old_root / "services" / "publisher.py", "publish_task_loop", "fun_dict"
    )
    expected_legacy_tasks = set(LEGACY_TASKS)
    v2_paths = {
        "worker": project_root / "src" / "amazon_crawler" / "application" / "worker.py",
        "store": project_root / "src" / "amazon_crawler" / "infra" / "sqlite_store.py",
        "legacy": project_root / "src" / "amazon_crawler" / "infra" / "legacy_compat.py",
        "sinks": project_root / "src" / "amazon_crawler" / "infra" / "result_sinks.py",
        "delivery": project_root / "src" / "amazon_crawler" / "application" / "delivery_worker.py",
        "resources": project_root / "src" / "amazon_crawler" / "infra" / "resources.py",
        "harvester": project_root / "src" / "amazon_crawler" / "infra" / "cookie_harvester.py",
        "maintenance": project_root / "src" / "amazon_crawler" / "application" / "cookie_maintenance.py",
        "api": project_root / "src" / "amazon_crawler" / "interfaces" / "api.py",
        "ui": project_root / "src" / "amazon_crawler" / "interfaces" / "static" / "index.html",
    }
    missing_v2_paths = sorted(name for name, path in v2_paths.items() if not path.is_file())
    v2_text = {
        name: _read(path) for name, path in v2_paths.items() if path.is_file()
    }

    groups: list[dict[str, Any]] = [
        {
            "name": "task_orchestration",
            "legacy_sources": ["main.py", "services/publisher.py"],
            "v2_sources": [
                "application/worker.py",
                "infra/sqlite_store.py",
                "infra/legacy_compat.py",
            ],
            "checks": {
                "all_legacy_publish_routes": set(publisher_routes) == expected_legacy_tasks,
                "all_legacy_cli_types": all(
                    repr(task.removesuffix("_jp")).strip("'") in main
                    for task in expected_legacy_tasks
                ),
                "publisher_claims_pending_rows": _contains_all(
                    publisher,
                    (
                        "find(state=0",
                        "SET state=2",
                        "task_active_registry",
                        "ZombieTaskRecover",
                    ),
                ),
                "v2_has_durable_claim_and_legacy_import": _contains_all(
                    v2_text.get("worker", "") + v2_text.get("store", "") + v2_text.get("legacy", ""),
                    ("claim_next", "lease_expires_at", "LegacyTaskImporter", "fetch_pending"),
                ),
            },
        },
        {
            "name": "result_and_status_delivery",
            "legacy_sources": ["services/data_saver.py", "tools/db_utils.py"],
            "v2_sources": [
                "infra/result_sinks.py",
                "application/delivery_worker.py",
                "infra/legacy_compat.py",
            ],
            "checks": {
                "legacy_zlib_result_buffer": _contains_all(
                    saver,
                    ("zlib.decompress", "zlib.compress", "lpop", "lpush"),
                ),
                "legacy_insert_ignore_and_dimension_stream": _contains_all(
                    saver,
                    ("INSERT IGNORE", "amazon_product_jp_dimession_items_buffer"),
                ),
                "legacy_status_batch_writeback": _contains_all(
                    db_utils,
                    ("class StatusBatchSaver", "status_buffer", "UPDATE", "state"),
                ),
                "v2_transactional_outbox_and_legacy_sinks": _contains_all(
                    v2_text.get("store", "")
                    + v2_text.get("sinks", "")
                    + v2_text.get("delivery", "")
                    + v2_text.get("legacy", ""),
                    (
                        "result_outbox",
                        "claim_delivery",
                        "LegacyRedisResultSink",
                        "LegacyMySQLResultSink",
                        "INSERT IGNORE",
                    ),
                ),
            },
        },
        {
            "name": "crash_recovery",
            "legacy_sources": ["tools/monitor.py", "services/publisher.py"],
            "v2_sources": ["infra/sqlite_store.py", "application/worker.py"],
            "checks": {
                "legacy_zombie_detection": _contains_all(
                    monitor,
                    ("state=2", "sismember", "SET state=0", "update_time"),
                ),
                "v2_expired_lease_recovery": _contains_all(
                    v2_text.get("store", "") + v2_text.get("worker", ""),
                    ("recover_expired_leases", "lease_expires_at", "lease_token"),
                ),
            },
        },
        {
            "name": "cookie_lifecycle",
            "legacy_sources": ["pool/cookies_pool.py"],
            "v2_sources": [
                "infra/resources.py",
                "infra/cookie_harvester.py",
                "application/cookie_maintenance.py",
            ],
            "checks": {
                "legacy_cache_refresh_and_pool_fallback": _contains_all(
                    cookies,
                    (
                        "update_interval = 1800",
                        'pattern_prefix = "cookie:"',
                        "blocking=False",
                        "直接穿透查询 Redis",
                        "mget",
                    ),
                ),
                "v2_cookie_consumer_and_producer": _contains_all(
                    v2_text.get("resources", "")
                    + v2_text.get("harvester", "")
                    + v2_text.get("maintenance", ""),
                    (
                        "class LegacyRedisCookieProvider",
                        "_direct_lookup",
                        "_refresh_lock",
                        "class AmazonCookieHarvester",
                        "address_not_applied",
                        "setex",
                        "run_once",
                    ),
                ),
            },
        },
        {
            "name": "proxy_lifecycle",
            "legacy_sources": ["pool/fast_proxypool.py"],
            "v2_sources": ["infra/resources.py"],
            "checks": {
                "legacy_proxy_pull_rotate_remove": _contains_all(
                    proxies,
                    ("pull_proxies", "proxy_queue", "get_proxy", "del_proxy"),
                ),
                "v2_proxy_singleflight_quarantine": _contains_all(
                    v2_text.get("resources", ""),
                    (
                        "class RotatingProxyProvider",
                        "_refresh_lock",
                        "quarantined_until",
                        "ProxyExtractionClient",
                    ),
                ),
            },
        },
        {
            "name": "management_surface",
            "legacy_sources": ["main.py"],
            "v2_sources": ["interfaces/api.py", "interfaces/static/index.html"],
            "checks": {
                "legacy_framework_web_manager": _contains_all(
                    main, ("start_funboost_web_manager", "mode == 4")
                ),
                "v2_job_api_and_ui": _contains_all(
                    v2_text.get("api", "") + v2_text.get("ui", ""),
                    ("/api/v1/jobs", "Amazon Crawler Control Plane"),
                ),
            },
        },
    ]
    for group in groups:
        checks = group["checks"]
        group["ok"] = bool(checks) and all(checks.values())
    return {
        "schema_version": "legacy-platform-source-audit.v1",
        "ok": not missing_v2_paths and all(group["ok"] for group in groups),
        "imports_executed": False,
        "legacy_task_count": len(expected_legacy_tasks),
        "missing_v2_paths": missing_v2_paths,
        "groups": groups,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit legacy orchestration, delivery, recovery, Cookie, proxy, and UI "
            "source contracts against their V2 architectural replacements."
        )
    )
    parser.add_argument(
        "--old-root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    try:
        report = audit(args.old_root, args.project_root)
    except (OSError, SyntaxError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": "legacy platform source audit could not be completed",
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
