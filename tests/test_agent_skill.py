from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "skills" / "operate-amazon-crawler"
DISCOVERED_SKILL_ROOT = PROJECT_ROOT / ".agents" / "skills" / "operate-amazon-crawler"
WRAPPER = SKILL_ROOT / "scripts" / "crawler_cli.py"


class AgentSkillTests(unittest.TestCase):
    def _environment(
        self,
        db_path: Path,
        overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CRAWLER_")
        }
        environment["CRAWLER_DB_PATH"] = str(db_path)
        environment["CRAWLER_WORKER_ENABLED"] = "false"
        environment["CRAWLER_AMAZON_COOKIE"] = (
            "session-id=AGENT_SKILL_TEST_SECRET"
        )
        environment.update(overrides or {})
        return environment

    def _run(
        self,
        db_path: Path,
        *args: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WRAPPER), *args],
            cwd=PROJECT_ROOT,
            env=self._environment(db_path, environment),
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )

    def test_skill_structure_and_interface_metadata_are_complete(self) -> None:
        required = (
            SKILL_ROOT / "SKILL.md",
            SKILL_ROOT / "agents" / "openai.yaml",
            SKILL_ROOT / "references" / "input-contracts.md",
            SKILL_ROOT / "references" / "operations.md",
            WRAPPER,
        )
        for path in required:
            self.assertTrue(path.is_file(), path)
        self.assertTrue(DISCOVERED_SKILL_ROOT.is_dir())
        self.assertEqual(DISCOVERED_SKILL_ROOT.resolve(), SKILL_ROOT.resolve())
        self.assertTrue((DISCOVERED_SKILL_ROOT / "SKILL.md").is_file())
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        self.assertTrue(skill.startswith("---\nname: operate-amazon-crawler\n"))
        self.assertIn("description:", skill.split("---", 2)[1])
        self.assertIn("deployment Worker prerequisite", skill)
        self.assertIn("Always run `doctor` before `create`", skill)
        self.assertIn("data.row_count", skill)
        self.assertIn("$operate-amazon-crawler", metadata)

    def test_wrapper_allowlist_excludes_deployment_and_secret_operations(self) -> None:
        spec = importlib.util.spec_from_file_location("crawler_skill_wrapper", WRAPPER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        forbidden = {
            "serve",
            "worker",
            "cookie-fill",
            "cookie-maintain",
            "legacy-import",
            "legacy-export",
            "legacy-sync-state",
        }
        self.assertTrue(forbidden.isdisjoint(module.ALLOWED_COMMANDS))

    def test_wrapper_allowlist_matches_published_agent_capabilities(self) -> None:
        spec = importlib.util.spec_from_file_location("crawler_skill_wrapper", WRAPPER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tempdir:
            payload = json.loads(
                self._run(
                    Path(tempdir) / "skill.db",
                    "capabilities",
                ).stdout
            )
        published = set(payload["agent_safe_operations"])
        published.remove("cancel_with_confirmation")
        published.add("cancel")
        self.assertEqual(set(module.ALLOWED_COMMANDS), published)

    def test_wrapper_operates_jobs_but_requires_cancel_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "skill.db"
            created = self._run(
                db_path,
                "create",
                "B000000001",
                "--marketplace",
                "US",
            )
            self.assertEqual(created.returncode, 0, created.stderr or created.stdout)
            payload = json.loads(created.stdout)
            job_id = payload["job"]["id"]

            denied = self._run(db_path, "cancel", job_id)
            self.assertEqual(denied.returncode, 2)
            self.assertIn("explicit user confirmation", denied.stdout)

            cancelled = self._run(
                db_path,
                "cancel",
                job_id,
                "--confirm-cancel",
            )
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr or cancelled.stdout)
            self.assertEqual(json.loads(cancelled.stdout)["job"]["status"], "cancelled")

            rendered = "\n".join(
                (created.stdout, denied.stdout, cancelled.stdout)
            ).lower()
            for secret_name in ("cookie_header", "proxy_url", "authorization"):
                self.assertNotIn(secret_name, rendered)
            self.assertNotIn("AGENT_SKILL_TEST_SECRET", rendered)

    def test_doctor_explains_missing_cookie_and_create_refuses_to_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "skill.db"
            missing_cookie = {
                "CRAWLER_AMAZON_COOKIE": "",
                "CRAWLER_REQUIRE_COOKIE": "true",
            }
            checked = self._run(
                db_path,
                "doctor",
                environment=missing_cookie,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr or checked.stdout)
            report = json.loads(checked.stdout)
            self.assertFalse(report["configuration_ready"])
            self.assertEqual(
                report["checks"]["cookie"]["selected_source"],
                "none",
            )
            self.assertIn(
                "COOKIE_SOURCE_MISSING",
                {issue["code"] for issue in report["blocking_issues"]},
            )
            self.assertIn("CRAWLER_COOKIE_REDIS_URL", checked.stdout)
            self.assertIn("CRAWLER_AMAZON_COOKIE", checked.stdout)

            refused = self._run(
                db_path,
                "create",
                "B000000001",
                "--marketplace",
                "US",
                environment=missing_cookie,
            )
            self.assertEqual(refused.returncode, 2)
            refused_report = json.loads(refused.stdout)
            self.assertEqual(
                refused_report["error"]["type"],
                "MissingConfiguration",
            )
            self.assertFalse(db_path.exists())

    def test_doctor_distinguishes_proxy_modes_without_disclosing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "skill.db"
            direct = self._run(db_path, "doctor")
            direct_report = json.loads(direct.stdout)
            self.assertTrue(direct_report["configuration_ready"])
            self.assertEqual(
                direct_report["checks"]["proxy"]["selected_mode"],
                "direct",
            )
            self.assertEqual(
                direct_report["checks"]["worker"]["runtime_status"],
                "not_verified",
            )

            extract_secret = "https://proxy.example/extract?token=DO_NOT_DISCLOSE"
            fixed_secret = "http://fixed-user:FIXED_SECRET@proxy.example:8080"
            incomplete = self._run(
                db_path,
                "doctor",
                environment={
                    "CRAWLER_PROXY_EXTRACT_URL": extract_secret,
                    "CRAWLER_PROXY_USERNAME": "dynamic-user",
                    "CRAWLER_PROXY_PASSWORD": "",
                    "CRAWLER_HTTP_PROXY": fixed_secret,
                },
            )
            report = json.loads(incomplete.stdout)
            self.assertFalse(report["configuration_ready"])
            self.assertEqual(
                report["checks"]["proxy"]["selected_mode"],
                "dynamic_extraction_api",
            )
            issue_codes = {issue["code"] for issue in report["blocking_issues"]}
            warning_codes = {warning["code"] for warning in report["warnings"]}
            self.assertIn("PROXY_CREDENTIALS_INCOMPLETE", issue_codes)
            self.assertIn("DYNAMIC_PROXY_TAKES_PRECEDENCE", warning_codes)
            self.assertNotIn(extract_secret, incomplete.stdout)
            self.assertNotIn(fixed_secret, incomplete.stdout)
            self.assertNotIn("FIXED_SECRET", incomplete.stdout)

    def test_wrapper_rejects_cookie_and_legacy_write_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "skill.db"
            for command in ("cookie-fill", "legacy-export", "worker"):
                result = self._run(db_path, command)
                self.assertEqual(result.returncode, 2, command)
                self.assertIn("only permits safe job operations", result.stdout)

    def test_wrapper_allows_named_local_sink_and_guards_legacy_sinks(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "skill.db"
            local = self._run(
                db_path,
                "create",
                "B000000001",
                "--marketplace",
                "US",
                "--result-sink",
                "sqlite",
                "--result-sink",
                "jsonl",
            )
            self.assertEqual(local.returncode, 0, local.stderr or local.stdout)
            payload = json.loads(local.stdout)
            self.assertEqual(
                payload["job"]["options"]["result_sinks"],
                ["jsonl", "sqlite"],
            )
            deliveries = self._run(
                db_path,
                "deliveries",
                payload["job"]["id"],
            )
            self.assertEqual(deliveries.returncode, 0, deliveries.stderr or deliveries.stdout)

            denied = self._run(
                db_path,
                "create",
                "B000000002",
                "--marketplace",
                "US",
                "--result-sink",
                "legacy_mysql",
            )
            self.assertEqual(denied.returncode, 2)
            self.assertIn("explicit user confirmation", denied.stdout)


if __name__ == "__main__":
    unittest.main()
