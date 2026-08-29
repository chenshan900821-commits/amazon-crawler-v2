from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.shared.exceptions import MCPError

from amazon_crawler.bootstrap import build_application
from amazon_crawler.config import Settings
from amazon_crawler.interfaces.mcp_auth0_check import check_auth0_configuration
from amazon_crawler.interfaces.mcp_policy import (
    ApprovalAuthority,
    MCPPrincipal,
    MCPRuntimeConfig,
    OAuthJWTTokenVerifier,
    RunConcurrencyGate,
    TestSHA256TokenVerifier,
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

    async def test_test_token_verifier_exposes_identity_without_storing_plaintext(
        self,
    ) -> None:
        raw_token = "mcp-test-bearer"
        with patch.dict(
            os.environ,
            {
                **environment(self.root / "auth.db"),
                "CRAWLER_MCP_AUTH_MODE": "test-token",
                "CRAWLER_MCP_ISSUER_URL": "http://127.0.0.1:9000",
                "CRAWLER_MCP_RESOURCE_SERVER_URL": "http://127.0.0.1:8000/mcp",
                "CRAWLER_MCP_TEST_TOKEN": raw_token,
                "CRAWLER_MCP_TEST_CLIENT_ID": "client-a",
                "CRAWLER_MCP_TEST_ACTOR_ID": "actor-a",
                "CRAWLER_MCP_TEST_TENANT_ID": "tenant-a",
                "CRAWLER_MCP_TEST_SCOPES": "crawler:read",
            },
            clear=True,
        ):
            runtime = MCPRuntimeConfig.from_env()
        verifier = TestSHA256TokenVerifier(runtime.test_token_records)
        verified = await verifier.verify_token(raw_token)
        rejected = await verifier.verify_token("wrong")
        self.assertEqual(verified.claims["tenant_id"], "tenant-a")
        self.assertEqual(verified.scopes, ["crawler:read"])
        self.assertIsNone(rejected)
        self.assertNotIn(raw_token, repr(runtime))

    async def test_oauth_jwt_verifier_checks_signature_issuer_audience_and_claims(
        self,
    ) -> None:
        issuer = "https://auth.example.test"
        audience = "https://crawler.example.test/mcp"
        with patch.dict(
            os.environ,
            {
                **environment(self.root / "oauth.db"),
                "CRAWLER_MCP_AUTH_MODE": "oauth",
                "CRAWLER_MCP_ISSUER_URL": issuer,
                "CRAWLER_MCP_RESOURCE_SERVER_URL": audience,
                "CRAWLER_MCP_OAUTH_JWKS_URL": f"{issuer}/.well-known/jwks.json",
            },
            clear=True,
        ):
            runtime = MCPRuntimeConfig.from_env()
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()

        class LocalJWKClient:
            def get_signing_key_from_jwt(self, token: str):
                return SimpleNamespace(key=public_key)

        verifier = OAuthJWTTokenVerifier(runtime, jwks_client=LocalJWKClient())
        now = datetime.now(UTC)
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": "auth0|operator-a",
            "client_id": "automation-a",
            "tenant_id": "tenant-a",
            "scope": "crawler:read crawler:run",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "jti": "test-jti",
        }
        valid_token = jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key", "typ": "at+jwt"},
        )
        wrong_audience_token = jwt.encode(
            {**claims, "aud": "https://other.example.test/mcp"},
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key", "typ": "at+jwt"},
        )
        id_token_shape = jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key", "typ": "JWT"},
        )

        verified = await verifier.verify_token(valid_token)
        self.assertEqual(verified.client_id, "automation-a")
        self.assertEqual(verified.subject, "auth0|operator-a")
        self.assertEqual(verified.claims["tenant_id"], "tenant-a")
        self.assertEqual(verified.scopes, ["crawler:read", "crawler:run"])
        self.assertIsNone(await verifier.verify_token(wrong_audience_token))
        self.assertIsNone(await verifier.verify_token(id_token_shape))

    async def test_auth0_profile_derives_urls_and_uses_subject_tenancy(self) -> None:
        audience = "https://crawler.example.test/mcp"
        with patch.dict(
            os.environ,
            {
                **environment(self.root / "auth0.db"),
                "CRAWLER_MCP_AUTH_MODE": "oauth",
                "CRAWLER_MCP_OAUTH_PROVIDER": "auth0",
                "CRAWLER_MCP_AUTH0_DOMAIN": "tenant.us.auth0.com",
                "CRAWLER_MCP_RESOURCE_SERVER_URL": audience,
            },
            clear=True,
        ):
            runtime = MCPRuntimeConfig.from_env()

        self.assertEqual(runtime.issuer_url, "https://tenant.us.auth0.com/")
        self.assertEqual(
            runtime.oauth_jwks_url,
            "https://tenant.us.auth0.com/.well-known/jwks.json",
        )
        self.assertEqual(runtime.oauth_tenant_mode, "subject")
        self.assertEqual(runtime.oauth_permissions_claim, "permissions")

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()

        class LocalJWKClient:
            def get_signing_key_from_jwt(self, token: str):
                return SimpleNamespace(key=public_key)

        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "iss": runtime.issuer_url,
                "aud": audience,
                "sub": "auth0|operator-a",
                "client_id": "mcp-client-a",
                "scope": "crawler:read",
                "permissions": ["crawler:read", "crawler:run"],
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "jti": "auth0-test-jti",
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "auth0-test-key", "typ": "at+jwt"},
        )
        verified = await OAuthJWTTokenVerifier(
            runtime, jwks_client=LocalJWKClient()
        ).verify_token(token)

        self.assertIsNotNone(verified)
        self.assertEqual(verified.claims["tenant_id"], "auth0|operator-a")
        self.assertEqual(verified.scopes, ["crawler:read", "crawler:run"])

    async def test_auth0_public_configuration_preflight(self) -> None:
        audience = "https://crawler.example.test/mcp"
        issuer = "https://tenant.us.auth0.com/"
        jwks_url = f"{issuer}.well-known/jwks.json"
        with patch.dict(
            os.environ,
            {
                **environment(self.root / "auth0-check.db"),
                "CRAWLER_MCP_AUTH_MODE": "oauth",
                "CRAWLER_MCP_OAUTH_PROVIDER": "auth0",
                "CRAWLER_MCP_AUTH0_DOMAIN": "tenant.us.auth0.com",
                "CRAWLER_MCP_RESOURCE_SERVER_URL": audience,
            },
            clear=True,
        ):
            runtime = MCPRuntimeConfig.from_env()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.well-known/oauth-authorization-server":
                return httpx.Response(
                    200,
                    json={
                        "issuer": issuer,
                        "jwks_uri": jwks_url,
                        "authorization_endpoint": f"{issuer}authorize",
                        "token_endpoint": f"{issuer}oauth/token",
                        "code_challenge_methods_supported": ["S256"],
                    },
                )
            if request.url.path == "/.well-known/jwks.json":
                return httpx.Response(
                    200,
                    json={
                        "keys": [
                            {
                                "kid": "auth0-test-key",
                                "kty": "RSA",
                                "use": "sig",
                                "alg": "RS256",
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await check_auth0_configuration(runtime, client=client)

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "auth0")
        self.assertEqual(len(result["checks"]), 6)
        self.assertEqual(len(result["manual_dashboard_checks"]), 5)

    async def test_job_pages_are_bounded_and_audits_remain_internal(self) -> None:
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

        self.assertEqual(len(first.structured_content["jobs"]), 2)
        self.assertEqual(len(second.structured_content["jobs"]), 1)
        self.assertIsNotNone(next_cursor)
        persisted = self.application.store.list_mcp_audit_events(
            tenant_id="tenant-page",
            limit=20,
        )
        rendered = str(persisted)
        self.assertIn("arguments_sha256", rendered)
        self.assertNotIn("B000000000", rendered)
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
        with self.assertRaisesRegex(ValueError, "requires OAuth authentication"):
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

    def test_test_token_mode_is_loopback_only_and_forbidden_in_production(self) -> None:
        configured = {
            **environment(Path("/tmp/mcp-test-token.db")),
            "CRAWLER_MCP_AUTH_MODE": "test-token",
            "CRAWLER_MCP_ISSUER_URL": "http://127.0.0.1:9000",
            "CRAWLER_MCP_RESOURCE_SERVER_URL": "http://127.0.0.1:8000/mcp",
            "CRAWLER_MCP_TEST_TOKEN": "local-test-token",
        }
        with patch.dict(os.environ, configured, clear=True):
            runtime = MCPRuntimeConfig.from_env()
        runtime.validate_http("127.0.0.1")
        with self.assertRaisesRegex(ValueError, "requires OAuth"):
            runtime.validate_http("0.0.0.0")
        with (
            patch.dict(
                os.environ,
                {**configured, "CRAWLER_MCP_PRODUCTION": "true"},
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "forbidden in production"),
        ):
            MCPRuntimeConfig.from_env()

    def test_auth0_profile_refuses_non_rfc9068_configuration(self) -> None:
        configured = {
            **environment(Path("/tmp/mcp-auth0-config.db")),
            "CRAWLER_MCP_AUTH_MODE": "oauth",
            "CRAWLER_MCP_OAUTH_PROVIDER": "auth0",
            "CRAWLER_MCP_AUTH0_DOMAIN": "tenant.us.auth0.com",
            "CRAWLER_MCP_RESOURCE_SERVER_URL": "https://crawler.example.test/mcp",
            "CRAWLER_MCP_OAUTH_REQUIRE_AT_JWT": "false",
        }
        with (
            patch.dict(os.environ, configured, clear=True),
            self.assertRaisesRegex(ValueError, "Auth0 P0 profile"),
        ):
            MCPRuntimeConfig.from_env()

    def test_alert_webhook_is_secret_and_requires_safe_transport(self) -> None:
        unsafe = {
            **environment(Path("/tmp/mcp-alert-config.db")),
            "CRAWLER_MCP_ALERT_WEBHOOK_URL": "http://alerts.example.test/hook",
        }
        with (
            patch.dict(os.environ, unsafe, clear=True),
            self.assertRaisesRegex(ValueError, "ALERT_WEBHOOK_URL"),
        ):
            MCPRuntimeConfig.from_env()

        local_url = "http://127.0.0.1:9999/hook?key=ALERT_TEST_SECRET"
        with patch.dict(
            os.environ,
            {**unsafe, "CRAWLER_MCP_ALERT_WEBHOOK_URL": local_url},
            clear=True,
        ):
            runtime = MCPRuntimeConfig.from_env()
        self.assertEqual(runtime.alert_webhook_url, local_url)
        self.assertNotIn("ALERT_TEST_SECRET", repr(runtime))

    def test_internal_observability_is_disabled_by_default_and_requires_digest(
        self,
    ) -> None:
        base = environment(Path("/tmp/mcp-internal-observability.db"))
        with patch.dict(os.environ, base, clear=True):
            runtime = MCPRuntimeConfig.from_env()
        self.assertFalse(runtime.internal_observability_enabled)

        with (
            patch.dict(
                os.environ,
                {
                    **base,
                    "CRAWLER_MCP_INTERNAL_OBSERVABILITY_ENABLED": "true",
                },
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "TOKEN_SHA256"),
        ):
            MCPRuntimeConfig.from_env()

        digest = "a" * 64
        with patch.dict(
            os.environ,
            {
                **base,
                "CRAWLER_MCP_INTERNAL_OBSERVABILITY_ENABLED": "true",
                "CRAWLER_MCP_INTERNAL_OBSERVABILITY_TOKEN_SHA256": digest,
            },
            clear=True,
        ):
            runtime = MCPRuntimeConfig.from_env()
        self.assertTrue(runtime.internal_observability_enabled)
        self.assertNotIn(digest, repr(runtime))


if __name__ == "__main__":
    unittest.main()
