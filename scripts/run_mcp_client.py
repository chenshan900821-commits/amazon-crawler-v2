#!/usr/bin/env python3
"""Run the project MCP client after safely loading CRAWLER_* from .env."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from start_mcp_stdio import PROJECT_ROOT, load_project_dotenv


def main() -> None:
    os.chdir(PROJECT_ROOT)
    source_dir = str(PROJECT_ROOT / "src")
    sys.path.insert(0, source_dir)
    inherited_pythonpath = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (
        source_dir
        if not inherited_pythonpath
        else source_dir + os.pathsep + inherited_pythonpath
    )
    load_project_dotenv()
    sys.argv = ["amazon-crawler-mcp-client", *sys.argv[1:]]
    from amazon_crawler.interfaces.mcp_client import main as client_main

    client_main()


if __name__ == "__main__":
    main()
