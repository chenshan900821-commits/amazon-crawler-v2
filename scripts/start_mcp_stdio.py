#!/usr/bin/env python3
"""Start the project-local MCP server after safely loading CRAWLER_* from .env."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_WRAPPER = (
    PROJECT_ROOT
    / "skills"
    / "operate-amazon-crawler"
    / "scripts"
    / "crawler_cli.py"
)


def load_project_dotenv() -> None:
    spec = importlib.util.spec_from_file_location("amazon_crawler_skill_cli", SKILL_WRAPPER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load safe dotenv reader: {SKILL_WRAPPER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_project_dotenv(PROJECT_ROOT)


def main() -> None:
    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    load_project_dotenv()
    sys.argv = ["amazon-crawler-mcp", "--transport", "stdio"]
    from amazon_crawler.interfaces.mcp_server import main as mcp_main

    mcp_main()


if __name__ == "__main__":
    main()
