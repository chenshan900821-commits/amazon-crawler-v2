#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from amazon_crawler.application.cookie_maintenance import CookieMaintenanceService
from amazon_crawler.config import Settings
from amazon_crawler.infra.cookie_harvester import (
    AmazonCookieHarvester,
    HarvesterPolicy,
    RedisCookieStore,
)
from amazon_crawler.infra.resources import (
    BrowserFingerprintProvider,
    LegacyRedisCookieProvider,
    ProxyExtractionClient,
    RotatingProxyProvider,
    StaticProxyProvider,
    redis_cookie_client_from_url,
)
from amazon_crawler.plugins.marketplaces import MARKETPLACE_COOKIE_ALIASES, MARKETPLACES
from scripts.compile_controlled_evidence import (
    ControlledEvidenceError,
    _scan_for_secret_material,
)
from scripts.runtime_source_fingerprint import runtime_source_sha256
from scripts.verify_parity_manifest import _validate_cookie_production_evidence


MINIMUM_TARGET_INTERVAL_SECONDS = 10.0
MAX_SCANNED_KEYS = 100_000
AUTHORIZATION_ENV = "CONTROLLED_COOKIE_PRODUCTION_AUTHORIZATION"


@dataclass(frozen=True, slots=True)
class CookieProductionTarget:
    pool: str
    marketplace: str
    postal_code: str

    @property
    def amazon_marketplace_id(self) -> str:
        return MARKETPLACES[self.marketplace].amazon_marketplace_id


class _RestrictedRedisClient:
    """Expose only newly created keys to the real consumer implementation."""

    def __init__(self, client: Any, allowed_keys: set[str]) -> None:
        self._client = client
        self._allowed_keys = tuple(sorted(allowed_keys))

    def scan(
        self, cursor: int, *, match: str, count: int
    ) -> tuple[int, list[str]]:
        if int(cursor) != 0:
            return 0, []
        return 0, [key for key in self._allowed_keys if fnmatch.fnmatchcase(key, match)]

    def mget(self, keys: list[str]) -> list[str | bytes | None]:
        if any(key not in self._allowed_keys for key in keys):
            raise RuntimeError("restricted Cookie consumer rejected an unknown key")
        return self._client.mget(keys)

    def get(self, key: str) -> str | bytes | None:
        if key not in self._allowed_keys:
            return None
        return self._client.get(key)


def _text(value: str | bytes) -> str:
    return value.decode("utf-8", errors="strict") if isinstance(value, bytes) else value


def _scan_group_keys(
    client: Any,
    *,
    marketplace_id: str,
    postal_code: str,
    max_scanned_keys: int = MAX_SCANNED_KEYS,
) -> set[str]:
    cursor: int | bytes | str = 0
    keys: set[str] = set()
    pattern = f"cookie:{marketplace_id}:{postal_code}:*"
    while True:
        cursor, batch = client.scan(int(cursor), match=pattern, count=500)
        for raw_key in batch:
            key = _text(raw_key)
            parts = key.split(":", 3)
            if (
                len(parts) == 4
                and parts[0] == "cookie"
                and parts[1] == marketplace_id
                and parts[2] == postal_code
            ):
                keys.add(key)
            if len(keys) > max_scanned_keys:
                raise ControlledEvidenceError(
                    "Cookie production evidence scan exceeded its bounded key limit"
                )
        if int(cursor) == 0:
            return keys


async def _new_cookie_is_consumer_visible(
    client: Any,
    *,
    new_keys: set[str],
    marketplace_id: str,
    postal_code: str,
) -> bool:
    provider = LegacyRedisCookieProvider(
        _RestrictedRedisClient(client, new_keys),
        refresh_seconds=1800,
        quarantine_seconds=900,
        marketplace_aliases=MARKETPLACE_COOKIE_ALIASES,
    )
    await provider.refresh()
    lease = await provider.acquire(marketplace_id, postal_code)
    return lease is not None and lease.marketplace_id == marketplace_id


async def collect_cookie_production_receipt(
    *,
    targets: list[CookieProductionTarget],
    harvesters: dict[str, AmazonCookieHarvester],
    clients: dict[str, Any],
    authorization_reference: str,
    project_root: Path,
    minimum_target_interval_seconds: float = 25.0,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    if [(target.pool, target.marketplace) for target in targets] != [
        ("default", "US"),
        ("overseas", "JP"),
    ]:
        raise ControlledEvidenceError(
            "Cookie production evidence requires exactly default/US then overseas/JP"
        )
    if len({target.postal_code for target in targets}) != 2:
        raise ControlledEvidenceError(
            "Cookie production evidence requires two distinct postal codes"
        )
    if minimum_target_interval_seconds < MINIMUM_TARGET_INTERVAL_SECONDS:
        raise ControlledEvidenceError(
            f"Cookie production target interval must be at least {MINIMUM_TARGET_INTERVAL_SECONDS:g} seconds"
        )
    if not authorization_reference.strip():
        raise ControlledEvidenceError("Cookie production authorization reference is required")
    if set(harvesters) < {"default", "overseas"} or set(clients) < {
        "default",
        "overseas",
    }:
        raise ControlledEvidenceError("default and overseas Cookie pools must be configured")
    if any(
        harvester.backend != "curl_cffi" or harvester.tls_impersonation is not True
        for harvester in harvesters.values()
    ):
        raise ControlledEvidenceError(
            "Cookie production evidence requires curl_cffi TLS impersonation"
        )

    operations: list[dict[str, Any]] = []
    previous_started: float | None = None
    for target in targets:
        if previous_started is not None:
            remaining = minimum_target_interval_seconds - (clock() - previous_started)
            if remaining > 0:
                await sleeper(remaining)
        previous_started = clock()
        client = clients[target.pool]
        before_keys = await asyncio.to_thread(
            _scan_group_keys,
            client,
            marketplace_id=target.amazon_marketplace_id,
            postal_code=target.postal_code,
        )
        target_count = len(before_keys) + 1
        maintenance = CookieMaintenanceService(
            {target.pool: harvesters[target.pool]},
            {
                target.pool: {
                    target.marketplace: {target.postal_code: target_count}
                }
            },
            interval_seconds=60,
        )
        reports = await maintenance.run_once()
        if len(reports) != 1 or reports[0].get("ok") is not True:
            raise ControlledEvidenceError(
                f"Cookie maintenance failed for {target.pool}/{target.marketplace}"
            )
        report = reports[0]
        created = report.get("created")
        rejected = report.get("rejected")
        if (
            isinstance(created, bool)
            or not isinstance(created, int)
            or created < 1
            or isinstance(rejected, bool)
            or not isinstance(rejected, int)
            or rejected < 0
        ):
            safe_codes = report.get("failure_codes")
            rendered_codes = (
                ",".join(
                    sorted(
                        {
                            str(value)
                            for value in safe_codes
                            if isinstance(value, str) and value
                        }
                    )
                )
                if isinstance(safe_codes, (list, tuple))
                else ""
            )
            suffix = f" ({rendered_codes})" if rendered_codes else ""
            raise ControlledEvidenceError(
                f"Cookie production did not create a new Cookie for "
                f"{target.pool}/{target.marketplace}{suffix}"
            )
        after_keys = await asyncio.to_thread(
            _scan_group_keys,
            client,
            marketplace_id=target.amazon_marketplace_id,
            postal_code=target.postal_code,
        )
        new_keys = after_keys - before_keys
        if len(after_keys) < len(before_keys) + created or len(new_keys) < created:
            raise ControlledEvidenceError(
                f"Cookie production count proof failed for {target.pool}/{target.marketplace}"
            )
        ttl_values = await asyncio.gather(
            *(asyncio.to_thread(client.ttl, key) for key in sorted(new_keys))
        )
        if not ttl_values or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 60
            for value in ttl_values
        ):
            raise ControlledEvidenceError(
                f"Cookie production TTL proof failed for {target.pool}/{target.marketplace}"
            )
        consumer_visible = await _new_cookie_is_consumer_visible(
            client,
            new_keys=new_keys,
            marketplace_id=target.amazon_marketplace_id,
            postal_code=target.postal_code,
        )
        if not consumer_visible:
            raise ControlledEvidenceError(
                f"new Cookie was not visible to the consumer for {target.pool}/{target.marketplace}"
            )
        operations.append(
            {
                "pool": target.pool,
                "marketplace_id": target.marketplace,
                "postal_code_hash": hashlib.sha256(
                    target.postal_code.encode("utf-8")
                ).hexdigest(),
                "available_before": len(before_keys),
                "created": created,
                "rejected": rejected,
                "available_after": len(after_keys),
                "ttl_seconds": min(ttl_values),
                "address_confirmed": True,
                "consumer_visible": True,
            }
        )

    receipt = {
        "schema_version": "controlled-cookie-production-evidence.v1",
        "capability": "cookie_production_scheduler",
        "status": "passed",
        "environment": "isolated",
        "validated_at": datetime.now(UTC).isoformat(),
        "authorization_reference": authorization_reference,
        "runtime": {
            "transport_backend": "curl_cffi",
            "tls_impersonation": True,
            "v2_source_sha256": runtime_source_sha256(project_root),
        },
        "checks": {
            "external_write_authorized": True,
            "address_confirmed": True,
            "ttl_verified": True,
            "consumer_visible": True,
            "cookie_values_redacted": True,
            "maintenance_run_once_passed": True,
        },
        "operations": operations,
    }
    _scan_for_secret_material(receipt)
    return receipt


def _atomic_write_outside_project(
    path: Path, payload: object, *, project_root: Path
) -> None:
    if path.is_symlink():
        raise ControlledEvidenceError(
            "refusing to replace a symlinked Cookie production receipt"
        )
    resolved = path.resolve()
    if resolved.is_relative_to(project_root.resolve()):
        raise ControlledEvidenceError(
            "raw Cookie production receipt must be written outside the project"
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{resolved.name}.", suffix=".tmp", dir=resolved.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary_path, resolved)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _build_resources(
    settings: Settings,
    *,
    max_attempts_per_cookie: int = 1,
) -> tuple[dict[str, AmazonCookieHarvester], dict[str, Any]]:
    if not 1 <= max_attempts_per_cookie <= 5:
        raise ControlledEvidenceError(
            "Cookie production attempts per Cookie must be between 1 and 5"
        )
    urls = {
        "default": settings.cookie_redis_url,
        "overseas": settings.cookie_redis_overseas_url,
    }
    if any(not value for value in urls.values()):
        raise ControlledEvidenceError(
            "default and overseas Cookie Redis URLs must be configured"
        )
    clients = {
        pool: redis_cookie_client_from_url(str(url)) for pool, url in urls.items()
    }
    proxy_provider: Any = StaticProxyProvider(settings.http_proxy)
    if settings.proxy_extract_url:
        proxy_provider = RotatingProxyProvider(
            ProxyExtractionClient(
                settings.proxy_extract_url,
                username=settings.proxy_username,
                password=settings.proxy_password,
            ),
            quarantine_seconds=settings.proxy_quarantine_seconds,
        )
    fingerprint_provider = BrowserFingerprintProvider()
    policy = HarvesterPolicy(
        ttl_seconds=settings.cookie_ttl_seconds,
        max_attempts_per_cookie=max_attempts_per_cookie,
        concurrency=1,
        timeout_seconds=settings.request_timeout_seconds,
        require_proxy=settings.cookie_harvest_require_proxy,
    )
    harvesters: dict[str, AmazonCookieHarvester] = {}
    for pool, client in clients.items():
        consumer = LegacyRedisCookieProvider(
            client,
            refresh_seconds=settings.cookie_refresh_seconds,
            quarantine_seconds=settings.cookie_quarantine_seconds,
            marketplace_aliases=MARKETPLACE_COOKIE_ALIASES,
        )
        harvesters[pool] = AmazonCookieHarvester(
            store=RedisCookieStore(client),
            proxy_provider=proxy_provider,
            fingerprint_provider=fingerprint_provider,
            consumer_provider=consumer,
            policy=policy,
            transport_backend="curl_cffi",
        )
    return harvesters, clients


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create one new US and one new JP Cookie through the production chain, "
            "then emit a redacted controlled evidence receipt. This sends Amazon "
            "requests and writes the selected Cookie Redis pools."
        )
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--us-postal-code", required=True)
    parser.add_argument("--jp-postal-code", required=True)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--minimum-target-interval-seconds", type=float, default=25.0)
    parser.add_argument("--max-attempts-per-cookie", type=int, default=1)
    parser.add_argument("--confirm-authorized-cookie-production", action="store_true")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    clients: dict[str, Any] = {}
    try:
        if not args.confirm_authorized_cookie_production:
            raise ControlledEvidenceError(
                "Cookie production requires explicit confirmation because it sends "
                "Cookie-bearing Amazon requests and writes Cookie Redis"
            )
        expected_authorization = os.getenv(AUTHORIZATION_ENV, "").strip()
        if (
            not expected_authorization
            or expected_authorization != args.authorization_reference.strip()
        ):
            raise ControlledEvidenceError(
                f"{AUTHORIZATION_ENV} must exactly match --authorization-reference"
            )
        settings = Settings.from_env(project_root)
        harvesters, clients = _build_resources(
            settings,
            max_attempts_per_cookie=args.max_attempts_per_cookie,
        )
        receipt = asyncio.run(
            collect_cookie_production_receipt(
                targets=[
                    CookieProductionTarget("default", "US", args.us_postal_code),
                    CookieProductionTarget("overseas", "JP", args.jp_postal_code),
                ],
                harvesters=harvesters,
                clients=clients,
                authorization_reference=expected_authorization,
                project_root=project_root,
                minimum_target_interval_seconds=args.minimum_target_interval_seconds,
            )
        )
        _atomic_write_outside_project(args.output, receipt, project_root=project_root)
        errors = _validate_cookie_production_evidence(args.output.resolve())
        if errors:
            raise ControlledEvidenceError(
                "generated Cookie production receipt failed validation: "
                + "; ".join(errors)
            )
    except (ControlledEvidenceError, RuntimeError, ValueError, OSError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "Cookie production evidence collection failed"
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
                        "message": "Cookie production evidence collection failed",
                    },
                },
                ensure_ascii=False,
            )
        )
        return 2
    finally:
        for client in clients.values():
            try:
                client.close()
            except Exception:
                pass
    print(
        json.dumps(
            {
                "ok": True,
                "operation_count": len(receipt["operations"]),
                "output": str(args.output.resolve()),
                "cookie_values_redacted": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
