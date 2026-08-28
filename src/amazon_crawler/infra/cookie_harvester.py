from __future__ import annotations

import asyncio
import inspect
import html as html_module
import json
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Protocol
from urllib.parse import urljoin, urlparse

import httpx

from amazon_crawler.domain.ports import CookieProvider, FingerprintProvider, ProxyProvider
from amazon_crawler.domain.resources import HarvestReport, ResourceOutcome
from amazon_crawler.plugins.amazon_parser import looks_blocked
from amazon_crawler.plugins.marketplaces import MARKETPLACES


LOCALE_COOKIES: dict[str, tuple[str, str] | None] = {
    "US": ("lc-main", "en_US"),
    "CA": ("lc-acbca", "en_CA"),
    "UK": None,
    "DE": ("lc-acbde", "en_GB"),
    "FR": ("lc-acbfr", "en_GB"),
    "IT": ("lc-acbit", "en_GB"),
    "ES": None,
    "NL": ("lc-acbnl", "en_GB"),
    "SE": ("lc-acbse", "en_GB"),
    "PL": None,
    "BE": ("lc-acbbe", "en_GB"),
    "IE": ("lc-acbie", "en_IE"),
    "EG": ("lc-acbeg", "en_AE"),
    "JP": ("lc-acbjp", "en_US"),
    "AE": ("lc-acbae", "en_AE"),
    "SA": ("lc-acbsa", "en_AE"),
    "IN": ("lc-acbin", "en_US"),
}

CURRENCIES = {
    "US": "USD",
    "CA": "CAD",
    "MX": "MXN",
    "BR": "BRL",
    "UK": "GBP",
    "DE": "EUR",
    "FR": "EUR",
    "IT": "EUR",
    "ES": "EUR",
    "NL": "EUR",
    "SE": "SEK",
    "PL": "PLN",
    "BE": "EUR",
    "IE": "EUR",
    "ZA": "ZAR",
    "EG": "EGP",
    "JP": "JPY",
    "AU": "AUD",
    "IN": "INR",
    "SG": "SGD",
    "AE": "AED",
    "SA": "SAR",
    "TR": "TRY",
}

UBID_COOKIE_NAMES = {
    "US": "ubid-main",
    "CA": "ubid-acbca",
    "MX": "ubid-acbmx",
    "BR": "ubid-acbbr",
    "UK": "ubid-acbuk",
    "DE": "ubid-acbde",
    "FR": "ubid-acbfr",
    "IT": "ubid-acbit",
    "ES": "ubid-acbes",
    "NL": "ubid-acbnl",
    "SE": "ubid-acbse",
    "PL": "ubid-acbpl",
    "BE": "ubid-acbbe",
    "IE": "ubid-acbie",
    "ZA": "ubid-acbza",
    "EG": "ubid-acbeg",
    "JP": "ubid-acbjp",
    "AU": "ubid-acbau",
    "IN": "ubid-acbin",
    "SG": "ubid-acbsg",
    "AE": "ubid-acbae",
    "SA": "ubid-acbsa",
    "TR": "ubid-acbtr",
}


class CookieStore(Protocol):
    async def count(self, marketplace_id: str, postal_code: str) -> int: ...

    async def save(
        self,
        marketplace_id: str,
        postal_code: str,
        cookies: dict[str, str],
        ttl_seconds: int,
    ) -> None: ...


class RedisCookieWriter(Protocol):
    def scan(
        self, cursor: int, *, match: str, count: int
    ) -> tuple[int | bytes | str, list[str | bytes]]: ...

    def setex(self, key: str, ttl_seconds: int, value: str) -> object: ...


class RedisCookieStore:
    def __init__(self, client: RedisCookieWriter, *, scan_count: int = 1000) -> None:
        self._client = client
        self._scan_count = max(10, scan_count)

    def _count_sync(self, marketplace_id: str, postal_code: str) -> int:
        cursor: int | bytes | str = 0
        total = 0
        pattern = f"cookie:{marketplace_id}:{postal_code}:*"
        while True:
            cursor, keys = self._client.scan(
                int(cursor), match=pattern, count=self._scan_count
            )
            total += len(keys)
            if int(cursor) == 0:
                return total

    async def count(self, marketplace_id: str, postal_code: str) -> int:
        try:
            return await asyncio.to_thread(
                self._count_sync, marketplace_id, postal_code
            )
        except Exception as exc:
            raise RuntimeError("cookie Redis count failed") from exc

    async def save(
        self,
        marketplace_id: str,
        postal_code: str,
        cookies: dict[str, str],
        ttl_seconds: int,
    ) -> None:
        safe_cookies = {
            str(key): str(value)
            for key, value in cookies.items()
            if key and value and key != "sp-cdn"
        }
        if not safe_cookies:
            raise ValueError("refusing to store an empty cookie set")
        timestamp_ms = int(time.time() * 1000)
        suffix = uuid.uuid4().hex[:12]
        key = f"cookie:{marketplace_id}:{postal_code}:{timestamp_ms}-{suffix}"
        value = json.dumps(safe_cookies, ensure_ascii=True, sort_keys=True)
        try:
            await asyncio.to_thread(self._client.setex, key, ttl_seconds, value)
        except Exception as exc:
            raise RuntimeError("cookie Redis write failed") from exc


class RoutedCookieStore:
    def __init__(self, routes: dict[str, CookieStore], default: CookieStore) -> None:
        self._routes = routes
        self._default = default

    def _store(self, marketplace_id: str) -> CookieStore:
        return self._routes.get(marketplace_id, self._default)

    async def count(self, marketplace_id: str, postal_code: str) -> int:
        return await self._store(marketplace_id).count(marketplace_id, postal_code)

    async def save(
        self,
        marketplace_id: str,
        postal_code: str,
        cookies: dict[str, str],
        ttl_seconds: int,
    ) -> None:
        await self._store(marketplace_id).save(
            marketplace_id, postal_code, cookies, ttl_seconds
        )


class CookieHarvestError(RuntimeError):
    def __init__(self, code: str, outcome: ResourceOutcome) -> None:
        super().__init__(code)
        self.code = code
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class HarvesterPolicy:
    ttl_seconds: int = 172800
    max_attempts_per_cookie: int = 4
    concurrency: int = 2
    timeout_seconds: float = 30
    require_proxy: bool = False


class AmazonCookieHarvester:
    def __init__(
        self,
        *,
        store: CookieStore,
        proxy_provider: ProxyProvider,
        fingerprint_provider: FingerprintProvider,
        consumer_provider: CookieProvider | None = None,
        policy: HarvesterPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        transport_backend: str = "auto",
        curl_session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._store = store
        self._proxy_provider = proxy_provider
        self._fingerprint_provider = fingerprint_provider
        self._consumer_provider = consumer_provider
        self._policy = policy or HarvesterPolicy()
        self._transport = transport
        self._curl_session_factory = curl_session_factory
        selected = transport_backend.strip().lower()
        if selected not in {"auto", "curl_cffi", "httpx"}:
            raise ValueError("Cookie transport must be auto, curl_cffi, or httpx")
        if transport is not None:
            selected = "httpx"
        if selected in {"auto", "curl_cffi"} and curl_session_factory is None:
            try:
                from curl_cffi.requests import AsyncSession
            except ImportError:
                if selected == "curl_cffi":
                    raise RuntimeError(
                        "curl_cffi Cookie transport was requested but is not installed"
                    ) from None
            else:
                self._curl_session_factory = AsyncSession
        self.backend = "curl_cffi" if self._curl_session_factory else "httpx"
        self.tls_impersonation = self.backend == "curl_cffi"
        self._semaphore = asyncio.Semaphore(max(1, self._policy.concurrency))

    @staticmethod
    def _session_id() -> str:
        return (
            f"{100 + secrets.randbelow(500)}-"
            f"{secrets.randbelow(10_000_000):07d}-"
            f"{secrets.randbelow(10_000_000):07d}"
        )

    @staticmethod
    def _validation_token(html: str) -> str | None:
        match = re.search(
            r'id=["\'](?:glowValidationToken|glow-validation-token)["\'][^>]*value=["\']([^"\']+)',
            html,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)
        match = re.search(
            r'value=["\']([^"\']+)["\'][^>]*id=["\'](?:glowValidationToken|glow-validation-token)["\']',
            html,
            re.IGNORECASE,
        )
        return match.group(1) if match else None

    @staticmethod
    def _csrf_token(text: str) -> str | None:
        match = re.search(r'CSRF_TOKEN\s*:\s*["\']([^"\']+)', text)
        return match.group(1) if match else None

    @staticmethod
    def _address_matches(text: str, postal_code: str) -> bool:
        """Verify the new session renders the requested delivery location."""

        candidates = re.findall(
            r'id=["\'](?:glow-ingress-line2|glow-ingress-block)["\'][^>]*>(.*?)</(?:span|div)>',
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        expected = re.sub(r"[^0-9a-z]", "", postal_code.casefold())
        if not expected:
            return False
        for candidate in candidates:
            visible = re.sub(r"<[^>]+>", " ", candidate)
            normalized = re.sub(
                r"[^0-9a-z]", "", html_module.unescape(visible).casefold()
            )
            if expected in normalized:
                return True
        return False

    @staticmethod
    def _cookie_dict(client: Any) -> dict[str, str]:
        get_dict = getattr(client.cookies, "get_dict", None)
        if callable(get_dict):
            return {str(key): str(value) for key, value in get_dict().items()}
        values: dict[str, str] = {}
        for cookie in client.cookies.jar:
            values[cookie.name] = cookie.value
        return values

    @asynccontextmanager
    async def _session(
        self,
        *,
        headers: dict[str, str],
        proxy_url: str | None,
        impersonate: str | None,
    ) -> AsyncIterator[Any]:
        if self.backend == "curl_cffi":
            if not impersonate:
                raise RuntimeError("Cookie TLS transport requires an impersonation profile")
            kwargs: dict[str, Any] = {
                "headers": headers,
                "cookies": {"session-id": self._session_id()},
                "timeout": self._policy.timeout_seconds,
                "allow_redirects": False,
                "impersonate": impersonate,
            }
            if proxy_url:
                kwargs["proxy"] = proxy_url
            client = self._curl_session_factory(**kwargs)
        else:
            client = httpx.AsyncClient(
                headers=headers,
                cookies={"session-id": self._session_id()},
                timeout=self._policy.timeout_seconds,
                follow_redirects=False,
                proxy=proxy_url,
                transport=self._transport,
            )
        try:
            yield client
        finally:
            closer = getattr(client, "aclose", None) or getattr(client, "close", None)
            if closer:
                result = closer()
                if inspect.isawaitable(result):
                    await result

    async def _request(
        self,
        client: Any,
        method: str,
        url: str,
        **kwargs: object,
    ) -> httpx.Response:
        request_method = method.upper()
        request_url = url
        request_kwargs = dict(kwargs)
        initial_host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        redirect_count = 0
        while True:
            try:
                response = await client.request(
                    request_method,
                    request_url,
                    **request_kwargs,
                )
            except Exception as exc:
                raise CookieHarvestError(
                    "network_error", ResourceOutcome.PROXY_ERROR
                ) from exc
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("location")
            if not location:
                break
            target = urljoin(str(getattr(response, "url", request_url)), location)
            parsed_target = urlparse(target)
            target_host = (parsed_target.hostname or "").lower().removeprefix("www.")
            if parsed_target.scheme != "https" or target_host != initial_host:
                raise CookieHarvestError(
                    "redirect_host_not_allowed", ResourceOutcome.BLOCKED
                )
            redirect_count += 1
            if redirect_count > 5:
                raise CookieHarvestError(
                    "redirect_limit_exceeded", ResourceOutcome.NETWORK_ERROR
                )
            if response.status_code == 303 or (
                response.status_code in {301, 302} and request_method == "POST"
            ):
                request_method = "GET"
                request_kwargs.pop("json", None)
                request_headers = request_kwargs.get("headers")
                if isinstance(request_headers, dict):
                    request_kwargs["headers"] = {
                        key: value
                        for key, value in request_headers.items()
                        if str(key).lower() != "content-type"
                    }
            request_url = target
        if response.status_code in {403, 429} or looks_blocked(response.text):
            raise CookieHarvestError("blocked", ResourceOutcome.BLOCKED)
        if response.status_code not in {200, 202, 400}:
            raise CookieHarvestError("unexpected_status", ResourceOutcome.NETWORK_ERROR)
        return response

    async def _harvest_one(self, marketplace_code: str, postal_code: str) -> str | None:
        market = MARKETPLACES.get(marketplace_code)
        if not market:
            raise ValueError("unsupported marketplace")
        marketplace_id = market.amazon_marketplace_id
        try:
            proxy = await self._proxy_provider.acquire("cookie", marketplace_code)
        except Exception:
            return "proxy_acquisition_failed"
        if self._policy.require_proxy and proxy is None:
            return "proxy_unavailable"
        fingerprint = await self._fingerprint_provider.acquire("cookie", marketplace_code)
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/json",
            "Cache-Control": "no-cache",
            "Referer": f"https://{market.domain}/",
            **{
                key: value
                for key, value in fingerprint.headers.items()
                if key.lower() not in {"authorization", "cookie", "proxy-authorization"}
            },
        }
        proxy_url = proxy.proxy_url.reveal() if proxy else None
        try:
            async with self._session(
                headers=headers,
                proxy_url=proxy_url,
                impersonate=fingerprint.impersonate,
            ) as client:
                base_url = f"https://{market.domain}"
                first = await self._request(client, "GET", f"{base_url}/")
                validation_token = self._validation_token(first.text)
                if not validation_token:
                    raise CookieHarvestError("missing_validation_token", ResourceOutcome.PARSE_ERROR)

                ubid_name = UBID_COOKIE_NAMES.get(marketplace_code)
                cookies = self._cookie_dict(client)
                if ubid_name and ubid_name not in cookies:
                    await self._request(
                        client,
                        "GET",
                        f"{base_url}/privacyprefs/retail/v3/banner",
                    )
                    cookies = self._cookie_dict(client)
                    if ubid_name not in cookies:
                        raise CookieHarvestError("missing_ubid_cookie", ResourceOutcome.AUTH_INVALID)

                selection = await self._request(
                    client,
                    "GET",
                    f"{base_url}/portal-migration/hz/glow/get-rendered-address-selections",
                    params={
                        "deviceType": "desktop",
                        "pageType": "Gateway",
                        "storeContext": "NoStoreName",
                        "actionSource": "desktop-modal",
                    },
                    headers={"anti-csrftoken-a2z": validation_token},
                )
                csrf_token = self._csrf_token(selection.text)
                if not csrf_token:
                    raise CookieHarvestError("missing_csrf_token", ResourceOutcome.PARSE_ERROR)

                payload: dict[str, str] = {
                    "locationType": "LOCATION_INPUT",
                    "zipCode": postal_code,
                    "storeContext": "generic",
                    "deviceType": "web",
                    "pageType": "Gateway",
                    "actionSource": "glow",
                }
                if marketplace_code == "AU":
                    payload["locationType"] = "POSTAL_CODE_WITH_CITY"
                    if postal_code == "2000":
                        payload["city"] = "SYDNEY"
                changed = await self._request(
                    client,
                    "POST",
                    f"{base_url}/portal-migration/hz/glow/address-change?actionSource=glow",
                    json=payload,
                    headers={
                        "anti-csrftoken-a2z": csrf_token,
                        "content-type": "application/json",
                    },
                )
                try:
                    valid_address = int(changed.json().get("isValidAddress", 0)) == 1
                except (ValueError, AttributeError, TypeError):
                    valid_address = bool(re.search(r'"isValidAddress"\s*:\s*1', changed.text))
                if not valid_address:
                    raise CookieHarvestError("invalid_address", ResourceOutcome.AUTH_INVALID)

                verification = await self._request(client, "GET", f"{base_url}/")
                if not self._address_matches(verification.text, postal_code):
                    raise CookieHarvestError(
                        "address_not_applied", ResourceOutcome.AUTH_INVALID
                    )

                cookies = self._cookie_dict(client)
                locale_cookie = LOCALE_COOKIES.get(marketplace_code)
                if locale_cookie:
                    cookies[locale_cookie[0]] = locale_cookie[1]
                currency = CURRENCIES.get(marketplace_code)
                if currency:
                    cookies["i18n-prefs"] = currency
                await self._store.save(
                    marketplace_id,
                    postal_code,
                    cookies,
                    self._policy.ttl_seconds,
                )
            if proxy:
                await self._proxy_provider.report(proxy, ResourceOutcome.SUCCESS)
            return None
        except CookieHarvestError as exc:
            if proxy:
                await self._proxy_provider.report(proxy, exc.outcome)
            return exc.code

    async def ensure_capacity(
        self, marketplace_id: str, postal_code: str, target_count: int
    ) -> HarvestReport:
        if target_count < 0:
            raise ValueError("target_count cannot be negative")
        market = MARKETPLACES.get(marketplace_id.upper())
        if market is None:
            raise ValueError("unsupported marketplace")
        code = market.id
        amazon_id = market.amazon_marketplace_id
        existing = await self._store.count(amazon_id, postal_code)
        deficit = max(0, target_count - existing)
        created = 0
        rejected = 0
        failure_codes: list[str] = []

        async def attempt() -> str | None:
            async with self._semaphore:
                return await self._harvest_one(code, postal_code)

        remaining = deficit
        for _ in range(self._policy.max_attempts_per_cookie):
            if remaining <= 0:
                break
            results = await asyncio.gather(*(attempt() for _ in range(remaining)))
            successes = sum(1 for result in results if result is None)
            failures = len(results) - successes
            failure_codes.extend(
                result for result in results if isinstance(result, str)
            )
            created += successes
            rejected += failures
            remaining = deficit - created

        available_after = await self._store.count(amazon_id, postal_code)
        if created and self._consumer_provider:
            await self._consumer_provider.refresh()
        return HarvestReport(
            marketplace_id=amazon_id,
            postal_code=postal_code,
            requested=target_count,
            created=created,
            rejected=rejected,
            available_after=available_after,
            failure_codes=tuple(sorted(failure_codes)),
        )
