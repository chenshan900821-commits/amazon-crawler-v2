from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amazon_crawler.config import Settings
from amazon_crawler.infra.legacy_runtime_config import load_legacy_runtime_config


FIXTURE_CONFIG = '''
import random
MYSQL_URL = "mysql+pymysql://user:mysql-secret@127.0.0.1:3306/db"
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_PASSWORD = "redis-secret"
REDIS_DB = 3
REDIS_COOKIE_IP_PORTS = "8.8.8.8:6380"
REDIS_COOKIE_PASS = "cookie-secret"
REDIS_COOKIE_DB = 7
REDIS_COOKIE_DB_HW = 8
PROXY_EXTRACT_API_QG = "https://proxy.example.invalid/extract?token=proxy-secret"
username_qg = "proxy-user-secret"
password_qg = "proxy-password-secret"
PROXY_EXTRACT_API_QGHW = "https://overseas-proxy.example.invalid/extract?token=overseas-proxy-secret"
username_qghw = "overseas-proxy-user-secret"
password_qghw = "overseas-proxy-password-secret"
MERCHANT_COOKIE = {"session-id": "merchant-secret", "session-id-time": random.randint(1, 9)}
UNSAFE_VALUE = execute_arbitrary_code()
'''


class LegacyRuntimeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.path = self.root / "config.py"
        self.path.write_text(FIXTURE_CONFIG, encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_ast_loader_exposes_only_boundary_metadata(self) -> None:
        config = load_legacy_runtime_config(
            self.path,
            allow_external_cookie_read=True,
            allow_external_proxy_api=True,
        )
        report = config.public_report()
        self.assertEqual(report["target_scopes"]["mysql"], "loopback")
        self.assertEqual(report["target_scopes"]["result_redis"], "loopback")
        self.assertEqual(report["target_scopes"]["cookie_redis"], "public_network")
        self.assertTrue(report["configured"]["merchant_cookie"])
        self.assertEqual(report["proxy_route"], "qg")
        self.assertFalse(report["imports_executed"])
        rendered = repr(config) + str(report)
        for secret in ("mysql-secret", "redis-secret", "cookie-secret", "proxy-secret", "proxy-user-secret", "proxy-password-secret", "merchant-secret"):
            self.assertNotIn(secret, rendered)

    def test_external_cookie_and_proxy_require_separate_runtime_consent(self) -> None:
        config = load_legacy_runtime_config(self.path)
        self.assertIsNone(config.cookie_redis_url)
        self.assertIsNone(config.cookie_redis_overseas_url)
        self.assertIsNone(config.proxy_extract_url)
        self.assertIsNotNone(config.mysql_url)
        self.assertIsNotNone(config.result_redis_url)

    def test_explicit_proxy_route_selects_an_allowlisted_legacy_route(self) -> None:
        config = load_legacy_runtime_config(
            self.path,
            allow_external_proxy_api=True,
            proxy_route="qghw",
        )
        self.assertEqual(config.proxy_route, "qghw")
        self.assertEqual(config.target_scopes["proxy_extract"], "hostname")
        self.assertIn("overseas-proxy.example.invalid", str(config.proxy_extract_url))
        self.assertNotIn("overseas-proxy-secret", repr(config))

    def test_unknown_proxy_route_is_rejected_without_fallback(self) -> None:
        with self.assertRaisesRegex(ValueError, "legacy proxy route"):
            load_legacy_runtime_config(self.path, proxy_route="unknown")

    def test_non_loopback_write_targets_are_rejected(self) -> None:
        remote = self.root / "remote.py"
        remote.write_text(
            FIXTURE_CONFIG.replace("127.0.0.1:3306", "198.51.100.5:3306"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "loopback"):
            load_legacy_runtime_config(remote)

    def test_settings_can_use_legacy_values_without_environment_secret_copy(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CRAWLER_USE_LEGACY_CONFIG": "1",
                "CRAWLER_LEGACY_CONFIG_PATH": str(self.path),
                "CRAWLER_ALLOW_EXTERNAL_COOKIE_READ": "1",
                "CRAWLER_ALLOW_EXTERNAL_PROXY_API": "1",
                "CRAWLER_LEGACY_PROXY_ROUTE": "qghw",
            },
            clear=True,
        ):
            settings = Settings.from_env(self.root)
        self.assertTrue(settings.legacy_config_loaded)
        self.assertEqual(settings.legacy_target_scopes["mysql"], "loopback")
        self.assertIsNotNone(settings.legacy_mysql_url)
        self.assertIsNotNone(settings.cookie_redis_url)
        rendered = repr(settings)
        for secret in ("mysql-secret", "redis-secret", "cookie-secret", "proxy-secret", "proxy-user-secret", "proxy-password-secret", "merchant-secret"):
            self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
