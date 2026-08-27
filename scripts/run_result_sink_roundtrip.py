#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from amazon_crawler.bootstrap import build_application
from amazon_crawler.config import Settings
from amazon_crawler.domain.models import CrawlResult
from amazon_crawler.infra.legacy_compat import LegacyRedisResultBuffer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--controlled-receipt", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--authorization-reference")
    parser.add_argument("--confirm-loopback-write", action="store_true")
    args = parser.parse_args()
    if not args.confirm_loopback_write:
        raise SystemExit("--confirm-loopback-write is required")

    config_path = args.legacy_config.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    state_path = output.parent / "result-sink-roundtrip-state.db"
    settings = Settings.from_env(output.parent)
    from amazon_crawler.infra.legacy_runtime_config import load_legacy_runtime_config

    runtime = load_legacy_runtime_config(config_path)
    if not runtime.mysql_url or not runtime.result_redis_url:
        raise SystemExit("legacy config is missing loopback result destinations")
    if runtime.target_scopes.get("mysql") != "loopback" or runtime.target_scopes.get(
        "result_redis"
    ) != "loopback":
        raise SystemExit("result sink roundtrip accepts loopback targets only")
    settings = replace(
        settings,
        db_path=state_path,
        legacy_mysql_url=runtime.mysql_url,
        legacy_result_redis_url=runtime.result_redis_url,
        worker_enabled=False,
        delivery_worker_enabled=False,
        legacy_config_loaded=True,
        legacy_target_scopes=runtime.target_scopes,
        cookie_redis_url=None,
        cookie_redis_jp_url=None,
        cookie_redis_overseas_url=None,
        proxy_extract_url=None,
        http_proxy=None,
        amazon_cookie=None,
        merchant_cookie=None,
    )
    app = build_application(settings)

    token = secrets.token_hex(16)
    asin = "V2" + token[:8].upper()
    row = {
        "market_id": "ATVPDKIKX0DER",
        "asin": asin,
        "review_title": f"crawler-v2-probe-{token}",
        "review_text": f"isolated result sink roundtrip {token}",
        "imgs": [],
        "review_stars": "5.0 out of 5 stars",
        "review_date": "Reviewed in an isolated loopback test",
        "attributes": "fixture",
        "video": "",
        "crawl_date": datetime.now(UTC).date().isoformat(),
        "task_date": datetime.now(UTC).date().isoformat(),
    }
    job, _ = app.service.create_job(
        kind="reviews",
        inputs=[
            {
                "market_id": "ATVPDKIKX0DER",
                "asin": asin,
                "post_code": "10001",
                "add_date": row["task_date"],
            }
        ],
        options={"result_sinks": ["legacy_mysql", "legacy_redis"]},
        idempotency_key=f"loopback-result-sink:{token}",
    )
    item = app.store.claim_next("roundtrip-crawl-worker", 30)
    if item is None:
        raise RuntimeError("roundtrip item could not be claimed")
    app.store.complete_item(
        item,
        CrawlResult(
            data={"items": [row], "row_count": 1},
            schema_version="amazon.reviews.v1",
            evidence={"sha256": hashlib.sha256(token.encode("utf-8")).hexdigest()},
        ),
    )
    before = app.store.list_deliveries(job["id"])
    processed = asyncio.run(app.delivery_worker.run_until_idle())
    after = app.store.list_deliveries(job["id"])

    import redis
    import sqlalchemy

    redis_client = redis.Redis.from_url(
        runtime.result_redis_url,
        decode_responses=False,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    engine = sqlalchemy.create_engine(
        runtime.mysql_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    mysql_seen = False
    mysql_removed = False
    redis_seen = False
    redis_removed = False
    markers_removed = 0
    try:
        with engine.connect() as connection:
            mysql_seen = bool(
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM `amazon_reviews` "
                    "WHERE `market_id` = %s AND `asin` = %s AND `review_title` = %s",
                    (row["market_id"], row["asin"], row["review_title"]),
                ).scalar()
            )
        canonical_row = app.store.list_results(job["id"])[0]["data"]["items"][0]
        encoded = LegacyRedisResultBuffer._encode(canonical_row)
        redis_key = "amazon_reviews_items_buffer"
        redis_seen = redis_client.lpos(redis_key, encoded) is not None

        with engine.begin() as connection:
            deleted = connection.exec_driver_sql(
                "DELETE FROM `amazon_reviews` "
                "WHERE `market_id` = %s AND `asin` = %s AND `review_title` = %s",
                (row["market_id"], row["asin"], row["review_title"]),
            )
            mysql_removed = deleted.rowcount >= 1
        redis_removed = int(redis_client.lrem(redis_key, 0, encoded)) >= 1
        for delivery in after:
            if delivery["sink_name"] != "legacy_redis":
                continue
            marker_hash = hashlib.sha256(delivery["id"].encode("utf-8")).hexdigest()
            markers_removed += int(
                redis_client.delete(f"amazon_crawler_v2:delivery:{marker_hash}")
            )
    finally:
        redis_client.close()
        engine.dispose()

    report = {
        "schema_version": "amazon-crawler.result-sink-roundtrip.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "authorization": "explicit_confirm_loopback_write",
        "legacy_config": {
            "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "imports_executed": False,
            "values_disclosed": False,
            "target_scopes": runtime.target_scopes,
        },
        "canonical": {
            "job_status": app.store.get_job(job["id"])["status"],
            "result_count": len(app.store.list_results(job["id"])),
            "outbox_before": len(before),
            "delivery_worker_processed": processed,
            "delivery_statuses": sorted(
                f"{value['sink_name']}:{value['status']}" for value in after
            ),
        },
        "legacy_mysql": {
            "sentinel_observed": mysql_seen,
            "sentinel_removed": mysql_removed,
        },
        "legacy_redis": {
            "sentinel_observed": redis_seen,
            "sentinel_removed": redis_removed,
            "delivery_markers_removed": markers_removed,
        },
    }
    report["ok"] = bool(
        report["canonical"]["job_status"] == "succeeded"
        and report["canonical"]["result_count"] == 1
        and report["canonical"]["outbox_before"] == 2
        and report["canonical"]["delivery_statuses"]
        == ["legacy_mysql:delivered", "legacy_redis:delivered"]
        and mysql_seen
        and mysql_removed
        and redis_seen
        and redis_removed
        and markers_removed == 1
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    secret_candidates = {
        value
        for url in (runtime.mysql_url, runtime.result_redis_url)
        for value in (urlsplit(url).username, urlsplit(url).password)
        if value
    }
    if any(secret in rendered for secret in secret_candidates):
        raise RuntimeError("secret boundary check rejected the sink report")
    output.write_text(rendered + "\n", encoding="utf-8")
    receipt_path = None
    receipt_args = (
        args.controlled_receipt,
        args.run_id,
        args.authorization_reference,
    )
    if any(receipt_args) and not all(receipt_args):
        raise SystemExit(
            "--controlled-receipt, --run-id and --authorization-reference must be supplied together"
        )
    if all(receipt_args):
        receipt_path = args.controlled_receipt.resolve()
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "schema_version": "controlled-check-receipt.v1",
            "check": "legacy_bridge_roundtrip",
            "status": "passed" if report["ok"] else "failed",
            "environment": "isolated",
            "run_id": args.run_id,
            "authorization_reference": args.authorization_reference,
            "validated_at": report["generated_at"],
            "artifact_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        }
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "receipt": str(output),
                "controlled_receipt": str(receipt_path) if receipt_path else None,
                "deliveries": report["canonical"]["delivery_statuses"],
                "sentinels_removed": mysql_removed and redis_removed,
                "values_disclosed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
