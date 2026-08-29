from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from amazon_crawler.infra.safe_logging import emit_structured_event


@dataclass(frozen=True, slots=True)
class MCPCallSample:
    recorded_at: float
    tenant_id: str
    tool_name: str
    status: str
    error_code: str | None
    latency_ms: int


class MCPObservability:
    """Bounded per-process MCP metrics, alerts, and secret-safe event logging."""

    def __init__(
        self,
        *,
        window_seconds: int,
        max_samples: int,
        alert_min_calls: int,
        alert_error_rate: float,
        alert_p95_latency_ms: int,
        alert_rate_limit_count: int,
        alert_concurrency_rejection_count: int,
        alert_webhook_url: str | None,
        alert_webhook_timeout_seconds: float,
        alert_webhook_max_attempts: int,
        webhook_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.window_seconds = max(30, int(window_seconds))
        self.max_samples = max(100, int(max_samples))
        self.alert_min_calls = max(1, int(alert_min_calls))
        self.alert_error_rate = max(0.0, min(1.0, float(alert_error_rate)))
        self.alert_p95_latency_ms = max(1, int(alert_p95_latency_ms))
        self.alert_rate_limit_count = max(1, int(alert_rate_limit_count))
        self.alert_concurrency_rejection_count = max(
            1, int(alert_concurrency_rejection_count)
        )
        self.alert_webhook_url = alert_webhook_url
        self.alert_webhook_timeout_seconds = max(
            0.25, float(alert_webhook_timeout_seconds)
        )
        self.alert_webhook_max_attempts = max(1, int(alert_webhook_max_attempts))
        self._webhook_transport = webhook_transport
        self._samples: deque[MCPCallSample] = deque(maxlen=self.max_samples)
        self._inflight: Counter[str] = Counter()
        self._samples_lock = asyncio.Lock()
        self._alerts_lock = asyncio.Lock()
        self._active_alerts: dict[tuple[str, str], dict[str, Any]] = {}
        self._webhook_tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def _scope_hash(tenant_id: str | None) -> str:
        value = tenant_id or "all-tenants"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    async def begin(self, tenant_id: str) -> None:
        async with self._samples_lock:
            self._inflight[tenant_id] += 1

    async def finish(
        self,
        *,
        tenant_id: str,
        tool_name: str,
        status: str,
        error_code: str | None,
        latency_ms: int,
        audit_id: str,
        request_id: str | None,
    ) -> None:
        sample = MCPCallSample(
            recorded_at=time.monotonic(),
            tenant_id=tenant_id,
            tool_name=tool_name,
            status=status,
            error_code=error_code,
            latency_ms=max(0, int(latency_ms)),
        )
        async with self._samples_lock:
            self._samples.append(sample)
            self._inflight[tenant_id] = max(0, self._inflight[tenant_id] - 1)
            if self._inflight[tenant_id] == 0:
                del self._inflight[tenant_id]
        emit_structured_event(
            "mcp_request_completed",
            level=logging.INFO if status == "succeeded" else logging.WARNING,
            audit_id=audit_id,
            request_id=request_id,
            principal_hash=self._scope_hash(tenant_id),
            tool_name=tool_name,
            status=status,
            error_code=error_code,
            latency_ms=sample.latency_ms,
        )

    async def snapshot(self, tenant_id: str | None) -> dict[str, Any]:
        cutoff = time.monotonic() - self.window_seconds
        async with self._samples_lock:
            while self._samples and self._samples[0].recorded_at < cutoff:
                self._samples.popleft()
            selected = [
                sample
                for sample in self._samples
                if tenant_id is None or sample.tenant_id == tenant_id
            ]
            inflight = (
                sum(self._inflight.values())
                if tenant_id is None
                else self._inflight.get(tenant_id, 0)
            )
        status_counts = Counter(sample.status for sample in selected)
        error_counts = Counter(
            sample.error_code for sample in selected if sample.error_code
        )
        tool_counts = Counter(sample.tool_name for sample in selected)
        latencies = sorted(sample.latency_ms for sample in selected)

        def percentile(ratio: float) -> int | None:
            if not latencies:
                return None
            index = max(
                0, min(len(latencies) - 1, math.ceil(len(latencies) * ratio) - 1)
            )
            return latencies[index]

        return {
            "window_seconds": self.window_seconds,
            "retained_sample_limit": self.max_samples,
            "calls": {
                "total": len(selected),
                "succeeded": status_counts.get("succeeded", 0),
                "failed": status_counts.get("failed", 0),
                "inflight": inflight,
                "rate_limited": error_counts.get("rate_limited", 0),
                "concurrency_rejected": error_counts.get("concurrency_rejected", 0),
            },
            "latency_ms": {
                "p50": percentile(0.50),
                "p95": percentile(0.95),
                "max": latencies[-1] if latencies else None,
            },
            "calls_by_tool": dict(sorted(tool_counts.items())),
            "errors_by_code": dict(sorted(error_counts.items())),
            "process_local": True,
            "resets_on_restart": True,
        }

    @staticmethod
    def _alert(
        code: str,
        severity: str,
        message: str,
        observed: float | str,
        threshold: float | str,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "severity": severity,
            "message": message,
            "observed": observed,
            "threshold": threshold,
        }

    async def evaluate_alerts(
        self,
        *,
        tenant_id: str | None,
        runtime_metrics: dict[str, Any],
        database_ready: bool,
    ) -> list[dict[str, Any]]:
        calls = runtime_metrics.get("calls") or {}
        latency = runtime_metrics.get("latency_ms") or {}
        total = int(calls.get("total") or 0)
        failed = int(calls.get("failed") or 0)
        error_rate = round(failed / total, 4) if total else 0.0
        candidates: list[dict[str, Any]] = []
        if not database_ready:
            candidates.append(
                self._alert(
                    "database_unavailable",
                    "critical",
                    "the crawler state database is unavailable",
                    "unavailable",
                    "ready",
                )
            )
        if total >= self.alert_min_calls and error_rate >= self.alert_error_rate:
            candidates.append(
                self._alert(
                    "mcp_error_rate_high",
                    "warning",
                    "MCP call error rate exceeds the configured threshold",
                    error_rate,
                    self.alert_error_rate,
                )
            )
        p95 = latency.get("p95")
        if (
            total >= self.alert_min_calls
            and isinstance(p95, (int, float))
            and p95 >= self.alert_p95_latency_ms
        ):
            candidates.append(
                self._alert(
                    "mcp_latency_high",
                    "warning",
                    "MCP p95 latency exceeds the configured threshold",
                    p95,
                    self.alert_p95_latency_ms,
                )
            )
        rate_limited = int(calls.get("rate_limited") or 0)
        if rate_limited >= self.alert_rate_limit_count:
            candidates.append(
                self._alert(
                    "mcp_rate_limit_high",
                    "warning",
                    "MCP rate-limit rejections exceed the configured threshold",
                    rate_limited,
                    self.alert_rate_limit_count,
                )
            )
        rejected = int(calls.get("concurrency_rejected") or 0)
        if rejected >= self.alert_concurrency_rejection_count:
            candidates.append(
                self._alert(
                    "mcp_concurrency_saturated",
                    "warning",
                    "synchronous MCP run concurrency is repeatedly saturated",
                    rejected,
                    self.alert_concurrency_rejection_count,
                )
            )
        await self._publish_transitions(tenant_id, candidates)
        return sorted(candidates, key=lambda item: (item["severity"], item["code"]))

    async def _publish_transitions(
        self,
        tenant_id: str | None,
        current: list[dict[str, Any]],
    ) -> None:
        scope = self._scope_hash(tenant_id)
        current_by_code = {item["code"]: item for item in current}
        transitions: list[dict[str, Any]] = []
        async with self._alerts_lock:
            existing_codes = {
                code
                for (existing_scope, code) in self._active_alerts
                if existing_scope == scope
            }
            for code, alert in current_by_code.items():
                key = (scope, code)
                if key not in self._active_alerts:
                    transitions.append({"state": "active", **alert})
                self._active_alerts[key] = alert
            for code in existing_codes - set(current_by_code):
                previous = self._active_alerts.pop((scope, code))
                transitions.append({"state": "resolved", **previous})
        for transition in transitions:
            emit_structured_event(
                "crawler_alert_state_changed",
                level=(
                    logging.ERROR
                    if transition["state"] == "active"
                    and transition["severity"] == "critical"
                    else logging.WARNING
                    if transition["state"] == "active"
                    else logging.INFO
                ),
                tenant_hash=scope,
                **transition,
            )
            if self.alert_webhook_url:
                task = asyncio.create_task(self._send_webhook(scope, transition))
                self._webhook_tasks.add(task)
                task.add_done_callback(self._webhook_done)

    def _webhook_done(self, task: asyncio.Task[None]) -> None:
        self._webhook_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            emit_structured_event(
                "crawler_alert_delivery_failed",
                level=logging.ERROR,
                attempts=0,
                error_type=type(error).__name__,
            )

    async def _send_webhook(
        self,
        tenant_hash: str,
        transition: dict[str, Any],
    ) -> None:
        payload = {
            "schema": "amazon_crawler_alert_v1",
            "occurred_at": datetime.now(UTC).isoformat(),
            "tenant_hash": tenant_hash,
            "alert": transition,
        }
        error_type: str | None = None
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.alert_webhook_timeout_seconds),
            follow_redirects=False,
            transport=self._webhook_transport,
        ) as client:
            for attempt in range(1, self.alert_webhook_max_attempts + 1):
                try:
                    response = await client.post(
                        str(self.alert_webhook_url), json=payload
                    )
                    if 200 <= response.status_code < 300:
                        emit_structured_event(
                            "crawler_alert_delivered",
                            tenant_hash=tenant_hash,
                            alert_code=transition["code"],
                            state=transition["state"],
                            attempt=attempt,
                        )
                        return
                    error_type = f"HTTP_{response.status_code}"
                except httpx.HTTPError as exc:
                    error_type = type(exc).__name__
                if attempt < self.alert_webhook_max_attempts:
                    await asyncio.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
        emit_structured_event(
            "crawler_alert_delivery_failed",
            level=logging.ERROR,
            tenant_hash=tenant_hash,
            alert_code=transition["code"],
            state=transition["state"],
            attempts=self.alert_webhook_max_attempts,
            error_type=error_type,
        )
