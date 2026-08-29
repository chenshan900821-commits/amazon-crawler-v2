from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcp import Client
from mcp.shared.exceptions import MCPError

from amazon_crawler.bootstrap import build_application
from amazon_crawler.config import Settings
from amazon_crawler.interfaces.mcp_policy import (
    ApprovalAuthority,
    MCPPrincipal,
    MCPRuntimeConfig,
    RunConcurrencyGate,
    StaticSHA256TokenVerifier,
    TokenBucketRateLimiter,
)
from amazon_crawler.interfaces.mcp_server import (
    _single_instance_lock,
    create_mcp_server,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def environment(db_path: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "src:.",
        "CRAWLER_DB_PATH": str(db_path),
        "CRAWLER_EVIDENCE_DIR": str(db_path.parent / "evidence"),
        "CRAWLER_RESULT_JSONL_DIR": str(db_path.parent / "jsonl"),
        "CRAWLER_WORKER_ENABLED": "false",
        "CRAWLER_DELIVERY_WORKER_ENABLED": "false",
        "CRAWLER_CAPTURE_EVIDENCE": "false",
        "CRAWLER_REQUIRE_COOKIE": "true",
        "CRAWLER_AMAZON_COOKIE": "session-id=PRODUCTION_TEST_SECRET",
    }


class MCPProductionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment_patch = patch.dict(
            os.environ,
            environment(self.root / "crawler.db"),
            clear=True,
        )
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.settings = Settings.from_env(PROJECT_ROOT)
        self.application = build_application(self.settings)
        self.local_runtime = MCPRuntimeConfig.from_env()

    async def test_jobs_are_isolated_by_tenant(self) -> None:
        tenant_b_job, _ = self.application.service.create_job(
            inputs=["B000000001"],
            marketplace_id="US",
            tenant_id="tenant-b",
            created_by="actor-b",
        )
        runtime_a = replace(
            self.local_runtime,
            local_tenant_id="tenant-a",
            local_actor_id="actor-a",
        )
        server = create_mcp_server(self.application, runtime_config=runtime_a)

        async with Client(server, raise_exceptions=True) as client:
            hidden = await client.call_tool(
                "crawler_get_job", {"job_id": tenant_b_job["id"]}
            )
            listed = await client.call_tool("crawler_list_jobs", {})

        self.assertTrue(hidden.is_error)
        self.assertEqual(listed.structured_content["jobs"], [])

    async def test_same_caller_idempotency_key_is_namespaced_by_tenant(self) -> None:
        first, first_created = self.application.service.create_job(
            inputs=["B000000001"],
            marketplace_id="US",
            tenant_id="tenant-a",
            created_by="actor-a",
            idempotency_key="shared-caller-key",
        )
        second, second_created = self.application.service.create_job(
            inputs=["B000000001"],
            marketplace_id="US",
            tenant_id="tenant-b",
            created_by="actor-b",
            idempotency_key="shared-caller-key",
        )
        self.assertTrue(first_created)
        self.assertTrue(second_created)
        self.assertNotEqual(first["id"], second["id"])

    async def test_production_cancel_requires_bound_one_time_approval(self) -> None:
        runtime = replace(
            self.local_runtime,
            production=True,
            local_tenant_id="tenant-a",
            local_actor_id="operator-a",
            local_scopes=("*",),
            approval_signing_key="approval-test-key-at-least-32-bytes",
        )
        server = create_mcp_server(self.application, runtime_config=runtime)
        principal = MCPPrincipal(
            tenant_id="tenant-a",
            actor_id="operator-a",
            client_id="stdio",
            scopes=frozenset({"*"}),
        )
        authority = ApprovalAuthority(runtime, self.application.store)

        async with Client(server, raise_exceptions=True) as client:
            created = await client.call_tool(
                "crawler_create_job",
                {"inputs": ["B000000002"], "marketplace_id": "US"},
            )
            job_id = created.structured_content["job"]["id"]
            denied = await client.call_tool(
                "crawler_cancel_job", {"job_id": job_id, "confirm": True}
            )
            receipt = authority.issue(
                principal=principal,
                action="crawler_cancel_job",
                arguments={"job_id": job_id},
            )
            accepted = await client.call_tool(
                "crawler_cancel_job",
                {"job_id": job_id, "approval_receipt": receipt},
            )
            replayed = await client.call_tool(
                "crawler_cancel_job",
                {"job_id": job_id, "approval_receipt": receipt},
            )

        self.assertTrue(denied.is_error)
        self.assertFalse(accepted.is_error)
        self.assertTrue(replayed.is_error)

    async def test_static_token_verifier_exposes_identity_without_storing_plaintext(
        self,
    ) -> None:
        raw_token = "mcp-test-bearer"
        digest = hashlib.sha256(raw_token.encode()).hexdigest()
        with patch.dict(
            os.environ,
            {
                **environment(self.root / "auth.db"),
                "CRAWLER_MCP_AUTH_MODE": "static",
                "CRAWLER_MCP_ISSUER_URL": "https://auth.example.test",
                "CRAWLER_MCP_RESOURCE_SERVER_URL": "https://crawler.example.test/mcp",
                "CRAWLER_MCP_STATIC_TOKENS_SHA256_JSON": (
                    '{"' + digest + '":{"client_id":"client-a","actor_id":"actor-a",'
                    '"tenant_id":"tenant-a","scopes":["crawler:read"]}}'
                ),
            },
            clear=True,
        ):
            runtime = MCPRuntimeConfig.from_env()
        verified = await StaticSHA256TokenVerifier(runtime.token_records).verify_token(
            raw_token
        )
        rejected = await StaticSHA256TokenVerifier(runtime.token_records).verify_token(
            "wrong"
        )
        self.assertEqual(verified.claims["tenant_id"], "tenant-a")
        self.assertEqual(verified.scopes, ["crawler:read"])
        self.assertIsNone(rejected)
        self.assertNotIn(raw_token, repr(runtime))

    async def test_job_and_audit_pages_are_bounded_and_cursor_driven(self) -> None:
        runtime = replace(
            self.local_runtime,
            local_tenant_id="tenant-page",
            local_actor_id="actor-page",
            max_page_size=2,
        )
        server = create_mcp_server(self.application, runtime_config=runtime)
        async with Client(server, raise_exceptions=True) as client:
            for number in range(3):
                created = await client.call_tool(
                    "crawler_create_job",
                    {
                        "inputs": [f"B00000000{number}"],
                        "marketplace_id": "US",
                        "idempotency_key": f"page-{number}",
                    },
                )
                self.assertFalse(created.is_error)
            first = await client.call_tool("crawler_list_jobs", {"limit": 2})
            next_cursor = first.structured_content["page"]["next_cursor"]
            second = await client.call_tool(
                "crawler_list_jobs", {"limit": 2, "cursor": next_cursor}
            )
            audits = await client.call_tool("crawler_get_audit_events", {"limit": 2})

        self.assertEqual(len(first.structured_content["jobs"]), 2)
        self.assertEqual(len(second.structured_content["jobs"]), 1)
        self.assertIsNotNone(next_cursor)
        rendered = str(audits.structured_content)
        self.assertIn("arguments_sha256", rendered)
        self.assertNotIn("B000000000", rendered)
        persisted = self.application.store.list_mcp_audit_events(
            tenant_id="tenant-page",
            limit=20,
        )
        completed_create = next(
            event for event in persisted if event["tool_name"] == "crawler_create_job"
        )
        self.assertEqual(completed_create["status"], "succeeded")
        self.assertIsNotNone(completed_create["finished_at"])

    async def test_rate_limit_and_concurrency_gate_fail_fast(self) -> None:
        limiter = TokenBucketRateLimiter(per_minute=1, burst=1)
        self.assertIsNone(await limiter.consume("tenant:actor"))
        self.assertGreater(await limiter.consume("tenant:actor"), 0)

        gate = RunConcurrencyGate(capacity=1, queue_timeout_seconds=0.01)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def first_operation():
            entered.set()
            await release.wait()
            return "first"

        first = asyncio.create_task(gate.run(first_operation))
        await entered.wait()
        with self.assertRaises(MCPError):
            await gate.run(lambda: asyncio.sleep(0, result="second"))
        release.set()
        self.assertEqual(await first, "first")

    async def test_production_sqlite_process_lock_rejects_second_server(self) -> None:
        runtime = replace(
            self.local_runtime,
            production=True,
            local_scopes=("*",),
        )
        with _single_instance_lock(self.settings, runtime):
            with self.assertRaisesRegex(SystemExit, "already owns"):
                with _single_instance_lock(self.settings, runtime):
                    self.fail(
                        "a second production Server acquired the same SQLite lock"
                    )


class MCPRuntimeConfigurationTests(unittest.TestCase):
    def test_non_loopback_http_refuses_unauthenticated_exposure(self) -> None:
        with patch.dict(
            os.environ, environment(Path("/tmp/mcp-config.db")), clear=True
        ):
            runtime = MCPRuntimeConfig.from_env()
        with self.assertRaisesRegex(ValueError, "requires authentication"):
            runtime.validate_http("0.0.0.0")

    def test_sqlite_production_refuses_false_multi_instance_configuration(self) -> None:
        configured = {
            **environment(Path("/tmp/mcp-config.db")),
            "CRAWLER_MCP_PRODUCTION": "true",
            "CRAWLER_MCP_INSTANCE_COUNT": "2",
        }
        with patch.dict(os.environ, configured, clear=True):
            with self.assertRaisesRegex(ValueError, "single-instance"):
                MCPRuntimeConfig.from_env()


if __name__ == "__main__":
    unittest.main()
