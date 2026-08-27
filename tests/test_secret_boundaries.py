from __future__ import annotations

import re
import unittest
from pathlib import Path

from amazon_crawler.domain.errors import ValidationError
from amazon_crawler.interfaces.cli import _public_error


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SecretBoundaryTests(unittest.TestCase):
    def test_cli_only_renders_messages_from_controlled_domain_errors(self) -> None:
        safe = _public_error(ValidationError("marketplace is required"))
        unsafe = _public_error(RuntimeError("cookie=session-secret"))

        self.assertEqual(safe["message"], "marketplace is required")
        self.assertEqual(unsafe["message"], "operation failed")
        self.assertNotIn("session-secret", str(unsafe))

    def test_runtime_and_skill_files_contain_no_literal_credentials(self) -> None:
        roots = (
            PROJECT_ROOT / "src",
            PROJECT_ROOT / "skills",
            PROJECT_ROOT / "scripts",
            PROJECT_ROOT / "docs",
            PROJECT_ROOT / "contracts",
        )
        files = [
            path
            for root in roots
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix
            in {".py", ".js", ".html", ".css", ".md", ".yaml", ".json"}
        ]
        files.extend(
            path
            for path in (
                PROJECT_ROOT / "README.md",
                PROJECT_ROOT / "ARCHITECTURE.md",
                PROJECT_ROOT / "ARCHITECTURE_EVALUATION.md",
                PROJECT_ROOT / "CONTROLLED_EVOLUTION.md",
                PROJECT_ROOT / "LEGACY_MIGRATION.md",
                PROJECT_ROOT / "pyproject.toml",
            )
            if path.is_file()
        )
        patterns = {
            "credential_uri": re.compile(
                r"(?:redis|mysql|https?)://[^\s/'\"]+:[^\s@/'\"]+@",
                re.IGNORECASE,
            ),
            "literal_password": re.compile(
                r"\bpassword\s*=\s*['\"][^'\"]{4,}['\"]",
                re.IGNORECASE,
            ),
            "literal_authorization": re.compile(
                r"\bauthorization\s*[:=]\s*['\"][^'\"]+['\"]",
                re.IGNORECASE,
            ),
        }
        findings: list[str] = []
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            for name, pattern in patterns.items():
                if pattern.search(text):
                    findings.append(f"{path.relative_to(PROJECT_ROOT)}:{name}")
        self.assertEqual(findings, [])

    def test_example_environment_keeps_all_secret_values_empty(self) -> None:
        values = {}
        for line in (PROJECT_ROOT / ".env.example").read_text(
            encoding="utf-8"
        ).splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        required_public_secret_keys = {
            "CRAWLER_HTTP_PROXY",
            "CRAWLER_AMAZON_COOKIE",
            "CRAWLER_MERCHANT_COOKIE",
            "CRAWLER_COOKIE_REDIS_URL",
            "CRAWLER_COOKIE_REDIS_OVERSEAS_URL",
            "CRAWLER_PROXY_EXTRACT_URL",
            "CRAWLER_PROXY_USERNAME",
            "CRAWLER_PROXY_PASSWORD",
        }
        internal_secret_keys = {
            "CRAWLER_COOKIE_REDIS_JP_URL",
            "CRAWLER_LEGACY_MYSQL_URL",
            "CRAWLER_LEGACY_RESULT_REDIS_URL",
        }
        required_public_keys = required_public_secret_keys | {
            "CRAWLER_DB_PATH",
            "CRAWLER_EVIDENCE_DIR",
            "CRAWLER_RESULT_JSONL_DIR",
            "CRAWLER_CAPTURE_EVIDENCE",
            "CRAWLER_WORKER_ENABLED",
            "CRAWLER_WORKER_CONCURRENCY",
            "CRAWLER_POLL_SECONDS",
            "CRAWLER_LEASE_SECONDS",
            "CRAWLER_DELIVERY_WORKER_ENABLED",
            "CRAWLER_DELIVERY_WORKER_CONCURRENCY",
            "CRAWLER_DELIVERY_POLL_SECONDS",
            "CRAWLER_DELIVERY_LEASE_SECONDS",
            "CRAWLER_DELIVERY_MAX_ATTEMPTS",
            "CRAWLER_REQUEST_TIMEOUT_SECONDS",
            "CRAWLER_MIN_HOST_INTERVAL_SECONDS",
            "CRAWLER_MAX_RESPONSE_BYTES",
            "CRAWLER_HTTP_TRANSPORT",
            "CRAWLER_USER_AGENT",
            "CRAWLER_REQUIRE_COOKIE",
            "CRAWLER_COOKIE_REFRESH_SECONDS",
            "CRAWLER_COOKIE_QUARANTINE_SECONDS",
            "CRAWLER_COOKIE_HARVEST_CONCURRENCY",
            "CRAWLER_COOKIE_HARVEST_MAX_ATTEMPTS",
            "CRAWLER_COOKIE_TTL_SECONDS",
            "CRAWLER_COOKIE_HARVEST_REQUIRE_PROXY",
            "CRAWLER_COOKIE_MAINTENANCE_ENABLED",
            "CRAWLER_COOKIE_TARGETS_PATH",
            "CRAWLER_COOKIE_MAINTENANCE_INTERVAL_SECONDS",
            "CRAWLER_PROXY_QUARANTINE_SECONDS",
        }
        self.assertTrue(required_public_keys.issubset(values))
        self.assertTrue(
            all(
                values.get(key, "") == ""
                for key in required_public_secret_keys | internal_secret_keys
            )
        )


if __name__ == "__main__":
    unittest.main()
