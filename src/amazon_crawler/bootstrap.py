from __future__ import annotations

from dataclasses import dataclass

from amazon_crawler.application.service import CrawlerService
from amazon_crawler.application.worker import Worker
from amazon_crawler.application.cookie_maintenance import CookieMaintenanceService
from amazon_crawler.application.delivery_worker import DeliveryWorker
from amazon_crawler.config import Settings, load_cookie_targets
from amazon_crawler.infra.evidence import EvidenceStore
from amazon_crawler.infra.cookie_harvester import (
    AmazonCookieHarvester,
    HarvesterPolicy,
    RedisCookieStore,
)
from amazon_crawler.infra.http import HttpFetcher
from amazon_crawler.infra.resources import (
    BrowserFingerprintProvider,
    LegacyRedisCookieProvider,
    ProxyExtractionClient,
    RequestContextFactory,
    RotatingProxyProvider,
    RoutedCookieProvider,
    StaticCookieProvider,
    StaticProxyProvider,
    redis_cookie_client_from_url,
)
from amazon_crawler.infra.sqlite_store import SQLiteStore
from amazon_crawler.infra.legacy_compat import (
    LegacyMySQLResultWriter,
    LegacyRedisResultBuffer,
    SQLAlchemyLegacyGateway,
)
from amazon_crawler.infra.result_sinks import (
    JsonlResultSink,
    LegacyMySQLResultSink,
    LegacyRedisResultSink,
    ResultSinkRegistry,
)
from amazon_crawler.plugins.amazon_product import AmazonProductPlugin
from amazon_crawler.plugins.amazon_collections import AmazonCollectionPlugin
from amazon_crawler.plugins.amazon_merchants import AmazonMerchantPlugin
from amazon_crawler.plugins.marketplaces import MARKETPLACE_COOKIE_ALIASES
from amazon_crawler.plugins.registry import PluginRegistry


@dataclass(slots=True)
class Application:
    settings: Settings
    store: SQLiteStore
    plugins: PluginRegistry
    service: CrawlerService
    worker: Worker
    delivery_worker: DeliveryWorker
    result_sinks: ResultSinkRegistry
    request_context: RequestContextFactory
    fetcher: HttpFetcher
    cookie_harvester: AmazonCookieHarvester | None
    cookie_harvesters: dict[str, AmazonCookieHarvester]
    cookie_maintenance: CookieMaintenanceService | None


def build_application(settings: Settings | None = None) -> Application:
    settings = settings or Settings.from_env()
    store = SQLiteStore(
        settings.db_path,
        delivery_max_attempts=settings.delivery_max_attempts,
    )
    store.initialize()

    configured_sinks = [JsonlResultSink(settings.result_jsonl_dir)]
    if settings.legacy_result_redis_url:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("legacy Redis result sink requires the redis package") from exc
        redis_client = redis.Redis.from_url(
            settings.legacy_result_redis_url,
            decode_responses=False,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
        configured_sinks.append(
            LegacyRedisResultSink(LegacyRedisResultBuffer(redis_client))
        )
    if settings.legacy_mysql_url:
        configured_sinks.append(
            LegacyMySQLResultSink(
                LegacyMySQLResultWriter(
                    SQLAlchemyLegacyGateway(settings.legacy_mysql_url)
                )
            )
        )
    result_sinks = ResultSinkRegistry(configured_sinks)

    default_cookie_provider = StaticCookieProvider(settings.amazon_cookie)
    default_cookie_client = None
    if settings.cookie_redis_url:
        default_cookie_client = redis_cookie_client_from_url(settings.cookie_redis_url)
        default_cookie_provider = LegacyRedisCookieProvider(
            default_cookie_client,
            refresh_seconds=settings.cookie_refresh_seconds,
            quarantine_seconds=settings.cookie_quarantine_seconds,
            marketplace_aliases=MARKETPLACE_COOKIE_ALIASES,
        )
    marketplace_cookie_routes = {}
    overseas_cookie_client = None
    overseas_cookie_provider = None
    if settings.cookie_redis_overseas_url:
        overseas_cookie_client = redis_cookie_client_from_url(
            settings.cookie_redis_overseas_url
        )
        overseas_cookie_provider = LegacyRedisCookieProvider(
            overseas_cookie_client,
            refresh_seconds=settings.cookie_refresh_seconds,
            quarantine_seconds=settings.cookie_quarantine_seconds,
            marketplace_aliases=MARKETPLACE_COOKIE_ALIASES,
        )
        marketplace_cookie_routes["JP"] = overseas_cookie_provider
    cookie_provider = (
        RoutedCookieProvider(marketplace_cookie_routes, default_cookie_provider)
        if marketplace_cookie_routes
        else default_cookie_provider
    )
    purpose_cookie_routes = {}
    if overseas_cookie_provider is not None:
        purpose_cookie_routes.update(
            {
                "product_hw": overseas_cookie_provider,
                "product_hw_jp": overseas_cookie_provider,
            }
        )
    if settings.merchant_cookie:
        merchant_cookie_provider = StaticCookieProvider(settings.merchant_cookie)
        purpose_cookie_routes.update(
            {
                "merchant": merchant_cookie_provider,
                "rank_list": merchant_cookie_provider,
                "rank_list_jp": merchant_cookie_provider,
            }
        )

    proxy_provider = StaticProxyProvider(settings.http_proxy)
    if settings.proxy_extract_url:
        proxy_provider = RotatingProxyProvider(
            ProxyExtractionClient(
                settings.proxy_extract_url,
                username=settings.proxy_username,
                password=settings.proxy_password,
            ),
            quarantine_seconds=settings.proxy_quarantine_seconds,
        )
    request_context = RequestContextFactory(
        cookie_provider,
        proxy_provider,
        BrowserFingerprintProvider(),
        cookie_providers_by_purpose=purpose_cookie_routes,
    )
    cookie_harvesters: dict[str, AmazonCookieHarvester] = {}
    if default_cookie_client is not None:
        default_cookie_store = RedisCookieStore(default_cookie_client)
        cookie_harvesters["default"] = AmazonCookieHarvester(
            store=default_cookie_store,
            proxy_provider=proxy_provider,
            fingerprint_provider=request_context.fingerprint_provider,
            consumer_provider=default_cookie_provider,
            policy=HarvesterPolicy(
                ttl_seconds=settings.cookie_ttl_seconds,
                max_attempts_per_cookie=settings.cookie_harvest_max_attempts,
                concurrency=settings.cookie_harvest_concurrency,
                timeout_seconds=settings.request_timeout_seconds,
                require_proxy=settings.cookie_harvest_require_proxy,
            ),
            transport_backend=settings.http_transport,
        )
    if overseas_cookie_client is not None and overseas_cookie_provider is not None:
        cookie_harvesters["overseas"] = AmazonCookieHarvester(
            store=RedisCookieStore(overseas_cookie_client),
            proxy_provider=proxy_provider,
            fingerprint_provider=request_context.fingerprint_provider,
            consumer_provider=overseas_cookie_provider,
            policy=HarvesterPolicy(
                ttl_seconds=settings.cookie_ttl_seconds,
                max_attempts_per_cookie=settings.cookie_harvest_max_attempts,
                concurrency=settings.cookie_harvest_concurrency,
                timeout_seconds=settings.request_timeout_seconds,
                require_proxy=settings.cookie_harvest_require_proxy,
            ),
            transport_backend=settings.http_transport,
        )
    cookie_harvester = cookie_harvesters.get("default")
    cookie_maintenance = None
    if settings.cookie_maintenance_enabled:
        if not cookie_harvesters:
            raise ValueError(
                "cookie maintenance requires at least one configured Cookie Redis pool"
            )
        cookie_maintenance = CookieMaintenanceService(
            cookie_harvesters,
            load_cookie_targets(settings.cookie_targets_path),
            interval_seconds=settings.cookie_maintenance_interval_seconds,
        )
    fetcher = HttpFetcher(
        timeout_seconds=settings.request_timeout_seconds,
        min_host_interval_seconds=settings.min_host_interval_seconds,
        max_response_bytes=settings.max_response_bytes,
        user_agent=settings.user_agent,
        context_factory=request_context,
        transport_backend=settings.http_transport,
    )
    evidence_store = EvidenceStore(settings.evidence_dir, settings.capture_evidence)
    product_plugins = [
            AmazonProductPlugin(
                fetcher=fetcher,
                evidence_store=evidence_store,
                require_cookie=settings.require_cookie,
                kind=kind,
            )
            for kind in (
                "amazon.product",
                "product",
                "product_jp",
                "product_hw",
                "product_hw_jp",
                "product_time",
                "product_time_jp",
            )
        ]
    collection_plugins = [
        AmazonCollectionPlugin(
            kind=kind,
            fetcher=fetcher,
            evidence_store=evidence_store,
            require_cookie=settings.require_cookie,
        )
        for kind in (
            "search",
            "search_jp",
            "search_hour",
            "search_hour_jp",
            "reviews",
            "category_asin_list",
            "asin_list_jp",
            "rank_list",
            "rank_list_jp",
        )
    ]
    merchant_plugins = [
        AmazonMerchantPlugin(
            kind=kind,
            fetcher=fetcher,
            evidence_store=evidence_store,
            require_cookie=settings.require_cookie,
        )
        for kind in ("merchant", "merchant_home", "merchant_products")
    ]
    plugins = PluginRegistry(
        [*product_plugins, *collection_plugins, *merchant_plugins]
    )
    service = CrawlerService(store, plugins, result_sinks=result_sinks.names)
    worker = Worker(
        store=store,
        plugins=plugins,
        lease_seconds=settings.lease_seconds,
        poll_seconds=settings.poll_seconds,
        concurrency=settings.worker_concurrency,
    )
    delivery_worker = DeliveryWorker(
        store=store,
        sinks=result_sinks,
        lease_seconds=settings.delivery_lease_seconds,
        poll_seconds=settings.delivery_poll_seconds,
        concurrency=settings.delivery_worker_concurrency,
    )
    return Application(
        settings,
        store,
        plugins,
        service,
        worker,
        delivery_worker,
        result_sinks,
        request_context,
        fetcher,
        cookie_harvester,
        cookie_harvesters,
        cookie_maintenance,
    )
