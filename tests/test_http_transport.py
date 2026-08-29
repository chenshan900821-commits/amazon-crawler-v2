from __future__ import annotations

import hashlib
import unittest

from amazon_crawler.domain.models import CrawlFailure
from amazon_crawler.domain.resources import FingerprintProfile, ResourceOutcome
from amazon_crawler.infra.http import (
    FetchResponse,
    HostCircuitBreaker,
    HttpFetcher,
    ResponseObservation,
)
from amazon_crawler.infra.resources import (
    BrowserFingerprintProvider,
    RequestContextFactory,
    StaticCookieProvider,
    StaticProxyProvider,
)


class FakeResponse:
    response_status = 200
    response_content = b"<html>fixture</html>"
    response_headers = {"Content-Type": "text/html"}

    def __init__(self, url: str) -> None:
        self.url = url
        self.status_code = type(self).response_status
        self.content = type(self).response_content
        self.text = self.content.decode()
        self.headers = dict(type(self).response_headers)


class FakeCurlSession:
    constructor_kwargs: dict[str, object] = {}
    response_url = "https://www.amazon.com/dp/B000000001"
    requests: list[tuple[str, str]] = []

    def __init__(self, **kwargs) -> None:
        type(self).constructor_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, _method: str, _url: str, **_kwargs):
        type(self).requests.append((_method, _url))
        return FakeResponse(type(self).response_url)


class TrackingCookieProvider(StaticCookieProvider):
    def __init__(self) -> None:
        super().__init__("fixture-session=value")
        self.outcomes: list[ResourceOutcome] = []

    async def report(self, lease, outcome: ResourceOutcome) -> None:
        self.outcomes.append(outcome)
        await super().report(lease, outcome)


class HttpTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeResponse.response_status = 200
        FakeResponse.response_content = b"<html>fixture</html>"
        FakeResponse.response_headers = {"Content-Type": "text/html"}
        FakeCurlSession.response_url = "https://www.amazon.com/dp/B000000001"
        FakeCurlSession.requests = []

    def fetcher(self) -> HttpFetcher:
        context = RequestContextFactory(
            StaticCookieProvider("fixture-session=value"),
            StaticProxyProvider(None),
            BrowserFingerprintProvider(
                [
                    FingerprintProfile(
                        "fixture-chrome",
                        {"User-Agent": "Fixture Browser"},
                        impersonate="chrome142",
                    )
                ]
            ),
        )
        return HttpFetcher(
            timeout_seconds=10,
            min_host_interval_seconds=0,
            max_response_bytes=100_000,
            user_agent="fallback",
            context_factory=context,
            transport_backend="curl_cffi",
            curl_session_factory=FakeCurlSession,
        )

    async def test_circuit_health_reports_open_and_recovered_state(self) -> None:
        circuit = HostCircuitBreaker(failure_threshold=2, recovery_seconds=60)
        url = "https://www.amazon.com/dp/B000000001"
        await circuit.failure(url)
        await circuit.failure(url)

        opened = await circuit.snapshot()
        self.assertEqual(opened["status"], "degraded")
        self.assertEqual(opened["open_circuit_count"], 1)
        self.assertEqual(opened["circuits"][0]["status"], "open")
        self.assertNotIn("B000000001", str(opened))

        await circuit.success(url)
        recovered = await circuit.snapshot()
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(recovered["open_circuit_count"], 0)

    async def test_curl_transport_uses_real_impersonation_profile(self) -> None:
        FakeResponse.response_status = 200
        FakeCurlSession.response_url = "https://www.amazon.com/dp/B000000001"
        fetcher = self.fetcher()
        response = await fetcher.fetch(
            FakeCurlSession.response_url,
            marketplace_id="US",
            postal_code="10001",
            purpose="product",
        )
        self.assertIsInstance(response, FetchResponse)
        self.assertEqual(fetcher.backend, "curl_cffi")
        self.assertTrue(fetcher.tls_impersonation)
        self.assertEqual(FakeCurlSession.constructor_kwargs["impersonate"], "chrome142")
        self.assertFalse(FakeCurlSession.constructor_kwargs["allow_redirects"])
        self.assertEqual(
            FakeCurlSession.constructor_kwargs["headers"]["Cookie"],
            "fixture-session=value",
        )
        self.assertEqual(
            FakeCurlSession.constructor_kwargs["headers"]["sec-fetch-mode"],
            "navigate",
        )
        self.assertNotIn("fixture-session=value", repr(response))

    async def test_ajax_request_uses_legacy_safe_header_shape(self) -> None:
        FakeResponse.response_status = 200
        FakeCurlSession.response_url = "https://www.amazon.com/s/query?k=mouse"
        response = await self.fetcher().fetch(
            FakeCurlSession.response_url,
            marketplace_id="US",
            postal_code="10001",
            purpose="search",
            method="POST",
            json_body={"customer-action": "pagination"},
        )
        self.assertIsInstance(response, FetchResponse)
        headers = FakeCurlSession.constructor_kwargs["headers"]
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["sec-fetch-mode"], "cors")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")

    async def test_redirect_to_non_amazon_host_is_rejected(self) -> None:
        FakeResponse.response_status = 200
        FakeCurlSession.response_url = "https://example.com/captured"
        response = await self.fetcher().fetch(
            "https://www.amazon.com/dp/B000000001",
            marketplace_id="US",
            postal_code=None,
            purpose="product",
        )
        self.assertIsInstance(response, CrawlFailure)
        self.assertEqual(response.code, "redirect_host_not_allowed")
        self.assertFalse(response.retryable)

    async def test_cross_host_redirect_is_rejected_before_a_second_request(
        self,
    ) -> None:
        FakeResponse.response_status = 302
        FakeResponse.response_headers = {
            "Content-Type": "text/html",
            "Location": "https://example.com/captured",
        }
        response = await self.fetcher().fetch(
            "https://www.amazon.com/dp/B000000001",
            marketplace_id="US",
            postal_code=None,
            purpose="product",
        )

        self.assertIsInstance(response, CrawlFailure)
        self.assertEqual(response.code, "redirect_host_not_allowed")
        self.assertEqual(
            FakeCurlSession.requests,
            [("GET", "https://www.amazon.com/dp/B000000001")],
        )

    async def test_same_host_redirect_is_followed_with_cookie_context_intact(
        self,
    ) -> None:
        class SameHostRedirectSession(FakeCurlSession):
            requests: list[tuple[str, str]] = []

            async def request(self, request_method: str, request_url: str, **_kwargs):
                type(self).requests.append((request_method, request_url))
                if len(type(self).requests) == 1:
                    response = FakeResponse(request_url)
                    response.status_code = 302
                    response.headers = {"location": "/dp/B000000001?ref=canonical"}
                    return response
                return FakeResponse(request_url)

        context = RequestContextFactory(
            StaticCookieProvider("fixture-session=value"),
            StaticProxyProvider(None),
            BrowserFingerprintProvider(),
        )
        fetcher = HttpFetcher(
            timeout_seconds=10,
            min_host_interval_seconds=0,
            max_response_bytes=100_000,
            user_agent="fallback",
            context_factory=context,
            transport_backend="curl_cffi",
            curl_session_factory=SameHostRedirectSession,
        )

        response = await fetcher.fetch(
            "https://www.amazon.com/gp/product/B000000001",
            marketplace_id="US",
            postal_code=None,
            purpose="product",
        )

        self.assertIsInstance(response, FetchResponse)
        self.assertEqual(
            SameHostRedirectSession.requests,
            [
                ("GET", "https://www.amazon.com/gp/product/B000000001"),
                ("GET", "https://www.amazon.com/dp/B000000001?ref=canonical"),
            ],
        )

    async def test_www_canonicalization_is_not_treated_as_external_redirect(
        self,
    ) -> None:
        FakeResponse.response_status = 200
        FakeCurlSession.response_url = "https://amazon.com/dp/B000000001"
        response = await self.fetcher().fetch(
            "https://www.amazon.com/dp/B000000001",
            marketplace_id="US",
            postal_code=None,
            purpose="product",
        )
        self.assertIsInstance(response, FetchResponse)

    async def test_retryable_http_status_reports_and_releases_resources(self) -> None:
        cookie = TrackingCookieProvider()
        context = RequestContextFactory(
            cookie,
            StaticProxyProvider(None),
            BrowserFingerprintProvider(),
        )
        fetcher = HttpFetcher(
            timeout_seconds=10,
            min_host_interval_seconds=0,
            max_response_bytes=100_000,
            user_agent="fallback",
            context_factory=context,
            transport_backend="curl_cffi",
            curl_session_factory=FakeCurlSession,
        )
        FakeCurlSession.response_url = "https://www.amazon.com/dp/B000000001"
        FakeResponse.response_status = 503
        response = await fetcher.fetch(
            FakeCurlSession.response_url,
            marketplace_id="US",
            postal_code="10001",
            purpose="product",
        )
        self.assertIsInstance(response, CrawlFailure)
        self.assertEqual(response.code, "upstream_retryable")
        self.assertEqual(response.details["http_status"], 503)
        self.assertEqual(
            response.details["evidence"]["sha256"],
            hashlib.sha256(
                FakeResponse(FakeCurlSession.response_url).content
            ).hexdigest(),
        )
        self.assertFalse(response.details["evidence"]["captured"])
        self.assertEqual(cookie.outcomes, [ResourceOutcome.NETWORK_ERROR])
        self.assertEqual(context._cookie_owners, {})

    async def test_repeated_upstream_failures_open_host_circuit(self) -> None:
        context = RequestContextFactory(
            StaticCookieProvider("fixture-session=value"),
            StaticProxyProvider(None),
            BrowserFingerprintProvider(),
        )
        fetcher = HttpFetcher(
            timeout_seconds=10,
            min_host_interval_seconds=0,
            circuit_failure_threshold=2,
            circuit_recovery_seconds=60,
            max_response_bytes=100_000,
            user_agent="fallback",
            context_factory=context,
            transport_backend="curl_cffi",
            curl_session_factory=FakeCurlSession,
        )
        FakeResponse.response_status = 503
        url = "https://www.amazon.com/dp/B000000001"

        first = await fetcher.fetch(
            url, marketplace_id="US", postal_code="10001", purpose="product"
        )
        second = await fetcher.fetch(
            url, marketplace_id="US", postal_code="10001", purpose="product"
        )
        third = await fetcher.fetch(
            url, marketplace_id="US", postal_code="10001", purpose="product"
        )

        self.assertEqual(first.code, "upstream_retryable")
        self.assertEqual(second.code, "upstream_retryable")
        self.assertEqual(third.code, "upstream_circuit_open")
        self.assertEqual(len(FakeCurlSession.requests), 2)
        self.assertEqual(context._cookie_owners, {})

    async def test_observer_sees_response_before_failure_classification(self) -> None:
        FakeCurlSession.response_url = "https://www.amazon.com/dp/B000000001"
        FakeResponse.response_status = 429
        observations: list[ResponseObservation] = []

        response = await self.fetcher().fetch(
            FakeCurlSession.response_url,
            marketplace_id="US",
            postal_code="10001",
            purpose="product",
            response_observer=observations.append,
        )

        self.assertIsInstance(response, CrawlFailure)
        self.assertEqual(response.code, "upstream_blocked")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].status_code, 429)
        self.assertEqual(
            observations[0].content_sha256,
            response.details["evidence"]["sha256"],
        )

    async def test_not_found_code_is_scoped_to_request_purpose(self) -> None:
        FakeCurlSession.response_url = "https://www.amazon.com/sp?seller=MISSING"
        FakeResponse.response_status = 404
        FakeResponse.response_content = b"Sorry! We couldn't find that page. Try searching or go to Amazon's home page."

        response = await self.fetcher().fetch(
            FakeCurlSession.response_url,
            marketplace_id="US",
            postal_code="10001",
            purpose="merchant",
        )

        self.assertIsInstance(response, CrawlFailure)
        self.assertEqual(response.code, "merchant_has_no_products")
        self.assertFalse(response.retryable)

    async def test_unclassified_not_found_status_preserves_legacy_retry(self) -> None:
        FakeCurlSession.response_url = (
            "https://www.amazon.com/s/query?i=merchant-items&me=MISSING"
        )
        FakeResponse.response_status = 404
        FakeResponse.response_content = b'{"isEmpty":true}'

        response = await self.fetcher().fetch(
            FakeCurlSession.response_url,
            marketplace_id="US",
            postal_code="10001",
            purpose="merchant_products",
            method="POST",
            json_body={"customer-action": "pagination"},
        )

        self.assertIsInstance(response, CrawlFailure)
        self.assertEqual(response.code, "upstream_retryable")
        self.assertTrue(response.retryable)


if __name__ == "__main__":
    unittest.main()
