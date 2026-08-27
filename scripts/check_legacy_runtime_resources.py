#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from amazon_crawler.bootstrap import build_application
from amazon_crawler.config import Settings
from amazon_crawler.infra.legacy_runtime_config import load_legacy_runtime_config


async def _check(args) -> dict[str, object]:
    config_path = args.legacy_config.resolve()
    runtime = load_legacy_runtime_config(
        config_path,
        allow_external_cookie_read=args.allow_external_cookie_read,
        allow_external_proxy_api=args.probe_proxy,
    )
    settings = replace(
        Settings.from_env(args.output.parent if args.output else Path.cwd()),
        db_path=(args.output.parent if args.output else Path.cwd())
        / "runtime-resource-check-state.db",
        worker_enabled=False,
        delivery_worker_enabled=False,
        legacy_mysql_url=None,
        legacy_result_redis_url=None,
        cookie_redis_url=runtime.cookie_redis_url,
        cookie_redis_jp_url=runtime.cookie_redis_overseas_url,
        cookie_redis_overseas_url=runtime.cookie_redis_overseas_url,
        proxy_extract_url=runtime.proxy_extract_url,
        proxy_username=runtime.proxy_username,
        proxy_password=runtime.proxy_password,
        amazon_cookie=None,
        merchant_cookie=runtime.merchant_cookie,
        legacy_config_loaded=True,
        legacy_target_scopes=runtime.target_scopes,
    )
    app = build_application(settings)
    cookie_health = await app.request_context.cookie_provider.refresh()
    route_health = await app.request_context.cookie_route_health()
    proxy_acquired = None
    if args.probe_proxy:
        proxy_acquired = (
            await app.request_context.proxy_provider.acquire("diagnostic", "US")
            is not None
        )
    proxy_health = await app.request_context.proxy_provider.health()
    return {
        "schema_version": "amazon-crawler.runtime-resource-check.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "authorization": {
            "external_cookie_read": bool(args.allow_external_cookie_read),
            "external_proxy_api": bool(args.probe_proxy),
        },
        "legacy_config": {
            "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "imports_executed": False,
            "values_disclosed": False,
            "target_scopes": runtime.target_scopes,
        },
        "cookie": cookie_health.as_public_dict(),
        "cookie_routes": route_health,
        "proxy": {
            **proxy_health.as_public_dict(),
            "probe_acquired": proxy_acquired,
        },
        "transport": {
            "backend": app.fetcher.backend,
            "tls_impersonation": app.fetcher.tls_impersonation,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-external-cookie-read", action="store_true")
    parser.add_argument("--probe-proxy", action="store_true")
    args = parser.parse_args()
    if not args.allow_external_cookie_read:
        raise SystemExit("--allow-external-cookie-read is required for this check")
    try:
        report = asyncio.run(_check(args))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "values_disclosed": False,
                },
                sort_keys=True,
            )
        )
        return 2
    cookie = report["cookie"]
    proxy = report["proxy"]
    report["ok"] = bool(
        cookie["available"] > 0
        and report["transport"]["backend"] == "curl_cffi"
        and report["transport"]["tls_impersonation"] is True
        and (not args.probe_proxy or proxy["probe_acquired"] is True)
    )
    if args.output:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "cookie_available": cookie["available"],
                "cookie_groups": cookie["groups"],
                "proxy_acquired": proxy["probe_acquired"],
                "transport": report["transport"],
                "receipt": str(args.output.resolve()) if args.output else None,
                "values_disclosed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
