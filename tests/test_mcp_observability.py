from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from amazon_crawler.infra.safe_logging import emit_structured_event
from amazon_crawler.interfaces.mcp_observability import MCPObservability


def observability(
    *,
    webhook_url: str | None = None,
    webhook_transport: httpx.AsyncBaseTransport | None = None,
) -> MCPObservability:
    return MCPObservability(
        window_seconds=300,
        max_samples=100,
        alert_min_calls=2,
        alert_error_rate=0.5,
        alert_p95_latency_ms=100,
        alert_rate_limit_count=1,
        alert_concurrency_rejection_count=1,
        alert_webhook_url=webhook_url,
        alert_webhook_timeout_seconds=1,
        alert_webhook_max_attempts=3,
        webhook_transport=webhook_transport,
    )


class MCPObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_alert_transitions_are_bounded_and_deduplicated(self) -> None:
        monitor = observability()
        with patch(
            "amazon_crawler.interfaces.mcp_observability.emit_structured_event"
        ) as emitted:
            await monitor.begin("tenant-a")
            await monitor.finish(
                tenant_id="tenant-a",
                tool_name="crawler_get_job",
                status="succeeded",
                error_code=None,
                latency_ms=50,
                audit_id="audit-1",
                request_id="request-1",
            )
            await monitor.begin("tenant-a")
            await monitor.finish(
                tenant_id="tenant-a",
                tool_name="crawler_run_job",
                status="failed",
                error_code="rate_limited",
                latency_ms=200,
                audit_id="audit-2",
                request_id="request-2",
            )
            metrics = await monitor.snapshot("tenant-a")
            emitted.reset_mock()
            alerts = await monitor.evaluate_alerts(
                tenant_id="tenant-a",
                runtime_metrics=metrics,
                database_ready=True,
            )
            first_transition_count = emitted.call_count
            await monitor.evaluate_alerts(
                tenant_id="tenant-a",
                runtime_metrics=metrics,
                database_ready=True,
            )

        self.assertEqual(metrics["calls"]["total"], 2)
        self.assertEqual(metrics["calls"]["failed"], 1)
        self.assertEqual(metrics["calls"]["rate_limited"], 1)
        self.assertEqual(metrics["latency_ms"]["p95"], 200)
        self.assertLessEqual(metrics["retained_sample_limit"], 100)
        self.assertEqual(
            {alert["code"] for alert in alerts},
            {
                "mcp_error_rate_high",
                "mcp_latency_high",
                "mcp_rate_limit_high",
            },
        )
        self.assertEqual(emitted.call_count, first_transition_count)

    async def test_webhook_uses_finite_attempts_and_exponential_backoff(self) -> None:
        statuses = iter((503, 429, 204))
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(next(statuses))

        monitor = observability(
            webhook_url="https://alerts.example.test/crawler",
            webhook_transport=httpx.MockTransport(handler),
        )
        transition = {
            "state": "active",
            "code": "test_alert",
            "severity": "warning",
            "message": "test",
            "observed": 2,
            "threshold": 1,
        }
        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            await monitor._send_webhook("tenant-hash", transition)

        self.assertEqual(len(requests), 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.25, 0.5])
        body = json.loads(requests[-1].content)
        self.assertEqual(body["schema"], "amazon_crawler_alert_v1")
        self.assertEqual(body["alert"]["code"], "test_alert")


class StructuredLoggingTests(unittest.TestCase):
    def test_structured_event_redacts_sensitive_fields_and_url_query(self) -> None:
        logger = MagicMock()
        with patch(
            "amazon_crawler.infra.safe_logging._event_logger", return_value=logger
        ):
            emit_structured_event(
                "test_event",
                level=logging.WARNING,
                cookie="session-secret",
                access_token="bearer-secret",
                endpoint="https://example.test/hook?token=secret",
                error_type="TimeoutError",
            )

        rendered = logger.log.call_args.args[1]
        self.assertNotIn("session-secret", rendered)
        self.assertNotIn("bearer-secret", rendered)
        self.assertNotIn("token=secret", rendered)
        payload = json.loads(rendered)
        self.assertEqual(payload["cookie"], "<redacted>")
        self.assertEqual(payload["access_token"], "<redacted>")
        self.assertEqual(payload["error_type"], "TimeoutError")


if __name__ == "__main__":
    unittest.main()
