from __future__ import annotations

from typing import Any, Protocol

from amazon_crawler.domain.models import (
    ClaimedDelivery,
    ClaimedItem,
    CrawlResult,
    NormalizedInput,
    PluginOutcome,
)
from amazon_crawler.domain.resources import (
    CookieLease,
    FingerprintProfile,
    HarvestReport,
    ProxyLease,
    ResourceHealth,
    ResourceOutcome,
)


class CookieProvider(Protocol):
    async def acquire(
        self, marketplace_id: str, postal_code: str | None
    ) -> CookieLease | None: ...

    async def report(self, lease: CookieLease, outcome: ResourceOutcome) -> None: ...

    async def refresh(self) -> ResourceHealth: ...

    async def health(self) -> ResourceHealth: ...


class CookieHarvester(Protocol):
    async def ensure_capacity(
        self, marketplace_id: str, postal_code: str, target_count: int
    ) -> HarvestReport: ...


class ProxyProvider(Protocol):
    async def acquire(self, purpose: str, marketplace_id: str) -> ProxyLease | None: ...

    async def report(self, lease: ProxyLease, outcome: ResourceOutcome) -> None: ...

    async def health(self) -> ResourceHealth: ...


class FingerprintProvider(Protocol):
    async def acquire(self, purpose: str, marketplace_id: str) -> FingerprintProfile: ...


class CrawlPlugin(Protocol):
    kind: str

    def normalize(
        self, source: Any, marketplace_id: str | None, postal_code: str | None
    ) -> NormalizedInput: ...

    async def execute(self, item: ClaimedItem) -> PluginOutcome: ...


class ResultSink(Protocol):
    name: str

    def publish(self, delivery: ClaimedDelivery) -> dict[str, Any]: ...


class StateStore(Protocol):
    def initialize(self) -> None: ...

    def create_job(
        self,
        *,
        kind: str,
        execution_mode: str,
        priority: int,
        inputs: list[NormalizedInput],
        options: dict[str, Any],
        idempotency_key: str,
        max_attempts: int,
    ) -> tuple[dict[str, Any], bool]: ...

    def list_jobs(self, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]: ...

    def get_job(self, job_id: str) -> dict[str, Any]: ...

    def list_results(self, job_id: str, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def claim_next(self, worker_id: str, lease_seconds: int) -> ClaimedItem | None: ...

    def heartbeat(self, item: ClaimedItem, lease_seconds: int) -> bool: ...

    def complete_item(self, item: ClaimedItem, result: CrawlResult) -> None: ...

    def fail_item(
        self,
        item: ClaimedItem,
        *,
        code: str,
        message: str,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None: ...

    def request_pause(self, job_id: str) -> dict[str, Any]: ...

    def resume(self, job_id: str) -> dict[str, Any]: ...

    def request_cancel(self, job_id: str) -> dict[str, Any]: ...

    def recover_expired_leases(self) -> int: ...

    def claim_delivery(
        self, worker_id: str, lease_seconds: int
    ) -> ClaimedDelivery | None: ...

    def heartbeat_delivery(
        self, delivery: ClaimedDelivery, lease_seconds: int
    ) -> bool: ...

    def complete_delivery(
        self, delivery: ClaimedDelivery, receipt: dict[str, Any]
    ) -> None: ...

    def fail_delivery(self, delivery: ClaimedDelivery, *, message: str) -> None: ...

    def recover_expired_delivery_leases(self) -> int: ...

    def list_deliveries(
        self, job_id: str | None = None, *, limit: int = 100
    ) -> list[dict[str, Any]]: ...
