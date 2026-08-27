#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from amazon_crawler.config import Settings
from amazon_crawler.plugins.marketplaces import MARKETPLACES
from scripts.compile_controlled_evidence import ControlledEvidenceError


MAX_SCANNED_KEYS = 100_000


def cookie_pool_coverage(
    client: Any,
    *,
    marketplace_id: str,
    scan_count: int = 500,
    max_scanned_keys: int = MAX_SCANNED_KEYS,
) -> dict[str, Any]:
    if scan_count < 1 or max_scanned_keys < 1:
        raise ControlledEvidenceError("cookie coverage scan limits must be positive")
    cursor: int | bytes | str = 0
    counts: Counter[str] = Counter()
    scanned = 0
    pattern = f"cookie:{marketplace_id}:*:*"
    while True:
        cursor, keys = client.scan(int(cursor), match=pattern, count=scan_count)
        for raw_key in keys:
            key = raw_key.decode("utf-8", errors="replace") if isinstance(raw_key, bytes) else str(raw_key)
            parts = key.split(":", 3)
            if len(parts) == 4 and parts[0] == "cookie" and parts[1] == marketplace_id:
                counts[parts[2]] += 1
            scanned += 1
            if scanned > max_scanned_keys:
                raise ControlledEvidenceError(
                    "cookie coverage scan exceeded its bounded key limit"
                )
        if int(cursor) == 0:
            break
    return {
        "schema_version": "cookie-pool-coverage.v1",
        "marketplace_id": marketplace_id,
        "group_count": len(counts),
        "cookie_count": sum(counts.values()),
        "groups": [
            {"postal_code": postal_code, "cookie_count": count}
            for postal_code, count in sorted(counts.items())
        ],
        "cookie_values_read": False,
        "cookie_identifiers_disclosed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report Cookie Redis marketplace/postal coverage from key names only. "
            "Cookie values, key identifiers, Redis URLs, and credentials are never read or printed."
        )
    )
    parser.add_argument("--pool", choices=("default", "overseas"), required=True)
    parser.add_argument("--marketplace", choices=sorted(MARKETPLACES), required=True)
    parser.add_argument("--confirm-external-read", action="store_true")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    client = None
    try:
        if not args.confirm_external_read:
            raise ControlledEvidenceError(
                "cookie coverage report requires explicit external-read confirmation"
            )
        settings = Settings.from_env(project_root)
        url = (
            settings.cookie_redis_url
            if args.pool == "default"
            else settings.cookie_redis_overseas_url
        )
        if not url:
            raise ControlledEvidenceError("selected Cookie Redis pool is not configured")
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("cookie coverage report requires redis") from exc
        client = redis.Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
        market = MARKETPLACES[args.marketplace]
        report = cookie_pool_coverage(
            client,
            marketplace_id=market.amazon_marketplace_id,
        )
        report["marketplace"] = args.marketplace
        report["pool"] = args.pool
    except (ControlledEvidenceError, RuntimeError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "cookie coverage report failed"
        )
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": message}},
                ensure_ascii=False,
            )
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": "Cookie Redis could not be read",
                    },
                },
                ensure_ascii=False,
            )
        )
        return 2
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
