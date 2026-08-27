from __future__ import annotations

import asyncio
import fnmatch
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from amazon_crawler.bootstrap import build_application
from amazon_crawler.config import Settings
from amazon_crawler.domain.resources import ResourceOutcome, SecretText
from amazon_crawler.infra.resources import (
    BrowserFingerprintProvider,
    LegacyRedisCookieProvider,
    ProxyExtractionClient,
    RequestContextFactory,
    RotatingProxyProvider,
    RoutedCookieProvider,
    StaticCookieProvider,
    StaticProxyProvider,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRedis:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.scan_calls = 0

    def scan(self, cursor: int, *, match: str, count: int):
        self.scan_calls += 1
        keys = sorted(key for key in self.values if fnmatch.fnmatch(key, match))
        return 0, keys

    def mget(self, keys: list[str]):
        return [self.values.get(key) for key in keys]

    def get(self, key: str):
        return self.values.get(key)


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_browser_fingerprints_have_coherent_platform_headers(self) -> None:
        profiles = BrowserFingerprintProvider()._profiles
        rendered = {profile.profile_id: profile.headers for profile in profiles}
        self.assertIn("Windows NT", rendered["chrome_windows"]["User-Agent"])
        self.assertEqual(
            rendered["chrome_windows"]["sec-ch-ua-platform"], '"Windows"'
        )
        self.assertIn("Macintosh", rendered["chrome_macos"]["User-Agent"])
        self.assertNotIn("Windows NT", rendered["chrome_macos"]["User-Agent"])
        self.assertEqual(
            rendered["chrome_macos"]["sec-ch-ua-platform"], '"macOS"'
        )
        self.assertTrue(all(profile.impersonate == "chrome131" for profile in profiles))

    async def test_secret_text_and_public_health_never_render_secret(self) -> None:
        secret_value = "session-id=very-sensitive"
        secret = SecretText(secret_value)
        self.assertEqual(str(secret), "<redacted>")
        self.assertNotIn(secret_value, repr(secret))

        provider = StaticCookieProvider(secret_value)
        lease = await provider.acquire("US", "10001")
        self.assertIsNotNone(lease)
        self.assertNotIn(secret_value, repr(lease))
        rendered_health = json.dumps((await provider.health()).as_public_dict())
        self.assertNotIn(secret_value, rendered_health)

    async def test_legacy_provider_selects_marketplace_and_postal_code(self) -> None:
        redis = FakeRedis(
            {
                "cookie:US:10001:1": "{'session-id': 'one', 'ubid-main': 'a'}",
                "cookie:US:94105:2": '{"session-id": "two"}',
                "cookie:JP:100-0001:3": '{"session-id": "three"}',
                "unrelated:key": "ignored",
            }
        )
        provider = LegacyRedisCookieProvider(redis)
        lease = await provider.acquire("US", "94105")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.postal_code, "94105")
        self.assertEqual(lease.cookie_header.reveal(), "session-id=two")
        self.assertNotIn("session-id=two", repr(lease))

        any_us = await provider.acquire("US", None)
        self.assertIsNotNone(any_us)
        self.assertEqual(any_us.marketplace_id, "US")
        self.assertIn(any_us.postal_code, {"10001", "94105"})

    async def test_legacy_provider_maps_short_code_to_amazon_marketplace_id(self) -> None:
        redis = FakeRedis(
            {"cookie:ATVPDKIKX0DER:10001:1": '{"session-id": "one"}'}
        )
        provider = LegacyRedisCookieProvider(
            redis,
            marketplace_aliases={"US": "ATVPDKIKX0DER"},
        )
        lease = await provider.acquire("US", "10001")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.marketplace_id, "ATVPDKIKX0DER")

    async def test_cold_start_concurrency_refreshes_once(self) -> None:
        redis = FakeRedis({"cookie:US:10001:1": '{"session-id": "one"}'})
        provider = LegacyRedisCookieProvider(redis)
        leases = await asyncio.gather(
            *(provider.acquire("US", "10001") for _ in range(20))
        )
        self.assertTrue(all(lease is not None for lease in leases))
        self.assertEqual(redis.scan_calls, 1)

    async def test_empty_cold_start_converges_refresh_and_direct_miss(self) -> None:
        redis = FakeRedis({})
        provider = LegacyRedisCookieProvider(redis, direct_miss_seconds=30)
        leases = await asyncio.gather(
            *(provider.acquire("US", "10001") for _ in range(20))
        )
        self.assertTrue(all(lease is None for lease in leases))
        # One complete cold-start refresh and one bounded per-target
        # penetration query; concurrent misses do not multiply Redis scans.
        self.assertEqual(redis.scan_calls, 2)
        self.assertIsNone(await provider.acquire("US", "10001"))
        self.assertEqual(redis.scan_calls, 2)

    async def test_invalid_cookie_is_quarantined_and_recovers_after_ttl(self) -> None:
        clock = FakeClock()
        redis = FakeRedis(
            {
                "cookie:US:10001:1": '{"session-id": "one"}',
                "cookie:US:10001:2": '{"session-id": "two"}',
            }
        )
        provider = LegacyRedisCookieProvider(
            redis,
            quarantine_seconds=30,
            clock=clock,
        )
        first = await provider.acquire("US", "10001")
        self.assertIsNotNone(first)
        await provider.report(first, ResourceOutcome.AUTH_INVALID)
        second = await provider.acquire("US", "10001")
        self.assertIsNotNone(second)
        self.assertNotEqual(first.lease_id, second.lease_id)
        health = await provider.health()
        self.assertEqual(health.available, 1)
        self.assertEqual(health.quarantined, 1)

        clock.advance(31)
        recovered = await provider.health()
        self.assertEqual(recovered.available, 2)
        self.assertEqual(recovered.quarantined, 0)

    async def test_cache_miss_reads_new_cookie_directly(self) -> None:
        redis = FakeRedis({"cookie:US:10001:1": '{"session-id": "one"}'})
        provider = LegacyRedisCookieProvider(redis, refresh_seconds=1800)
        self.assertIsNotNone(await provider.acquire("US", "10001"))
        redis.values["cookie:US:94105:2"] = '{"session-id": "new"}'
        lease = await provider.acquire("US", "94105")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.postal_code, "94105")
        self.assertEqual(lease.cookie_header.reveal(), "session-id=new")

    async def test_cookie_routing_matches_legacy_product_and_static_routes(self) -> None:
        default = StaticCookieProvider("pool=default")
        overseas = StaticCookieProvider("pool=overseas")
        merchant = StaticCookieProvider("pool=merchant")
        marketplace_routed = RoutedCookieProvider({"JP": overseas}, default)
        factory = RequestContextFactory(
            marketplace_routed,
            StaticProxyProvider(None),
            BrowserFingerprintProvider(),
            cookie_providers_by_purpose={
                "product_hw": overseas,
                "product_hw_jp": overseas,
                "merchant": merchant,
                "rank_list": merchant,
            },
        )

        standard_us = await factory.acquire(
            purpose="product", marketplace_id="US", postal_code="10001"
        )
        standard_jp = await factory.acquire(
            purpose="search", marketplace_id="JP", postal_code="100-0001"
        )
        hardware_us = await factory.acquire(
            purpose="product_hw", marketplace_id="US", postal_code="10001"
        )
        merchant_us = await factory.acquire(
            purpose="merchant", marketplace_id="US", postal_code=None
        )
        rank_us = await factory.acquire(
            purpose="rank_list", marketplace_id="US", postal_code=None
        )

        self.assertEqual(standard_us.cookie.cookie_header.reveal(), "pool=default")
        self.assertEqual(standard_jp.cookie.cookie_header.reveal(), "pool=overseas")
        self.assertEqual(hardware_us.cookie.cookie_header.reveal(), "pool=overseas")
        self.assertEqual(merchant_us.cookie.cookie_header.reveal(), "pool=merchant")
        self.assertEqual(rank_us.cookie.cookie_header.reveal(), "pool=merchant")
        route_health = await factory.cookie_route_health()
        self.assertEqual(
            set(route_health),
            {"default", "product_hw", "product_hw_jp", "merchant", "rank_list"},
        )
        self.assertNotIn("pool=", json.dumps(route_health))

    async def test_bootstrap_routes_rank_compatibility_alias_to_merchant_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = replace(
                Settings.from_env(root),
                db_path=root / "crawler.db",
                merchant_cookie="pool=merchant",
                worker_enabled=False,
                delivery_worker_enabled=False,
            )
            application = build_application(settings)
            canonical = await application.request_context.acquire(
                purpose="rank_list",
                marketplace_id="US",
                postal_code=None,
            )
            compatibility = await application.request_context.acquire(
                purpose="rank_list_jp",
                marketplace_id="JP",
                postal_code=None,
            )

        self.assertEqual(canonical.cookie.cookie_header.reveal(), "pool=merchant")
        self.assertEqual(compatibility.cookie.cookie_header.reveal(), "pool=merchant")
        self.assertEqual(
            set(application.request_context.cookie_providers_by_purpose),
            {"merchant", "rank_list", "rank_list_jp"},
        )

    async def test_rotating_proxy_quarantines_failed_lease_without_exposure(self) -> None:
        calls: list[tuple[str, str]] = []

        async def loader(purpose: str, marketplace_id: str) -> list[str]:
            calls.append((purpose, marketplace_id))
            return [
                "http://user:password@192.0.2.1:8080",
                "http://user:password@192.0.2.2:8080",
            ]

        provider = RotatingProxyProvider(loader, quarantine_seconds=60)
        first = await provider.acquire("product", "US")
        self.assertIsNotNone(first)
        await provider.report(first, ResourceOutcome.PROXY_ERROR)
        second = await provider.acquire("product", "US")
        self.assertIsNotNone(second)
        self.assertNotEqual(first.lease_id, second.lease_id)
        self.assertNotIn("password", repr(first))
        self.assertEqual(calls, [("product", "US")])
        public = json.dumps((await provider.health()).as_public_dict())
        self.assertNotIn("192.0.2", public)
        self.assertNotIn("password", public)

    async def test_proxy_refresh_failure_is_single_flight_and_recovers_after_cooldown(self) -> None:
        clock = FakeClock()
        calls = 0

        async def loader(_purpose: str, _marketplace_id: str) -> list[str]:
            nonlocal calls
            calls += 1
            raise RuntimeError("upstream unavailable")

        provider = RotatingProxyProvider(
            loader,
            load_failure_cooldown_seconds=30,
            clock=clock,
        )
        failures = await asyncio.gather(
            *(provider.acquire("product", "US") for _ in range(20)),
            return_exceptions=True,
        )
        self.assertTrue(all(isinstance(value, RuntimeError) for value in failures))
        self.assertEqual(calls, 1)

        clock.advance(31)
        with self.assertRaises(RuntimeError):
            await provider.acquire("product", "US")
        self.assertEqual(calls, 2)

    async def test_partial_resource_acquisition_releases_cookie_without_quarantine(self) -> None:
        class TrackingCookieProvider(StaticCookieProvider):
            def __init__(self) -> None:
                super().__init__("session-id=fixture")
                self.outcomes: list[ResourceOutcome] = []

            async def report(self, lease, outcome: ResourceOutcome) -> None:
                self.outcomes.append(outcome)

        class BrokenProxyProvider(StaticProxyProvider):
            async def acquire(self, purpose: str, marketplace_id: str):
                raise RuntimeError("proxy service unavailable")

        cookie = TrackingCookieProvider()
        factory = RequestContextFactory(
            cookie,
            BrokenProxyProvider(None),
            BrowserFingerprintProvider(),
        )
        with self.assertRaisesRegex(RuntimeError, "resource acquisition failed"):
            await factory.acquire(
                purpose="product",
                marketplace_id="US",
                postal_code="10001",
            )
        self.assertEqual(cookie.outcomes, [ResourceOutcome.PARSE_ERROR])
        self.assertEqual(factory._cookie_owners, {})

    async def test_proxy_extraction_formats_runtime_credentials(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertNotIn("proxy-user", str(request.url))
            return httpx.Response(200, text="192.0.2.10:8000\r\n192.0.2.11:8001")

        loader = ProxyExtractionClient(
            "https://extract.example.test/pool?token=sensitive",
            username="proxy-user",
            password="p@ss word",
            transport=httpx.MockTransport(handler),
        )
        proxies = await loader("product", "US")
        self.assertEqual(len(proxies), 2)
        self.assertEqual(
            proxies[0],
            "http://proxy-user:p%40ss%20word@192.0.2.10:8000",
        )
        self.assertNotIn("sensitive", repr(loader))


if __name__ == "__main__":
    unittest.main()
