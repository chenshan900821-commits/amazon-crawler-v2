from __future__ import annotations

import asyncio
from dataclasses import asdict

from amazon_crawler.domain.ports import CookieHarvester


class CookieMaintenanceService:
    def __init__(
        self,
        harvesters: dict[str, CookieHarvester],
        targets: dict[str, dict[str, dict[str, int]]],
        *,
        interval_seconds: float,
    ) -> None:
        if not harvesters:
            raise ValueError("cookie maintenance requires at least one harvester")
        unknown_pools = set(targets) - set(harvesters)
        if unknown_pools:
            raise ValueError(
                f"cookie targets reference unconfigured pools: {sorted(unknown_pools)}"
            )
        self._harvesters = dict(harvesters)
        self._targets = targets
        self._interval_seconds = max(60.0, interval_seconds)
        self._stop = asyncio.Event()

    async def run_once(self) -> list[dict[str, object]]:
        reports: list[dict[str, object]] = []
        for pool_name, marketplace_targets in self._targets.items():
            harvester = self._harvesters[pool_name]
            for marketplace_id, postal_targets in marketplace_targets.items():
                for postal_code, target_count in postal_targets.items():
                    try:
                        report = await harvester.ensure_capacity(
                            marketplace_id,
                            postal_code,
                            target_count,
                        )
                        rendered: dict[str, object] = asdict(report)
                        rendered.update({"pool": pool_name, "ok": True})
                    except Exception as exc:
                        rendered = {
                            "ok": False,
                            "pool": pool_name,
                            "marketplace_id": marketplace_id,
                            "postal_code": postal_code,
                            "requested": target_count,
                            "created": 0,
                            "rejected": 0,
                            "available_after": 0,
                            "error_code": "cookie_maintenance_target_failed",
                            "error_type": type(exc).__name__,
                        }
                    reports.append(rendered)
        return reports

    async def run_forever(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()
