from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    evidence_dir: Path
    result_jsonl_dir: Path
    capture_evidence: bool
    worker_enabled: bool
    worker_concurrency: int
    poll_seconds: float
    lease_seconds: int
    request_timeout_seconds: float
    min_host_interval_seconds: float
    max_response_bytes: int
    user_agent: str
    http_transport: str
    http_proxy: str | None = field(repr=False)
    amazon_cookie: str | None = field(repr=False)
    merchant_cookie: str | None = field(repr=False)
    require_cookie: bool
    cookie_redis_url: str | None = field(repr=False)
    cookie_redis_jp_url: str | None = field(repr=False)
    cookie_redis_overseas_url: str | None = field(repr=False)
    cookie_refresh_seconds: float
    cookie_quarantine_seconds: float
    cookie_harvest_concurrency: int
    cookie_harvest_max_attempts: int
    cookie_ttl_seconds: int
    cookie_harvest_require_proxy: bool
    cookie_maintenance_enabled: bool
    cookie_targets_path: Path
    cookie_maintenance_interval_seconds: float
    proxy_extract_url: str | None = field(repr=False)
    proxy_username: str | None = field(repr=False)
    proxy_password: str | None = field(repr=False)
    proxy_quarantine_seconds: float
    legacy_mysql_url: str | None = field(repr=False)
    legacy_result_redis_url: str | None = field(repr=False)
    delivery_worker_enabled: bool
    delivery_worker_concurrency: int
    delivery_poll_seconds: float
    delivery_lease_seconds: int
    delivery_max_attempts: int
    legacy_config_loaded: bool
    legacy_target_scopes: dict[str, str]

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "Settings":
        root = project_root or Path.cwd()

        def path_env(name: str, default: str) -> Path:
            value = Path(os.getenv(name, default)).expanduser()
            return value if value.is_absolute() else (root / value).resolve()

        http_transport = os.getenv("CRAWLER_HTTP_TRANSPORT", "auto").strip().lower()
        if http_transport not in {"auto", "curl_cffi", "httpx"}:
            raise ValueError("CRAWLER_HTTP_TRANSPORT must be auto, curl_cffi, or httpx")

        legacy = None
        if _as_bool(os.getenv("CRAWLER_USE_LEGACY_CONFIG"), False):
            from amazon_crawler.infra.legacy_runtime_config import (
                load_legacy_runtime_config,
            )

            configured_path = os.getenv("CRAWLER_LEGACY_CONFIG_PATH")
            if configured_path:
                legacy_path = Path(configured_path).expanduser()
                if not legacy_path.is_absolute():
                    legacy_path = (root / legacy_path).resolve()
            else:
                candidates = [
                    root / "settings" / "config.py",
                    root.parent / "settings" / "config.py",
                ]
                legacy_path = next((path for path in candidates if path.is_file()), candidates[0])
            legacy = load_legacy_runtime_config(
                legacy_path,
                allow_external_cookie_read=_as_bool(
                    os.getenv("CRAWLER_ALLOW_EXTERNAL_COOKIE_READ"), False
                ),
                allow_external_proxy_api=_as_bool(
                    os.getenv("CRAWLER_ALLOW_EXTERNAL_PROXY_API"), False
                ),
                proxy_route=os.getenv("CRAWLER_LEGACY_PROXY_ROUTE", "qg"),
            )

        def configured(name: str, fallback: str | None = None) -> str | None:
            return os.getenv(name) or fallback or None

        return cls(
            db_path=path_env("CRAWLER_DB_PATH", ".data/crawler.db"),
            evidence_dir=path_env("CRAWLER_EVIDENCE_DIR", ".data/evidence"),
            result_jsonl_dir=path_env(
                "CRAWLER_RESULT_JSONL_DIR", ".data/result-sinks/jsonl"
            ),
            capture_evidence=_as_bool(os.getenv("CRAWLER_CAPTURE_EVIDENCE"), False),
            worker_enabled=_as_bool(os.getenv("CRAWLER_WORKER_ENABLED"), True),
            worker_concurrency=max(1, int(os.getenv("CRAWLER_WORKER_CONCURRENCY", "2"))),
            poll_seconds=max(0.1, float(os.getenv("CRAWLER_POLL_SECONDS", "1.0"))),
            lease_seconds=max(15, int(os.getenv("CRAWLER_LEASE_SECONDS", "90"))),
            request_timeout_seconds=max(
                3.0, float(os.getenv("CRAWLER_REQUEST_TIMEOUT_SECONDS", "25"))
            ),
            min_host_interval_seconds=max(
                0.0, float(os.getenv("CRAWLER_MIN_HOST_INTERVAL_SECONDS", "1.5"))
            ),
            max_response_bytes=max(
                100_000, int(os.getenv("CRAWLER_MAX_RESPONSE_BYTES", "5000000"))
            ),
            user_agent=os.getenv(
                "CRAWLER_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36",
            ),
            http_transport=http_transport,
            http_proxy=os.getenv("CRAWLER_HTTP_PROXY") or None,
            amazon_cookie=os.getenv("CRAWLER_AMAZON_COOKIE") or None,
            merchant_cookie=configured(
                "CRAWLER_MERCHANT_COOKIE",
                legacy.merchant_cookie if legacy else None,
            ),
            require_cookie=_as_bool(os.getenv("CRAWLER_REQUIRE_COOKIE"), True),
            cookie_redis_url=configured(
                "CRAWLER_COOKIE_REDIS_URL",
                legacy.cookie_redis_url if legacy else None,
            ),
            cookie_redis_jp_url=configured(
                "CRAWLER_COOKIE_REDIS_JP_URL",
                legacy.cookie_redis_overseas_url if legacy else None,
            ),
            cookie_redis_overseas_url=(
                os.getenv("CRAWLER_COOKIE_REDIS_OVERSEAS_URL")
                or os.getenv("CRAWLER_COOKIE_REDIS_JP_URL")
                or (legacy.cookie_redis_overseas_url if legacy else None)
                or None
            ),
            cookie_refresh_seconds=max(
                10.0, float(os.getenv("CRAWLER_COOKIE_REFRESH_SECONDS", "1800"))
            ),
            cookie_quarantine_seconds=max(
                10.0, float(os.getenv("CRAWLER_COOKIE_QUARANTINE_SECONDS", "900"))
            ),
            cookie_harvest_concurrency=max(
                1, int(os.getenv("CRAWLER_COOKIE_HARVEST_CONCURRENCY", "2"))
            ),
            cookie_harvest_max_attempts=max(
                1, int(os.getenv("CRAWLER_COOKIE_HARVEST_MAX_ATTEMPTS", "4"))
            ),
            cookie_ttl_seconds=max(
                60, int(os.getenv("CRAWLER_COOKIE_TTL_SECONDS", "172800"))
            ),
            cookie_harvest_require_proxy=_as_bool(
                os.getenv("CRAWLER_COOKIE_HARVEST_REQUIRE_PROXY"), True
            ),
            cookie_maintenance_enabled=_as_bool(
                os.getenv("CRAWLER_COOKIE_MAINTENANCE_ENABLED"), False
            ),
            cookie_targets_path=path_env(
                "CRAWLER_COOKIE_TARGETS_PATH", ".data/cookie_targets.json"
            ),
            cookie_maintenance_interval_seconds=max(
                60.0,
                float(os.getenv("CRAWLER_COOKIE_MAINTENANCE_INTERVAL_SECONDS", "1800")),
            ),
            proxy_extract_url=configured(
                "CRAWLER_PROXY_EXTRACT_URL",
                legacy.proxy_extract_url if legacy else None,
            ),
            proxy_username=configured(
                "CRAWLER_PROXY_USERNAME",
                legacy.proxy_username if legacy else None,
            ),
            proxy_password=configured(
                "CRAWLER_PROXY_PASSWORD",
                legacy.proxy_password if legacy else None,
            ),
            proxy_quarantine_seconds=max(
                10.0, float(os.getenv("CRAWLER_PROXY_QUARANTINE_SECONDS", "300"))
            ),
            legacy_mysql_url=configured(
                "CRAWLER_LEGACY_MYSQL_URL",
                legacy.mysql_url if legacy else None,
            ),
            legacy_result_redis_url=configured(
                "CRAWLER_LEGACY_RESULT_REDIS_URL",
                legacy.result_redis_url if legacy else None,
            ),
            delivery_worker_enabled=_as_bool(
                os.getenv("CRAWLER_DELIVERY_WORKER_ENABLED"), True
            ),
            delivery_worker_concurrency=max(
                1, int(os.getenv("CRAWLER_DELIVERY_WORKER_CONCURRENCY", "2"))
            ),
            delivery_poll_seconds=max(
                0.1, float(os.getenv("CRAWLER_DELIVERY_POLL_SECONDS", "1.0"))
            ),
            delivery_lease_seconds=max(
                15, int(os.getenv("CRAWLER_DELIVERY_LEASE_SECONDS", "90"))
            ),
            delivery_max_attempts=max(
                1, int(os.getenv("CRAWLER_DELIVERY_MAX_ATTEMPTS", "8"))
            ),
            legacy_config_loaded=legacy is not None,
            legacy_target_scopes=dict(legacy.target_scopes) if legacy else {},
        )


def _normalize_cookie_target_group(
    payload: object,
) -> dict[str, dict[str, int]]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("cookie target group must be a non-empty object")
    normalized: dict[str, dict[str, int]] = {}
    for marketplace_id, postal_targets in payload.items():
        if not isinstance(marketplace_id, str) or not isinstance(postal_targets, dict):
            raise ValueError("cookie target marketplace entries must be objects")
        normalized[marketplace_id.upper()] = {}
        for postal_code, target in postal_targets.items():
            if not isinstance(postal_code, str) or not isinstance(target, int) or target < 0:
                raise ValueError(
                    "cookie targets require string postal codes and non-negative counts"
                )
            normalized[marketplace_id.upper()][postal_code] = target
    return normalized


def load_cookie_targets(path: Path) -> dict[str, dict[str, dict[str, int]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError("cookie targets must be a non-empty object")
    pool_names = {"default", "overseas"}
    if set(payload).issubset(pool_names):
        return {
            pool_name: _normalize_cookie_target_group(targets)
            for pool_name, targets in payload.items()
        }
    return {"default": _normalize_cookie_target_group(payload)}
