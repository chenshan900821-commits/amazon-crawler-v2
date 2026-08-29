from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from amazon_crawler.domain.models import CrawlFailure
from amazon_crawler.domain.resources import RequestContext, ResourceOutcome
from amazon_crawler.infra.resources import (
    BrowserFingerprintProvider,
    RequestContextFactory,
    StaticCookieProvider,
    StaticProxyProvider,
)

# The current legacy validators do not treat every HTTP 404/410 as a business
# terminal.  They first look for one of these page/address markers in the body;
# an otherwise unclassified non-success response is retried.  Keep this list
# deliberately narrower than the 2xx no-result markers used by the parsers.
LEGACY_NON_SUCCESS_NOT_FOUND_MARKERS = (
    "sorry! we couldn't find that page. try searching or go to amazon's home page.",
    "desculpe! não conseguimos encontrar esta página. pesquise novamente ou volte para a página inicial",
    "lo sentimos! no pudimos encontrar la página que buscabas. trata de usar la barra de búsqueda o visita la página principal",
    "we’re sorry. the web address you entered is not a functioning page on our site.",
    "we're sorry. the web address you've entered is not a functioning page on our site.",
    "üzgünüz. girdiğiniz web adresi, sitemizde işlev gösteren bir sayfaya karşılık gelmiyor.",
    "przepraszamy. wyszukiwana strona nie istnieje.",
    "nous sommes désolés. l'adresse web que vous avez saisie n'est pas une page fonctionnelle de notre site.",
    "siamo spiacenti. l'indirizzo web inserito non è una pagina funzionante sul nostro sito.",
    "vi ber om ursäkt. webbadressen du har angett är inte en fungerande sida på vår webbplats.",
    "lo sentimos. la dirección web que has especificado no es una página activa de nuestro sitio.",
    "this page is unavailable, usually because you're not a customer in the country or region where we have rights for this event.",
)


@dataclass(frozen=True, slots=True)
class FetchResponse:
    url: str
    status_code: int
    html: str
    headers: dict[str, str]
    resource_context: RequestContext = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResponseObservation:
    """In-memory transport observation available before status classification."""

    url: str
    status_code: int
    html: str
    headers: dict[str, str]
    content_sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class _TransportResponse:
    url: str
    status_code: int
    content: bytes
    text: str
    headers: dict[str, str]


class _TransportFailure(RuntimeError):
    def __init__(self, code: str, error_type: str) -> None:
        super().__init__(code)
        self.code = code
        self.error_type = error_type


class HostRateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self._interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last_request: dict[str, float] = {}

    async def wait(self, url: str) -> None:
        if self._interval <= 0:
            return
        host = urlparse(url).hostname or "unknown"
        async with self._lock:
            now = time.monotonic()
            delay = self._interval - (now - self._last_request.get(host, 0.0))
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request[host] = time.monotonic()


@dataclass(slots=True)
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False


class HostCircuitBreaker:
    """Bound repeated upstream failures without hiding durable retry state."""

    def __init__(self, failure_threshold: int, recovery_seconds: float) -> None:
        self._threshold = max(1, int(failure_threshold))
        self._recovery_seconds = max(1.0, float(recovery_seconds))
        self._states: dict[str, _CircuitState] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _host(url: str) -> str:
        return (urlparse(url).hostname or "unknown").lower()

    async def allow(self, url: str) -> tuple[bool, float | None]:
        host = self._host(url)
        async with self._lock:
            state = self._states.setdefault(host, _CircuitState())
            if state.opened_at is None:
                return True, None
            elapsed = time.monotonic() - state.opened_at
            if elapsed < self._recovery_seconds:
                return False, round(self._recovery_seconds - elapsed, 3)
            if state.probe_in_flight:
                return False, self._recovery_seconds
            state.probe_in_flight = True
            return True, None

    async def success(self, url: str) -> None:
        host = self._host(url)
        async with self._lock:
            self._states[host] = _CircuitState()

    async def failure(self, url: str) -> None:
        host = self._host(url)
        async with self._lock:
            state = self._states.setdefault(host, _CircuitState())
            state.probe_in_flight = False
            state.failures += 1
            if state.failures >= self._threshold:
                state.opened_at = time.monotonic()

    async def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        async with self._lock:
            circuits = []
            for host, state in sorted(self._states.items()):
                retry_after = (
                    max(0.0, self._recovery_seconds - (now - state.opened_at))
                    if state.opened_at is not None
                    else None
                )
                status = (
                    "half_open"
                    if state.probe_in_flight
                    else "open"
                    if state.opened_at is not None
                    else "closed"
                )
                circuits.append(
                    {
                        "host": host,
                        "status": status,
                        "consecutive_failures": state.failures,
                        "retry_after_seconds": (
                            round(retry_after, 3) if retry_after is not None else None
                        ),
                    }
                )
        open_count = sum(
            1 for circuit in circuits if circuit["status"] in {"open", "half_open"}
        )
        return {
            "status": "degraded" if open_count else "ready",
            "open_circuit_count": open_count,
            "circuits": circuits,
            "failure_threshold": self._threshold,
            "recovery_seconds": self._recovery_seconds,
        }


class HttpFetcher:
    supports_response_observer = True

    def __init__(
        self,
        *,
        timeout_seconds: float,
        min_host_interval_seconds: float,
        circuit_failure_threshold: int = 5,
        circuit_recovery_seconds: float = 60.0,
        max_response_bytes: int,
        user_agent: str,
        proxy: str | None = None,
        cookie: str | None = None,
        context_factory: RequestContextFactory | None = None,
        transport_backend: str = "auto",
        curl_session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._limiter = HostRateLimiter(min_host_interval_seconds)
        self._circuit = HostCircuitBreaker(
            circuit_failure_threshold,
            circuit_recovery_seconds,
        )
        self._base_headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
        }
        self._context_factory = context_factory or RequestContextFactory(
            StaticCookieProvider(cookie),
            StaticProxyProvider(proxy),
            BrowserFingerprintProvider(),
        )
        if transport_backend not in {"auto", "curl_cffi", "httpx"}:
            raise ValueError("transport_backend must be auto, curl_cffi, or httpx")
        curl_available = (
            curl_session_factory is not None
            or importlib.util.find_spec("curl_cffi") is not None
        )
        if transport_backend == "curl_cffi" and not curl_available:
            raise RuntimeError(
                "curl_cffi transport was requested but the curl-cffi package is unavailable"
            )
        self._transport_backend = (
            "curl_cffi"
            if transport_backend == "curl_cffi"
            or (transport_backend == "auto" and curl_available)
            else "httpx"
        )
        self._curl_session_factory = curl_session_factory

    @property
    def backend(self) -> str:
        return self._transport_backend

    @property
    def tls_impersonation(self) -> bool:
        return self._transport_backend == "curl_cffi"

    async def health(self) -> dict[str, Any]:
        circuit = await self._circuit.snapshot()
        return {
            "status": circuit["status"],
            "transport_backend": self.backend,
            "tls_impersonation": self.tls_impersonation,
            **circuit,
        }

    @staticmethod
    def _purpose_headers(purpose: str, method: str) -> dict[str, str]:
        if method.upper() != "POST":
            return {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            }
        headers = {
            "Accept": "text/html,image/webp,*/*",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "Pragma": "no-cache",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if purpose == "rank_list":
            headers["Accept"] = "text/html, application/json"
        return headers

    @staticmethod
    def _response_evidence(response: _TransportResponse) -> dict[str, object]:
        """Return only non-secret evidence safe for durable failure events."""

        return {
            "http_status": response.status_code,
            "evidence": {
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "bytes": len(response.content),
                "captured": False,
            },
        }

    async def _send_httpx(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, object] | None,
        proxy: str | None,
    ) -> _TransportResponse:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                proxy=proxy,
                headers=headers,
            ) as client:
                response = await client.request(method.upper(), url, json=json_body)
        except httpx.ProxyError as exc:
            raise _TransportFailure("proxy_error", type(exc).__name__) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise _TransportFailure("network_error", type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise _TransportFailure("http_client_error", type(exc).__name__) from exc
        return _TransportResponse(
            url=str(response.url),
            status_code=response.status_code,
            content=response.content,
            text=response.text,
            headers={
                str(key).lower(): str(value) for key, value in response.headers.items()
            },
        )

    async def _send_curl(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, object] | None,
        proxy: str | None,
        impersonate: str | None,
    ) -> _TransportResponse:
        session_factory = self._curl_session_factory
        if session_factory is None:
            from curl_cffi.requests import AsyncSession

            session_factory = AsyncSession
        try:
            async with session_factory(
                timeout=self._timeout,
                allow_redirects=False,
                proxy=proxy,
                headers=headers,
                impersonate=impersonate or "chrome131",
            ) as client:
                response = await client.request(method.upper(), url, json=json_body)
        except Exception as exc:
            name = type(exc).__name__
            code = (
                "proxy_error" if proxy and "proxy" in name.lower() else "network_error"
            )
            raise _TransportFailure(code, name) from exc
        return _TransportResponse(
            url=str(response.url),
            status_code=int(response.status_code),
            content=bytes(response.content),
            text=str(response.text),
            headers={
                str(key).lower(): str(value) for key, value in response.headers.items()
            },
        )

    async def fetch(
        self,
        url: str,
        *,
        marketplace_id: str,
        postal_code: str | None,
        purpose: str,
        require_cookie: bool = True,
        method: str = "GET",
        json_body: dict[str, object] | None = None,
        extra_headers: dict[str, str] | None = None,
        response_observer: Callable[[ResponseObservation], None] | None = None,
    ) -> FetchResponse | CrawlFailure:
        try:
            context = await self._context_factory.acquire(
                purpose=purpose,
                marketplace_id=marketplace_id,
                postal_code=postal_code,
            )
        except Exception as exc:
            return CrawlFailure(
                "resource_provider_error",
                "request resources could not be acquired",
                retryable=True,
                details={"error_type": type(exc).__name__},
            )
        if require_cookie and context.cookie is None:
            await self._context_factory.report(context, ResourceOutcome.SUCCESS)
            return CrawlFailure(
                "cookie_unavailable",
                "no healthy cookie is available for the marketplace and postal code",
                retryable=True,
                details={"marketplace_id": marketplace_id, "postal_code": postal_code},
            )

        allowed, retry_after = await self._circuit.allow(url)
        if not allowed:
            await self._context_factory.report(context, ResourceOutcome.SUCCESS)
            return CrawlFailure(
                "upstream_circuit_open",
                "Amazon requests are temporarily paused after repeated upstream failures",
                retryable=True,
                details={"retry_after_seconds": retry_after},
            )
        await self._limiter.wait(url)

        headers = dict(self._base_headers)
        headers.update(self._purpose_headers(purpose, method))
        headers.update(
            {
                key: value
                for key, value in context.fingerprint.headers.items()
                if key.lower() not in {"authorization", "cookie", "proxy-authorization"}
            }
        )
        if extra_headers:
            headers.update(
                {
                    key: value
                    for key, value in extra_headers.items()
                    if key.lower()
                    not in {"authorization", "cookie", "proxy-authorization"}
                }
            )
        if context.cookie:
            headers["Cookie"] = context.cookie.cookie_header.reveal()
        proxy = context.proxy.proxy_url.reveal() if context.proxy else None
        request_method = method.upper()
        request_url = url
        request_body = json_body

        async def send() -> _TransportResponse:
            if self._transport_backend == "curl_cffi":
                return await self._send_curl(
                    request_method,
                    request_url,
                    headers=headers,
                    json_body=request_body,
                    proxy=proxy,
                    impersonate=context.fingerprint.impersonate,
                )
            return await self._send_httpx(
                request_method,
                request_url,
                headers=headers,
                json_body=request_body,
                proxy=proxy,
            )

        initial_host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        try:
            response = await send()
            redirect_count = 0
            while response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    break
                target = urljoin(response.url, location)
                parsed_target = urlparse(target)
                target_host = (
                    (parsed_target.hostname or "").lower().removeprefix("www.")
                )
                if parsed_target.scheme != "https" or target_host != initial_host:
                    await self._circuit.success(url)
                    await self._context_factory.report(context, ResourceOutcome.BLOCKED)
                    return CrawlFailure(
                        "redirect_host_not_allowed",
                        "the upstream response attempted an unsafe redirect",
                        retryable=False,
                        details=self._response_evidence(response),
                    )
                redirect_count += 1
                if redirect_count > 5:
                    await self._circuit.failure(url)
                    await self._context_factory.report(
                        context, ResourceOutcome.NETWORK_ERROR
                    )
                    return CrawlFailure(
                        "redirect_limit_exceeded",
                        "the upstream response exceeded the redirect limit",
                        retryable=True,
                        details=self._response_evidence(response),
                    )
                if response.status_code == 303 or (
                    response.status_code in {301, 302} and request_method == "POST"
                ):
                    request_method = "GET"
                    request_body = None
                    headers.pop("Content-Type", None)
                request_url = target
                await self._limiter.wait(request_url)
                response = await send()
        except asyncio.CancelledError:
            await self._circuit.failure(url)
            await self._context_factory.report(context, ResourceOutcome.NETWORK_ERROR)
            raise
        except _TransportFailure as exc:
            await self._circuit.failure(url)
            outcome = (
                ResourceOutcome.PROXY_ERROR
                if exc.code == "proxy_error"
                else ResourceOutcome.NETWORK_ERROR
            )
            await self._context_factory.report(context, outcome)
            messages = {
                "proxy_error": "the selected proxy could not complete the request",
                "network_error": "the upstream request timed out or the network transport failed",
                "http_client_error": "the HTTP client could not complete the upstream request",
            }
            return CrawlFailure(
                exc.code,
                messages[exc.code],
                retryable=True,
                details={"error_type": exc.error_type},
            )

        if response_observer is not None:
            observation = ResponseObservation(
                url=str(response.url),
                status_code=response.status_code,
                html=response.text,
                headers={
                    "content-type": response.headers.get("content-type", ""),
                    "content-language": response.headers.get("content-language", ""),
                },
                content_sha256=hashlib.sha256(response.content).hexdigest(),
                byte_count=len(response.content),
            )
            try:
                response_observer(observation)
            except Exception:
                # Observability must never change crawler outcome. A controlled
                # evidence collector independently fails if its sink captured
                # nothing.
                pass

        final_host = (
            (urlparse(response.url).hostname or "").lower().removeprefix("www.")
        )
        if final_host != initial_host:
            await self._circuit.success(url)
            await self._context_factory.report(context, ResourceOutcome.BLOCKED)
            return CrawlFailure(
                "redirect_host_not_allowed",
                "the upstream response redirected outside the selected Amazon host",
                retryable=False,
                details=self._response_evidence(response),
            )

        if len(response.content) > self._max_response_bytes:
            await self._circuit.success(url)
            await self._context_factory.report(context, ResourceOutcome.PARSE_ERROR)
            return CrawlFailure(
                "response_too_large",
                f"response exceeded {self._max_response_bytes} bytes",
                retryable=False,
                details=self._response_evidence(response),
            )
        if response.status_code == 407:
            await self._circuit.success(url)
            await self._context_factory.report(context, ResourceOutcome.PROXY_ERROR)
            return CrawlFailure(
                "proxy_authentication_error",
                "the selected proxy rejected authentication",
                retryable=True,
                details=self._response_evidence(response),
            )
        if response.status_code in {403, 429}:
            await self._circuit.failure(url)
            await self._context_factory.report(context, ResourceOutcome.BLOCKED)
            return CrawlFailure(
                "upstream_blocked",
                f"Amazon returned HTTP {response.status_code}",
                retryable=True,
                details=self._response_evidence(response),
            )
        if response.status_code in {408, 425, 500, 502, 503, 504}:
            await self._circuit.failure(url)
            await self._context_factory.report(context, ResourceOutcome.NETWORK_ERROR)
            return CrawlFailure(
                "upstream_retryable",
                f"Amazon returned HTTP {response.status_code}",
                retryable=True,
                details=self._response_evidence(response),
            )
        await self._circuit.success(url)
        if response.status_code in {404, 410}:
            sample = response.text[:200_000].lower()
            if any(marker in sample for marker in LEGACY_NON_SUCCESS_NOT_FOUND_MARKERS):
                await self._context_factory.report(context, ResourceOutcome.PARSE_ERROR)
                not_found_code = {
                    "merchant": "merchant_has_no_products",
                    "merchant_home": "merchant_has_no_products",
                    "merchant_products": "merchant_page_has_no_products",
                    "search": "no_results",
                    "search_hour": "no_results",
                }.get(purpose, "product_not_found")
                return CrawlFailure(
                    not_found_code,
                    f"Amazon returned a confirmed missing page (HTTP {response.status_code})",
                    retryable=False,
                    details=self._response_evidence(response),
                )
            await self._context_factory.report(context, ResourceOutcome.NETWORK_ERROR)
            return CrawlFailure(
                "upstream_retryable",
                f"Amazon returned an unclassified HTTP {response.status_code}",
                retryable=True,
                details=self._response_evidence(response),
            )
        if response.status_code >= 400:
            await self._context_factory.report(context, ResourceOutcome.NETWORK_ERROR)
            return CrawlFailure(
                "upstream_http_error",
                f"Amazon returned HTTP {response.status_code}",
                retryable=False,
                details=self._response_evidence(response),
            )
        return FetchResponse(
            url=str(response.url),
            status_code=response.status_code,
            html=response.text,
            headers={
                "content-type": response.headers.get("content-type", ""),
                "content-language": response.headers.get("content-language", ""),
            },
            resource_context=context,
        )

    async def report(self, response: FetchResponse, outcome: ResourceOutcome) -> None:
        await self._context_factory.report(response.resource_context, outcome)
