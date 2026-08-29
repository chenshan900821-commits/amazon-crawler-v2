from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client
from mcp.types import TextResourceContents

from amazon_crawler.bootstrap import build_application
from amazon_crawler.config import Settings
from amazon_crawler.interfaces.mcp_policy import TOOL_SCOPES
from amazon_crawler.interfaces.mcp_server import create_mcp_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TOOLS = {
    "crawler_doctor",
    "crawler_capabilities",
    "crawler_health",
    "crawler_list_jobs",
    "crawler_get_job",
    "crawler_get_results",
    "crawler_get_events",
    "crawler_get_deliveries",
    "crawler_create_job",
    "crawler_run_job",
    "crawler_pause_job",
    "crawler_resume_job",
    "crawler_cancel_job",
}


def _environment(db_path: Path, *, cookie: bool = True) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CRAWLER_")
    }
    environment.update(
        {
            "CRAWLER_DB_PATH": str(db_path),
            "CRAWLER_EVIDENCE_DIR": str(db_path.parent / "evidence"),
            "CRAWLER_RESULT_JSONL_DIR": str(db_path.parent / "jsonl"),
            "CRAWLER_WORKER_ENABLED": "false",
            "CRAWLER_DELIVERY_WORKER_ENABLED": "false",
            "CRAWLER_CAPTURE_EVIDENCE": "false",
            "CRAWLER_REQUIRE_COOKIE": "true",
        }
    )
    if cookie:
        environment["CRAWLER_AMAZON_COOKIE"] = "session-id=MCP_TEST_SECRET"
    return environment


class MCPInMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = _environment(self.root / "crawler.db")
        self.environment_patch = patch.dict(os.environ, self.environment, clear=True)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        settings = Settings.from_env(PROJECT_ROOT)
        self.application = build_application(settings)
        self.server = create_mcp_server(self.application)

    async def test_handshake_tool_schemas_and_safe_capabilities(self) -> None:
        async with Client(self.server, raise_exceptions=True) as client:
            self.assertEqual(client.server_info.name, "amazon-crawler")
            self.assertTrue(client.protocol_version)
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            self.assertEqual(set(tools), REQUIRED_TOOLS)
            self.assertEqual(set(TOOL_SCOPES), REQUIRED_TOOLS)
            create_schema = tools["crawler_create_job"].input_schema
            self.assertIn("inputs", create_schema["required"])
            self.assertEqual(create_schema["properties"]["inputs"]["maxItems"], 500)
            rendered = json.dumps(
                [tool.model_dump(mode="json", by_alias=True) for tool in listed.tools]
            ).lower()
            for forbidden in ("cookie_header", "proxy_url", "redis_url", "password"):
                self.assertNotIn(forbidden, rendered)

            doctor = await client.call_tool("crawler_doctor", {})
            self.assertFalse(doctor.is_error)
            self.assertTrue(doctor.structured_content["configuration_ready"])
            capabilities = await client.call_tool("crawler_capabilities", {})
            self.assertFalse(capabilities.is_error)
            self.assertIn("plugins", capabilities.structured_content)
            health = await client.call_tool("crawler_health", {})
            self.assertFalse(health.is_error)
            self.assertEqual(health.structured_content["status"], "ready")
            self.assertEqual(
                set(health.structured_content),
                {"ok", "status", "accepting_jobs"},
            )
            self.assertTrue(health.structured_content["accepting_jobs"])

    async def test_create_query_resource_and_confirmed_cancel(self) -> None:
        async with Client(self.server, raise_exceptions=True) as client:
            created = await client.call_tool(
                "crawler_create_job",
                {
                    "inputs": ["B000000001"],
                    "marketplace_id": "US",
                    "idempotency_key": "mcp-in-memory-1",
                },
            )
            self.assertFalse(created.is_error)
            payload = created.structured_content
            self.assertTrue(payload["created"])
            self.assertFalse(payload["completion_evidence"])
            job_id = payload["job"]["id"]

            queried = await client.call_tool("crawler_get_job", {"job_id": job_id})
            self.assertFalse(queried.is_error)
            self.assertEqual(queried.structured_content["job"]["status"], "pending")

            resource = await client.read_resource(f"crawler://jobs/{job_id}")
            self.assertEqual(len(resource.contents), 1)
            self.assertIsInstance(resource.contents[0], TextResourceContents)
            resource_job = json.loads(resource.contents[0].text)
            self.assertEqual(resource_job["id"], job_id)

            denied = await client.call_tool(
                "crawler_cancel_job",
                {"job_id": job_id, "confirm": False},
            )
            self.assertTrue(denied.is_error)
            self.assertIn("explicit user confirmation", denied.content[0].text)

            cancelled = await client.call_tool(
                "crawler_cancel_job",
                {"job_id": job_id, "confirm": True},
            )
            self.assertFalse(cancelled.is_error)
            self.assertEqual(cancelled.structured_content["job"]["status"], "cancelled")

    async def test_missing_configuration_is_actionable_and_secret_safe(self) -> None:
        missing_environment = _environment(self.root / "missing.db", cookie=False)
        with patch.dict(os.environ, missing_environment, clear=True):
            server = create_mcp_server(settings=Settings.from_env(PROJECT_ROOT))

            async with Client(server, raise_exceptions=True) as client:
                doctor = await client.call_tool("crawler_doctor", {})
                self.assertFalse(doctor.structured_content["configuration_ready"])
                created = await client.call_tool(
                    "crawler_create_job",
                    {"inputs": ["B000000001"], "marketplace_id": "US"},
                )
                self.assertTrue(created.is_error)
                message = created.content[0].text
                self.assertIn("COOKIE_SOURCE_MISSING", message)
                self.assertIn("CRAWLER_AMAZON_COOKIE", message)
                self.assertNotIn("MCP_TEST_SECRET", message)

    async def test_unexpected_server_error_does_not_disclose_secret_text(self) -> None:
        with patch.object(
            self.application.store,
            "get_job",
            side_effect=RuntimeError("cookie=session-secret; proxy=user:pass@host"),
        ):
            async with Client(self.server, raise_exceptions=True) as client:
                result = await client.call_tool(
                    "crawler_get_job",
                    {"job_id": "job_safe_error_boundary"},
                )

        self.assertTrue(result.is_error)
        rendered = json.dumps(
            result.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
        )
        self.assertIn("internal crawler operation failed", rendered)
        self.assertNotIn("session-secret", rendered)
        self.assertNotIn("user:pass", rendered)


class MCPStdioClientTests(unittest.TestCase):
    def _run(
        self,
        db_path: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "amazon_crawler.interfaces.mcp_client",
                "--db",
                str(db_path),
                *arguments,
            ],
            cwd=PROJECT_ROOT,
            env=_environment(db_path),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    def test_real_stdio_server_client_handshake_and_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "stdio.db"
            smoke = self._run(db_path, "smoke")
            self.assertEqual(smoke.returncode, 0, smoke.stderr or smoke.stdout)
            report = json.loads(smoke.stdout)
            self.assertTrue(report["ok"])
            self.assertTrue(report["checks"]["handshake"])
            self.assertEqual(report["checks"]["missing_required_tools"], [])
            self.assertTrue(report["checks"]["configuration_ready"])
            self.assertTrue(report["protocol_version"])

            created = self._run(
                db_path,
                "call",
                "crawler_create_job",
                "--arguments",
                json.dumps(
                    {
                        "inputs": ["B000000002"],
                        "marketplace_id": "US",
                        "idempotency_key": "mcp-stdio-test-1",
                    }
                ),
            )
            self.assertEqual(created.returncode, 0, created.stderr or created.stdout)
            payload = json.loads(created.stdout)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["result"]["is_error"])
            job = payload["result"]["structured_content"]["job"]
            self.assertEqual(job["status"], "pending")
            self.assertTrue(db_path.is_file())


if __name__ == "__main__":
    unittest.main()
