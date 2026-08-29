from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from amazon_crawler.bootstrap import build_application
from amazon_crawler.config import Settings
from amazon_crawler.interfaces.mcp_policy import MCPRuntimeConfig
from amazon_crawler.interfaces.mcp_server import create_mcp_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MCPInternalObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CRAWLER_")
        }
        environment.update(
            {
                "CRAWLER_DB_PATH": str(root / "crawler.db"),
                "CRAWLER_EVIDENCE_DIR": str(root / "evidence"),
                "CRAWLER_RESULT_JSONL_DIR": str(root / "jsonl"),
                "CRAWLER_WORKER_ENABLED": "false",
                "CRAWLER_DELIVERY_WORKER_ENABLED": "false",
                "CRAWLER_CAPTURE_EVIDENCE": "false",
                "CRAWLER_REQUIRE_COOKIE": "false",
            }
        )
        self.environment_patch = patch.dict(os.environ, environment, clear=True)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.application = build_application(Settings.from_env(PROJECT_ROOT))
        self.runtime = MCPRuntimeConfig.from_env()

    def client(self, runtime: MCPRuntimeConfig) -> TestClient:
        server = create_mcp_server(self.application, runtime_config=runtime)
        return TestClient(server.streamable_http_app(stateless_http=True))

    def test_public_health_is_minimal_and_internal_routes_default_to_not_found(
        self,
    ) -> None:
        with self.client(self.runtime) as client:
            live = client.get("/health/live")
            ready = client.get("/health/ready")
            internal = client.get("/internal/mcp/observability")

        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json(), {"status": "live"})
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json(), {"status": "ready"})
        self.assertEqual(internal.status_code, 404)

    def test_internal_metrics_and_audit_require_a_separate_bearer_token(self) -> None:
        token = "internal-ops-token-with-sufficient-entropy"
        runtime = replace(
            self.runtime,
            internal_observability_enabled=True,
            internal_observability_token_sha256=hashlib.sha256(
                token.encode("utf-8")
            ).hexdigest(),
        )
        self.application.store.start_mcp_audit(
            audit_id="audit-internal-test",
            tenant_id="tenant-a",
            actor_id="actor-a",
            client_id="client-a",
            tool_name="crawler_health",
            arguments_sha256=hashlib.sha256(b"{}").hexdigest(),
            request_id="request-a",
        )
        self.application.store.finish_mcp_audit(
            "audit-internal-test",
            status="succeeded",
            latency_ms=12,
        )
        headers = {"Authorization": f"Bearer {token}"}

        with self.client(runtime) as client:
            missing = client.get("/internal/mcp/observability")
            wrong = client.get(
                "/internal/mcp/observability",
                headers={"Authorization": "Bearer wrong"},
            )
            observed = client.get("/internal/mcp/observability", headers=headers)
            audited = client.get(
                "/internal/mcp/audit?tenant_id=tenant-a&limit=10",
                headers=headers,
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(observed.status_code, 200)
        self.assertEqual(observed.headers["cache-control"], "no-store")
        payload = observed.json()
        self.assertEqual(payload["schema"], "amazon_crawler_mcp_observability_v1")
        self.assertIn("runtime_metrics", payload)
        self.assertIn("alerts", payload)
        self.assertIn("limits", payload)
        self.assertNotIn("durable_metrics", payload)
        self.assertNotIn("upstream", payload)
        self.assertEqual(audited.status_code, 200)
        self.assertEqual(audited.json()["scope"]["tenant_id"], "tenant-a")
        self.assertEqual(len(audited.json()["audit_events"]), 1)
        self.assertEqual(audited.json()["audit_events"][0]["id"], "audit-internal-test")


if __name__ == "__main__":
    unittest.main()
