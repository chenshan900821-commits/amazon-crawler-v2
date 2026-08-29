from __future__ import annotations

import asyncio
import logging
import uuid

from amazon_crawler.domain.errors import ConflictError
from amazon_crawler.domain.models import ClaimedDelivery
from amazon_crawler.domain.ports import StateStore
from amazon_crawler.infra.result_sinks import ResultSinkRegistry
from amazon_crawler.infra.safe_logging import emit_structured_event


class DeliveryWorker:
    """Drains the durable result outbox without changing crawl completion."""

    def __init__(
        self,
        *,
        store: StateStore,
        sinks: ResultSinkRegistry,
        lease_seconds: int,
        poll_seconds: float,
        concurrency: int,
        worker_id: str | None = None,
    ) -> None:
        self.store = store
        self.sinks = sinks
        self.lease_seconds = max(15, int(lease_seconds))
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.concurrency = max(1, int(concurrency))
        self.worker_id = worker_id or f"delivery_{uuid.uuid4().hex[:12]}"
        self._stop = asyncio.Event()

    async def process_one(
        self,
        slot: int = 0,
        *,
        job_id: str | None = None,
    ) -> bool:
        owner = f"{self.worker_id}:{slot}"
        delivery = await asyncio.to_thread(
            self.store.claim_delivery,
            owner,
            self.lease_seconds,
            job_id,
        )
        if delivery is None:
            return False
        emit_structured_event(
            "result_delivery_claimed",
            worker_id=self.worker_id,
            slot=slot,
            job_id=delivery.job_id,
            delivery_id=delivery.id,
            sink=delivery.sink_name,
            attempt=delivery.attempts,
            max_attempts=delivery.max_attempts,
        )
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(delivery, heartbeat_stop)
        )
        try:
            sink = self.sinks.get(delivery.sink_name)
            receipt = await asyncio.to_thread(sink.publish, delivery)
            await asyncio.to_thread(self.store.complete_delivery, delivery, receipt)
            emit_structured_event(
                "result_delivery_completed",
                worker_id=self.worker_id,
                job_id=delivery.job_id,
                delivery_id=delivery.id,
                sink=delivery.sink_name,
                attempt=delivery.attempts,
            )
        except ConflictError:
            emit_structured_event(
                "result_delivery_lease_lost",
                level=logging.WARNING,
                worker_id=self.worker_id,
                job_id=delivery.job_id,
                delivery_id=delivery.id,
            )
            return True
        except Exception as exc:
            emit_structured_event(
                "result_delivery_failed",
                level=logging.ERROR,
                worker_id=self.worker_id,
                job_id=delivery.job_id,
                delivery_id=delivery.id,
                sink=delivery.sink_name,
                attempt=delivery.attempts,
                max_attempts=delivery.max_attempts,
                error_type=type(exc).__name__,
            )
            try:
                await asyncio.to_thread(
                    self.store.fail_delivery,
                    delivery,
                    message=f"result sink delivery failed ({type(exc).__name__})",
                )
            except ConflictError:
                pass
        finally:
            heartbeat_stop.set()
            await heartbeat_task
        return True

    async def _heartbeat_loop(
        self, delivery: ClaimedDelivery, stop: asyncio.Event
    ) -> None:
        interval = max(5.0, self.lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                retained = await asyncio.to_thread(
                    self.store.heartbeat_delivery,
                    delivery,
                    self.lease_seconds,
                )
                if not retained:
                    return

    async def run_until_idle(
        self,
        *,
        max_deliveries: int | None = None,
        job_id: str | None = None,
    ) -> int:
        processed = 0
        while max_deliveries is None or processed < max_deliveries:
            if not await self.process_one(0, job_id=job_id):
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
        await asyncio.to_thread(self.store.recover_expired_delivery_leases)
        emit_structured_event(
            "result_delivery_worker_started",
            worker_id=self.worker_id,
            concurrency=self.concurrency,
            lease_seconds=self.lease_seconds,
        )
        try:
            async with asyncio.TaskGroup() as group:
                for slot in range(self.concurrency):
                    group.create_task(self._slot_loop(slot))
        finally:
            emit_structured_event(
                "result_delivery_worker_stopped",
                worker_id=self.worker_id,
            )

    def stop(self) -> None:
        self._stop.set()
