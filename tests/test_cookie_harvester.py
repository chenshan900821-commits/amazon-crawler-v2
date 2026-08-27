from __future__ import annotations

import json
import unittest

import httpx

from amazon_crawler.infra.cookie_harvester import AmazonCookieHarvester, HarvesterPolicy
from amazon_crawler.infra.resources import BrowserFingerprintProvider, StaticProxyProvider


class FakeCookieStore:
    def __init__(self) -> None:
        self.values: list[tuple[str, str, dict[str, str], int]] = []

    async def count(self, marketplace_id: str, postal_code: str) -> int:
        return sum(
            1
            for market, postal, _, _ in self.values
            if market == marketplace_id and postal == postal_code
        )

    async def save(
        self,
        marketplace_id: str,
        postal_code: str,
        cookies: dict[str, str],
        ttl_seconds: int,
    ) -> None:
        self.values.append((marketplace_id, postal_code, dict(cookies), ttl_seconds))


class FakeCurlResponse:
    def __init__(self, status_code: int, *, text: str = "", payload=None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class FakeCurlCookies:
    def __init__(self, initial: dict[str, str]) -> None:
        self.values = dict(initial)

    def get_dict(self) -> dict[str, str]:
        return dict(self.values)


class FakeCurlSession:
    def __init__(self, calls: list[tuple[str, str]], **kwargs) -> None:
        self.calls = calls
        self.kwargs = kwargs
        self.cookies = FakeCurlCookies(kwargs["cookies"])
        self.closed = False
        self.address_changed = False

    async def request(self, method: str, url: str, **kwargs):
        path = httpx.URL(url).path
        self.calls.append((method, path))
        if path == "/":
            if self.address_changed:
                return FakeCurlResponse(
                    200,
                    text='<span id="glow-ingress-line2">Deliver to 10001</span>',
                )
            self.cookies.values["ubid-main"] = "ubid-curl"
            return FakeCurlResponse(
                200,
                text='<input id="glowValidationToken" value="validation-curl">',
            )
        if path.endswith("get-rendered-address-selections"):
            return FakeCurlResponse(200, text='CSRF_TOKEN : "csrf-curl",')
        if path.endswith("address-change"):
            self.cookies.values["location"] = "10001"
            self.address_changed = True
            return FakeCurlResponse(200, payload={"isValidAddress": 1})
        return FakeCurlResponse(404)

    async def close(self) -> None:
        self.closed = True


class CookieHarvesterTests(unittest.IsolatedAsyncioTestCase):
    async def test_validated_postal_cookie_is_stored_with_ttl(self) -> None:
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/":
                if "location=10001" in request.headers.get("cookie", ""):
                    return httpx.Response(
                        200,
                        text='<span id="glow-ingress-line2">Deliver to 10001</span>',
                    )
                return httpx.Response(
                    200,
                    text='<input id="glowValidationToken" value="validation-1">',
                    headers={"set-cookie": "ubid-main=ubid-1; Path=/"},
                )
            if request.url.path.endswith("get-rendered-address-selections"):
                self.assertEqual(request.headers["anti-csrftoken-a2z"], "validation-1")
                return httpx.Response(200, text='CSRF_TOKEN : "csrf-1",')
            if request.url.path.endswith("address-change"):
                payload = json.loads(request.content)
                self.assertEqual(payload["zipCode"], "10001")
                return httpx.Response(
                    200,
                    json={"isValidAddress": 1},
                    headers={"set-cookie": "location=10001; Path=/"},
                )
            return httpx.Response(404)

        store = FakeCookieStore()
        harvester = AmazonCookieHarvester(
            store=store,
            proxy_provider=StaticProxyProvider(None),
            fingerprint_provider=BrowserFingerprintProvider(),
            policy=HarvesterPolicy(concurrency=2, max_attempts_per_cookie=2),
            transport=httpx.MockTransport(handler),
        )
        report = await harvester.ensure_capacity("US", "10001", 2)
        self.assertEqual(report.created, 2)
        self.assertEqual(report.available_after, 2)
        self.assertEqual(len(store.values), 2)
        market, postal, cookies, ttl = store.values[0]
        self.assertEqual(market, "ATVPDKIKX0DER")
        self.assertEqual(postal, "10001")
        self.assertEqual(ttl, 172800)
        self.assertEqual(cookies["location"], "10001")
        self.assertEqual(cookies["lc-main"], "en_US")
        self.assertEqual(cookies["i18n-prefs"], "USD")
        self.assertNotIn("validation-1", json.dumps(store.values))
        self.assertEqual(calls.count("/"), 4)
        self.assertEqual(harvester.backend, "httpx")
        self.assertFalse(harvester.tls_impersonation)

    async def test_cookie_production_uses_real_tls_impersonation_backend(self) -> None:
        calls: list[tuple[str, str]] = []
        sessions: list[FakeCurlSession] = []

        def factory(**kwargs):
            session = FakeCurlSession(calls, **kwargs)
            sessions.append(session)
            return session

        store = FakeCookieStore()
        harvester = AmazonCookieHarvester(
            store=store,
            proxy_provider=StaticProxyProvider("http://proxy.example.test:8000"),
            fingerprint_provider=BrowserFingerprintProvider(),
            policy=HarvesterPolicy(concurrency=1, max_attempts_per_cookie=1),
            transport_backend="curl_cffi",
            curl_session_factory=factory,
        )
        report = await harvester.ensure_capacity("US", "10001", 1)

        self.assertEqual(report.created, 1)
        self.assertEqual(harvester.backend, "curl_cffi")
        self.assertTrue(harvester.tls_impersonation)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].kwargs["impersonate"], "chrome131")
        self.assertFalse(sessions[0].kwargs["allow_redirects"])
        self.assertEqual(
            sessions[0].kwargs["proxy"], "http://proxy.example.test:8000"
        )
        self.assertTrue(sessions[0].closed)
        self.assertEqual(
            calls,
            [
                ("GET", "/"),
                ("GET", "/portal-migration/hz/glow/get-rendered-address-selections"),
                ("POST", "/portal-migration/hz/glow/address-change"),
                ("GET", "/"),
            ],
        )
        self.assertEqual(store.values[0][2]["location"], "10001")

    async def test_cookie_harvester_rejects_cross_host_redirect_before_following(self) -> None:
        hosts: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            hosts.append(request.url.host)
            return httpx.Response(
                302,
                headers={"location": "https://example.com/captured"},
            )

        store = FakeCookieStore()
        harvester = AmazonCookieHarvester(
            store=store,
            proxy_provider=StaticProxyProvider(None),
            fingerprint_provider=BrowserFingerprintProvider(),
            policy=HarvesterPolicy(concurrency=1, max_attempts_per_cookie=1),
            transport=httpx.MockTransport(handler),
        )

        report = await harvester.ensure_capacity("US", "10001", 1)

        self.assertEqual(report.created, 0)
        self.assertEqual(report.rejected, 1)
        self.assertEqual(hosts, ["www.amazon.com"])
        self.assertEqual(store.values, [])

    async def test_invalid_address_is_retried_but_never_stored(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(
                    200,
                    text='<input id="glowValidationToken" value="validation-1">',
                    headers={"set-cookie": "ubid-main=ubid-1; Path=/"},
                )
            if request.url.path.endswith("get-rendered-address-selections"):
                return httpx.Response(200, text='CSRF_TOKEN : "csrf-1",')
            return httpx.Response(200, json={"isValidAddress": 0})

        store = FakeCookieStore()
        harvester = AmazonCookieHarvester(
            store=store,
            proxy_provider=StaticProxyProvider(None),
            fingerprint_provider=BrowserFingerprintProvider(),
            policy=HarvesterPolicy(concurrency=1, max_attempts_per_cookie=3),
            transport=httpx.MockTransport(handler),
        )
        report = await harvester.ensure_capacity("US", "00000", 1)
        self.assertEqual(report.created, 0)
        self.assertEqual(report.rejected, 3)
        self.assertEqual(report.available_after, 0)
        self.assertEqual(report.failure_codes, ("invalid_address",) * 3)
        self.assertEqual(store.values, [])

    async def test_address_change_success_without_session_confirmation_is_rejected(self) -> None:
        changed = False

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal changed
            if request.url.path == "/":
                if changed:
                    return httpx.Response(
                        200,
                        text='<span id="glow-ingress-line2">Deliver to 94105</span>',
                    )
                return httpx.Response(
                    200,
                    text='<input id="glowValidationToken" value="validation-1">',
                    headers={"set-cookie": "ubid-main=ubid-1; Path=/"},
                )
            if request.url.path.endswith("get-rendered-address-selections"):
                return httpx.Response(200, text='CSRF_TOKEN : "csrf-1",')
            changed = True
            return httpx.Response(200, json={"isValidAddress": 1})

        store = FakeCookieStore()
        harvester = AmazonCookieHarvester(
            store=store,
            proxy_provider=StaticProxyProvider(None),
            fingerprint_provider=BrowserFingerprintProvider(),
            policy=HarvesterPolicy(concurrency=1, max_attempts_per_cookie=1),
            transport=httpx.MockTransport(handler),
        )
        report = await harvester.ensure_capacity("US", "10001", 1)
        self.assertEqual(report.created, 0)
        self.assertEqual(report.rejected, 1)
        self.assertEqual(report.failure_codes, ("address_not_applied",))
        self.assertEqual(store.values, [])


if __name__ == "__main__":
    unittest.main()
