from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable, Iterable, Protocol
from urllib.parse import quote, urlparse

import httpx

from amazon_crawler.domain.ports import CookieProvider, FingerprintProvider, ProxyProvider
from amazon_crawler.domain.resources import (
    CookieLease,
    FingerprintProfile,
    ProxyLease,
    RequestContext,
    ResourceHealth,
    ResourceOutcome,
    SecretText,
)


def _anonymous_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _cookie_header(raw: str) -> str:
    candidate = raw.strip()
    if not candidate:
        raise ValueError("cookie value is empty")
    if candidate.startswith("{"):
        parsed: object
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = ast.literal_eval(candidate)
        if not isinstance(parsed, dict):
            raise ValueError("cookie object must be a mapping")
        pairs = []
        for key, value in parsed.items():
            if not isinstance(key, str) or not isinstance(value, (str, int, float)):
                raise ValueError("cookie mapping contains unsupported values")
            if any(char in key for char in "\r\n;="):
                raise ValueError("cookie name contains unsafe characters")
            rendered = str(value)
            if any(char in rendered for char in "\r\n;"):
                raise ValueError("cookie value contains unsafe characters")
            pairs.append(f"{key}={rendered}")
        if not pairs:
            raise ValueError("cookie mapping is empty")
        return "; ".join(pairs)
    if "\r" in candidate or "\n" in candidate:
        raise ValueError("cookie header contains unsafe characters")
    return candidate


@dataclass(frozen=True, slots=True)
class _CookieEntry:
    redis_key: str
    marketplace_id: str
    postal_code: str
    lease_id: str
    cookie_header: SecretText


class RedisCookieClient(Protocol):
    def scan(
        self, cursor: int, *, match: str, count: int
    ) -> tuple[int | bytes | str, list[str | bytes]]: ...

    def mget(self, keys: list[str]) -> list[str | bytes | None]: ...

    def get(self, key: str) -> str | bytes | None: ...


class LegacyRedisCookieProvider:
    """Reads the legacy cookie key format without exposing cookie material."""

    def __init__(
        self,
        client: RedisCookieClient,
        *,
        refresh_seconds: float = 1800,
        quarantine_seconds: float = 900,
        scan_count: int = 1000,
        direct_miss_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        chooser: random.Random | random.SystemRandom | None = None,
        marketplace_aliases: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._refresh_seconds = max(1.0, refresh_seconds)
        self._quarantine_seconds = max(1.0, quarantine_seconds)
        self._scan_count = max(10, scan_count)
        self._direct_miss_seconds = max(0.1, direct_miss_seconds)
        self._clock = clock
        self._chooser = chooser or random.SystemRandom()
        self._marketplace_aliases = marketplace_aliases or {}
        self._cache: dict[tuple[str, str], tuple[_CookieEntry, ...]] = {}
        self._by_lease_id: dict[str, _CookieEntry] = {}
        self._quarantined_until: dict[str, float] = {}
        self._last_refresh_monotonic = 0.0
        self._last_refresh_at: datetime | None = None
        self._initialized = False
        self._refresh_lock = asyncio.Lock()
        self._direct_lookup_lock = asyncio.Lock()
        self._direct_miss_until: dict[tuple[str, str | None], float] = {}

    @staticmethod
    def _text(value: str | bytes) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else value

    def _scan_all(self, pattern: str) -> list[str]:
        cursor: int | bytes | str = 0
        keys: list[str] = []
        while True:
            cursor, batch = self._client.scan(
                int(cursor), match=pattern, count=self._scan_count
            )
            keys.extend(self._text(key) for key in batch)
            if int(cursor) == 0:
                return keys

    def _load_entries(self, pattern: str = "cookie:*") -> list[_CookieEntry]:
        keys = self._scan_all(pattern)
        values = self._client.mget(keys) if keys else []
        entries: list[_CookieEntry] = []
        for key, raw in zip(keys, values, strict=True):
            if raw is None:
                continue
            parts = key.split(":", 3)
            if len(parts) != 4 or parts[0] != "cookie":
                continue
            marketplace_id, postal_code = parts[1], parts[2]
            try:
                header = _cookie_header(self._text(raw))
            except (ValueError, SyntaxError):
                continue
            entries.append(
                _CookieEntry(
                    redis_key=key,
                    marketplace_id=marketplace_id,
                    postal_code=postal_code,
                    lease_id=_anonymous_id("cookie", key),
                    cookie_header=SecretText(header),
                )
            )
        return entries

    async def refresh(self) -> ResourceHealth:
        async with self._refresh_lock:
            entries = await asyncio.to_thread(self._load_entries)
            grouped: dict[tuple[str, str], list[_CookieEntry]] = {}
            for entry in entries:
                grouped.setdefault((entry.marketplace_id, entry.postal_code), []).append(entry)
            self._cache = {key: tuple(value) for key, value in grouped.items()}
            self._by_lease_id = {entry.lease_id: entry for entry in entries}
            self._last_refresh_monotonic = self._clock()
            self._last_refresh_at = datetime.now(UTC)
            self._initialized = True
            self._direct_miss_until.clear()
            self._remove_expired_quarantines()
        return await self.health()

    async def _ensure_fresh(self) -> None:
        now = self._clock()
        if self._initialized and now - self._last_refresh_monotonic < self._refresh_seconds:
            return
        async with self._refresh_lock:
            now = self._clock()
            if self._initialized and now - self._last_refresh_monotonic < self._refresh_seconds:
                return
            entries = await asyncio.to_thread(self._load_entries)
            grouped: dict[tuple[str, str], list[_CookieEntry]] = {}
            for entry in entries:
                grouped.setdefault((entry.marketplace_id, entry.postal_code), []).append(entry)
            self._cache = {key: tuple(value) for key, value in grouped.items()}
            self._by_lease_id = {entry.lease_id: entry for entry in entries}
            self._last_refresh_monotonic = now
            self._last_refresh_at = datetime.now(UTC)
            self._initialized = True
            self._direct_miss_until.clear()
            self._remove_expired_quarantines()

    def _remove_expired_quarantines(self) -> None:
        now = self._clock()
        self._quarantined_until = {
            lease_id: until
            for lease_id, until in self._quarantined_until.items()
            if until > now and lease_id in self._by_lease_id
        }

    def _eligible(self, marketplace_id: str, postal_code: str | None) -> list[_CookieEntry]:
        now = self._clock()
        groups: Iterable[tuple[_CookieEntry, ...]]
        if postal_code:
            groups = (self._cache.get((marketplace_id, postal_code), ()),)
        else:
            groups = (
                entries
                for (market, _), entries in self._cache.items()
                if market == marketplace_id
            )
        return [
            entry
            for entries in groups
            for entry in entries
            if self._quarantined_until.get(entry.lease_id, 0.0) <= now
        ]

    async def _direct_lookup(
        self, marketplace_id: str, postal_code: str | None
    ) -> _CookieEntry | None:
        miss_key = (marketplace_id, postal_code)
        now = self._clock()
        if self._direct_miss_until.get(miss_key, 0.0) > now:
            return None
        async with self._direct_lookup_lock:
            now = self._clock()
            if self._direct_miss_until.get(miss_key, 0.0) > now:
                return None
            pattern = f"cookie:{marketplace_id}:{postal_code or '*'}:*"
            entries = await asyncio.to_thread(self._load_entries, pattern)
            eligible = [
                entry
                for entry in entries
                if self._quarantined_until.get(entry.lease_id, 0.0) <= now
            ]
            if not eligible:
                self._direct_miss_until[miss_key] = now + self._direct_miss_seconds
                return None
            self._direct_miss_until.pop(miss_key, None)
            selected = self._chooser.choice(eligible)
            group = (selected.marketplace_id, selected.postal_code)
            existing = {entry.lease_id: entry for entry in self._cache.get(group, ())}
            existing[selected.lease_id] = selected
            self._cache[group] = tuple(existing.values())
            self._by_lease_id[selected.lease_id] = selected
            return selected

    async def acquire(
        self, marketplace_id: str, postal_code: str | None
    ) -> CookieLease | None:
        requested_market = marketplace_id.strip()
        market = self._marketplace_aliases.get(requested_market, requested_market)
        postal = postal_code.strip() if postal_code and postal_code.strip() else None
        await self._ensure_fresh()
        eligible = self._eligible(market, postal)
        entry = self._chooser.choice(eligible) if eligible else await self._direct_lookup(market, postal)
        if entry is None:
            return None
        return CookieLease(
            lease_id=entry.lease_id,
            marketplace_id=entry.marketplace_id,
            postal_code=entry.postal_code,
            cookie_header=entry.cookie_header,
            source="legacy_redis",
        )

    async def report(self, lease: CookieLease, outcome: ResourceOutcome) -> None:
        if lease.lease_id not in self._by_lease_id:
            return
        if outcome in {ResourceOutcome.BLOCKED, ResourceOutcome.AUTH_INVALID}:
            self._quarantined_until[lease.lease_id] = self._clock() + self._quarantine_seconds
        elif outcome is ResourceOutcome.SUCCESS:
            self._quarantined_until.pop(lease.lease_id, None)

    async def health(self) -> ResourceHealth:
        self._remove_expired_quarantines()
        now = self._clock()
        available = sum(
            1
            for lease_id in self._by_lease_id
            if self._quarantined_until.get(lease_id, 0.0) <= now
        )
        return ResourceHealth(
            provider="legacy_redis_cookie",
            available=available,
            quarantined=len(self._quarantined_until),
            groups=len(self._cache),
            last_refresh_at=self._last_refresh_at,
            stale=(
                self._last_refresh_at is None
                or now - self._last_refresh_monotonic >= self._refresh_seconds
            ),
        )


class StaticCookieProvider:
    def __init__(self, cookie: str | None) -> None:
        self._secret = SecretText(_cookie_header(cookie)) if cookie else None
        self._lease_id = _anonymous_id("cookie", cookie) if cookie else None

    async def acquire(
        self, marketplace_id: str, postal_code: str | None
    ) -> CookieLease | None:
        if self._secret is None or self._lease_id is None:
            return None
        return CookieLease(
            lease_id=self._lease_id,
            marketplace_id=marketplace_id,
            postal_code=postal_code,
            cookie_header=self._secret,
            source="static_runtime",
        )

    async def report(self, lease: CookieLease, outcome: ResourceOutcome) -> None:
        return None

    async def refresh(self) -> ResourceHealth:
        return await self.health()

    async def health(self) -> ResourceHealth:
        return ResourceHealth(
            provider="static_cookie",
            available=1 if self._secret else 0,
            quarantined=0,
            groups=1 if self._secret else 0,
            last_refresh_at=None,
            stale=False,
        )


class RoutedCookieProvider:
    def __init__(
        self,
        routes: dict[str, CookieProvider],
        default: CookieProvider,
    ) -> None:
        self._routes = routes
        self._default = default
        self._owners: dict[int, CookieProvider] = {}

    def _provider(self, marketplace_id: str) -> CookieProvider:
        return self._routes.get(marketplace_id, self._default)

    async def acquire(
        self, marketplace_id: str, postal_code: str | None
    ) -> CookieLease | None:
        provider = self._provider(marketplace_id)
        lease = await provider.acquire(marketplace_id, postal_code)
        if lease:
            self._owners[id(lease)] = provider
        return lease

    async def report(self, lease: CookieLease, outcome: ResourceOutcome) -> None:
        provider = self._owners.pop(id(lease), None)
        if provider:
            await provider.report(lease, outcome)

    def _providers(self) -> list[CookieProvider]:
        values = [self._default, *self._routes.values()]
        unique: list[CookieProvider] = []
        for provider in values:
            if all(provider is not existing for existing in unique):
                unique.append(provider)
        return unique

    async def refresh(self) -> ResourceHealth:
        await asyncio.gather(*(provider.refresh() for provider in self._providers()))
        return await self.health()

    async def health(self) -> ResourceHealth:
        reports = await asyncio.gather(*(provider.health() for provider in self._providers()))
        refreshed = [report.last_refresh_at for report in reports if report.last_refresh_at]
        return ResourceHealth(
            provider="routed_cookie",
            available=sum(report.available for report in reports),
            quarantined=sum(report.quarantined for report in reports),
            groups=sum(report.groups for report in reports),
            last_refresh_at=max(refreshed) if refreshed else None,
            stale=any(report.stale for report in reports),
        )


def redis_cookie_client_from_url(url: str) -> RedisCookieClient:
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError("Redis cookie support requires the redis package") from exc
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
    )


class StaticProxyProvider:
    def __init__(self, proxy_url: str | None) -> None:
        self._secret = SecretText(proxy_url) if proxy_url else None
        self._lease_id = _anonymous_id("proxy", proxy_url) if proxy_url else None

    async def acquire(self, purpose: str, marketplace_id: str) -> ProxyLease | None:
        if self._secret is None or self._lease_id is None:
            return None
        return ProxyLease(self._lease_id, self._secret, "static_runtime")

    async def report(self, lease: ProxyLease, outcome: ResourceOutcome) -> None:
        return None

    async def health(self) -> ResourceHealth:
        return ResourceHealth(
            provider="static_proxy",
            available=1 if self._secret else 0,
            quarantined=0,
            groups=1 if self._secret else 0,
            last_refresh_at=None,
            stale=False,
        )


ProxyLoader = Callable[[str, str], Awaitable[list[str]]]


class ProxyExtractionClient:
    def __init__(
        self,
        endpoint: str,
        *,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: float = 10,
        max_response_bytes: int = 100_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = SecretText(endpoint)
        self._username = SecretText(username) if username else None
        self._password = SecretText(password) if password else None
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    def _format(self, candidate: str) -> str | None:
        raw = candidate.strip()
        if not raw or any(char in raw for char in "\r\n"):
            return None
        parsed = urlparse(raw if "://" in raw else f"//{raw}")
        if not parsed.hostname or parsed.port is None:
            return None
        scheme = parsed.scheme or "http"
        if scheme not in {"http", "https"}:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        authority = f"{host}:{parsed.port}"
        if self._username and self._password:
            user = quote(self._username.reveal(), safe="")
            password = quote(self._password.reveal(), safe="")
            authority = f"{user}:{password}@{authority}"
        return f"{scheme}://{authority}"

    async def __call__(self, purpose: str, marketplace_id: str) -> list[str]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.get(self._endpoint.reveal())
        except httpx.HTTPError as exc:
            raise RuntimeError("proxy extraction service request failed") from exc
        if response.is_error:
            raise RuntimeError("proxy extraction service returned an error")
        if len(response.content) > self._max_response_bytes:
            raise RuntimeError("proxy extraction response is too large")
        content_type = response.headers.get("content-type", "").lower()
        body = response.text.strip()
        if "html" in content_type or body.startswith("<"):
            raise RuntimeError("proxy extraction response has an unsupported format")
        proxies = [self._format(line) for line in body.replace("\r", "").split("\n")]
        valid = [proxy for proxy in proxies if proxy]
        if not valid:
            raise RuntimeError("proxy extraction service returned no valid proxies")
        return valid


class RotatingProxyProvider:
    def __init__(
        self,
        loader: ProxyLoader,
        *,
        quarantine_seconds: float = 300,
        load_failure_cooldown_seconds: float = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._quarantine_seconds = max(1.0, quarantine_seconds)
        self._load_failure_cooldown_seconds = max(
            0.1, load_failure_cooldown_seconds
        )
        self._clock = clock
        self._queue: deque[str] = deque()
        self._by_id: dict[str, str] = {}
        self._quarantined_until: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._last_refresh_at: datetime | None = None
        self._load_retry_after = 0.0

    async def _load_if_empty(self, purpose: str, marketplace_id: str) -> None:
        now = self._clock()
        if any(self._quarantined_until.get(_anonymous_id("proxy", url), 0.0) <= now for url in self._queue):
            return
        if self._load_retry_after > now:
            raise RuntimeError("proxy pool refresh is cooling down")
        async with self._lock:
            now = self._clock()
            if any(
                self._quarantined_until.get(_anonymous_id("proxy", url), 0.0) <= now
                for url in self._queue
            ):
                return
            if self._load_retry_after > now:
                raise RuntimeError("proxy pool refresh is cooling down")
            try:
                loaded = await self._loader(purpose, marketplace_id)
            except Exception:
                self._load_retry_after = (
                    self._clock() + self._load_failure_cooldown_seconds
                )
                raise
            accepted = 0
            for url in loaded:
                candidate = url.strip()
                if not candidate or "\r" in candidate or "\n" in candidate:
                    continue
                lease_id = _anonymous_id("proxy", candidate)
                self._by_id[lease_id] = candidate
                if candidate not in self._queue:
                    self._queue.append(candidate)
                accepted += 1
            if accepted == 0:
                self._load_retry_after = (
                    self._clock() + self._load_failure_cooldown_seconds
                )
                raise RuntimeError("proxy extraction service returned no usable proxies")
            self._load_retry_after = 0.0
            self._last_refresh_at = datetime.now(UTC)

    async def acquire(self, purpose: str, marketplace_id: str) -> ProxyLease | None:
        await self._load_if_empty(purpose, marketplace_id)
        now = self._clock()
        for _ in range(len(self._queue)):
            url = self._queue[0]
            self._queue.rotate(-1)
            lease_id = _anonymous_id("proxy", url)
            if self._quarantined_until.get(lease_id, 0.0) <= now:
                return ProxyLease(lease_id, SecretText(url), "dynamic")
        return None

    async def report(self, lease: ProxyLease, outcome: ResourceOutcome) -> None:
        if lease.lease_id not in self._by_id:
            return
        if outcome in {ResourceOutcome.PROXY_ERROR, ResourceOutcome.BLOCKED}:
            self._quarantined_until[lease.lease_id] = self._clock() + self._quarantine_seconds
        elif outcome is ResourceOutcome.SUCCESS:
            self._quarantined_until.pop(lease.lease_id, None)

    async def health(self) -> ResourceHealth:
        now = self._clock()
        quarantined = sum(1 for until in self._quarantined_until.values() if until > now)
        return ResourceHealth(
            provider="rotating_proxy",
            available=max(0, len(self._by_id) - quarantined),
            quarantined=quarantined,
            groups=1 if self._by_id else 0,
            last_refresh_at=self._last_refresh_at,
            stale=not self._by_id,
        )


class BrowserFingerprintProvider:
    def __init__(self, profiles: list[FingerprintProfile] | None = None) -> None:
        self._profiles = profiles or [
            FingerprintProfile(
                "chrome_windows",
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "sec-ch-ua-platform": '"Windows"',
                    "Accept-Language": "en-US,en;q=0.8",
                },
                impersonate="chrome131",
            ),
            FingerprintProfile(
                "chrome_macos",
                {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "sec-ch-ua-platform": '"macOS"',
                    "Accept-Language": "en-US,en;q=0.8",
                },
                impersonate="chrome131",
            ),
        ]
        if not self._profiles:
            raise ValueError("at least one fingerprint profile is required")
        self._chooser = random.SystemRandom()

    async def acquire(self, purpose: str, marketplace_id: str) -> FingerprintProfile:
        return self._chooser.choice(self._profiles)


class RequestContextFactory:
    def __init__(
        self,
        cookie_provider: CookieProvider,
        proxy_provider: ProxyProvider,
        fingerprint_provider: FingerprintProvider,
        cookie_providers_by_purpose: dict[str, CookieProvider] | None = None,
    ) -> None:
        self.cookie_provider = cookie_provider
        self.proxy_provider = proxy_provider
        self.fingerprint_provider = fingerprint_provider
        self.cookie_providers_by_purpose = cookie_providers_by_purpose or {}
        self._cookie_owners: dict[int, CookieProvider] = {}

    def _cookie_provider_for(self, purpose: str) -> CookieProvider:
        return self.cookie_providers_by_purpose.get(purpose, self.cookie_provider)

    async def acquire(
        self, *, purpose: str, marketplace_id: str, postal_code: str | None
    ) -> RequestContext:
        cookie_provider = self._cookie_provider_for(purpose)
        cookie_result, proxy_result, fingerprint_result = await asyncio.gather(
            cookie_provider.acquire(marketplace_id, postal_code),
            self.proxy_provider.acquire(purpose, marketplace_id),
            self.fingerprint_provider.acquire(purpose, marketplace_id),
            return_exceptions=True,
        )
        failures = [
            value
            for value in (cookie_result, proxy_result, fingerprint_result)
            if isinstance(value, BaseException)
        ]
        if failures:
            cleanup = []
            if isinstance(cookie_result, CookieLease):
                cleanup.append(
                    cookie_provider.report(cookie_result, ResourceOutcome.PARSE_ERROR)
                )
            if isinstance(proxy_result, ProxyLease):
                cleanup.append(
                    self.proxy_provider.report(
                        proxy_result, ResourceOutcome.PARSE_ERROR
                    )
                )
            if cleanup:
                await asyncio.gather(*cleanup, return_exceptions=True)
            primary = failures[0]
            if not isinstance(primary, Exception):
                raise primary
            raise RuntimeError("request resource acquisition failed") from primary

        cookie = cookie_result if isinstance(cookie_result, CookieLease) else None
        proxy = proxy_result if isinstance(proxy_result, ProxyLease) else None
        if not isinstance(fingerprint_result, FingerprintProfile):
            raise RuntimeError("fingerprint provider returned an invalid profile")
        fingerprint = fingerprint_result
        if cookie:
            self._cookie_owners[id(cookie)] = cookie_provider
        return RequestContext(cookie=cookie, proxy=proxy, fingerprint=fingerprint)

    async def report(self, context: RequestContext, outcome: ResourceOutcome) -> None:
        reports = []
        if context.cookie:
            provider = self._cookie_owners.pop(id(context.cookie), self.cookie_provider)
            reports.append(provider.report(context.cookie, outcome))
        if context.proxy:
            reports.append(self.proxy_provider.report(context.proxy, outcome))
        if reports:
            await asyncio.gather(*reports)

    async def cookie_route_health(self) -> dict[str, dict[str, str | int | bool | None]]:
        providers = {"default": self.cookie_provider, **self.cookie_providers_by_purpose}
        reports = await asyncio.gather(*(provider.health() for provider in providers.values()))
        return {
            route: report.as_public_dict()
            for route, report in zip(providers, reports, strict=True)
        }
