from __future__ import annotations

import asyncio
import uuid

from amazon_crawler.domain.errors import ConflictError
from amazon_crawler.domain.ports import StateStore
from amazon_crawler.plugins.registry import PluginRegistry


def _public_failure_details(details: dict[str, object]) -> dict[str, object]:
    allowed: dict[str, object] = {}
    for key in (
        "error_type",
        "status_code",
        "http_status",
        "marketplace_id",
        "postal_code",
    ):
        value = details.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            allowed[key] = value
    missing = details.get("missing")
    if isinstance(missing, list) and all(isinstance(value, str) for value in missing):
        allowed["missing"] = missing[:100]
    evidence = details.get("evidence")
    if isinstance(evidence, dict):
        public_evidence = {
            key: evidence[key]
            for key in ("sha256", "bytes", "captured", "artifact_ref")
            if key in evidence
            and isinstance(evidence[key], (str, int, bool))
        }
        if public_evidence:
            allowed["evidence"] = public_evidence
    return allowed


class Worker:
    def __init__(
        self,
        *,
        store: StateStore,
        plugins: PluginRegistry,
        lease_seconds: int,
        poll_seconds: float,
        concurrency: int,
        worker_id: str | None = None,
    ) -> None:
        self.store = store
        self.plugins = plugins
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.concurrency = concurrency
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:12]}"
        self._stop = asyncio.Event()

    async def process_one(self, slot: int = 0) -> bool:
        owner = f"{self.worker_id}:{slot}"
        item = await asyncio.to_thread(self.store.claim_next, owner, self.lease_seconds)
        if not item:
            return False
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(item, heartbeat_stop))
        try:
            outcome = await self.plugins.get(item.kind).execute(item)
            if outcome.ok and outcome.result:
                await asyncio.to_thread(self.store.complete_item, item, outcome.result)
            elif outcome.failure:
                await asyncio.to_thread(
                    self.store.fail_item,
                    item,
                    code=outcome.failure.code,
                    message=outcome.failure.message,
                    retryable=outcome.failure.retryable,
                    details=_public_failure_details(outcome.failure.details),
                )
            else:
                await asyncio.to_thread(
                    self.store.fail_item,
                    item,
                    code="invalid_plugin_outcome",
                    message="plugin returned neither a result nor a failure",
                    retryable=False,
                    details={"error_type": "InvalidPluginOutcome"},
                )
        except ConflictError:
            # A recovered lease belongs to another worker. Its result must not be committed.
            return True
        except Exception as exc:
            try:
                await asyncio.to_thread(
                    self.store.fail_item,
                    item,
                    code="worker_internal_error",
                    message=f"worker internal error ({type(exc).__name__})",
                    retryable=True,
                    details={"error_type": type(exc).__name__},
                )
            except ConflictError:
                pass
        finally:
            heartbeat_stop.set()
            await heartbeat_task
        return True

    async def _heartbeat_loop(self, item, stop: asyncio.Event) -> None:
        interval = max(5.0, self.lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                retained = await asyncio.to_thread(
                    self.store.heartbeat, item, self.lease_seconds
                )
                if not retained:
                    return

    async def run_until_idle(self, *, max_items: int | None = None) -> int:
        processed = 0
        while max_items is None or processed < max_items:
            if not await self.process_one(0):
                break
            processed += 1
        return processed

    async def _slot_loop(self, slot: int) -> None:
        while not self._stop.is_set():
            processed = await self.process_one(slot)
            if not processed:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

    async def run_forever(self) -> None:
        await asyncio.to_thread(self.store.recover_expired_leases)
        async with asyncio.TaskGroup() as group:
            for slot in range(self.concurrency):
                group.create_task(self._slot_loop(slot))

    def stop(self) -> None:
        self._stop.set()
