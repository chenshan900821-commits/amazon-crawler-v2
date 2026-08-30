from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = PROJECT_ROOT / "scripts" / "start_mcp_stdio.py"


def load_launcher():
    spec = importlib.util.spec_from_file_location("start_mcp_stdio", LAUNCHER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import MCP launcher")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class McpLauncherTests(unittest.TestCase):
    def test_launcher_uses_skill_safe_dotenv_reader(self) -> None:
        launcher = load_launcher()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_path = root / "skills" / "operate-amazon-crawler" / "scripts"
            skill_path.mkdir(parents=True)
            (root / ".env").write_text(
                "CRAWLER_DB_PATH=var/test.sqlite3\nNOT_CRAWLER_SECRET=ignored\n",
                encoding="utf-8",
            )
            (skill_path / "crawler_cli.py").write_text(
                PROJECT_ROOT.joinpath(
                    "skills", "operate-amazon-crawler", "scripts", "crawler_cli.py"
                ).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with (
                patch.object(launcher, "PROJECT_ROOT", root),
                patch.object(launcher, "SKILL_WRAPPER", skill_path / "crawler_cli.py"),
                patch.dict(os.environ, {}, clear=True),
            ):
                launcher.load_project_dotenv()
                self.assertEqual(os.environ["CRAWLER_DB_PATH"], "var/test.sqlite3")
                self.assertNotIn("NOT_CRAWLER_SECRET", os.environ)


if __name__ == "__main__":
    unittest.main()
