#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ALLOWED_COMMANDS = {
    "capabilities",
    "create",
    "list",
    "show",
    "pause",
    "resume",
    "cancel",
    "results",
    "events",
    "deliveries",
    "metrics",
}


def fail(message: str) -> None:
    print(json.dumps({"ok": False, "error": {"type": "SkillPolicyError", "message": message}}))
    raise SystemExit(2)


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ALLOWED_COMMANDS:
        fail("the Agent Skill only permits safe job operations")
    if "--db" in args:
        fail("database path overrides are not permitted through the Agent Skill")
    if args[0] == "cancel":
        if "--confirm-cancel" not in args:
            fail("cancel requires explicit user confirmation and --confirm-cancel")
        args.remove("--confirm-cancel")
    external_sinks = {"legacy_mysql", "legacy_redis"}
    selected_sinks = {
        args[index + 1]
        for index, value in enumerate(args[:-1])
        if value == "--result-sink"
    }
    confirmation = "--confirm-external-result-write"
    if selected_sinks & external_sinks:
        if confirmation not in args:
            fail(
                "legacy result sinks require explicit user confirmation and "
                "--confirm-external-result-write"
            )
    if confirmation in args:
        if not selected_sinks & external_sinks:
            fail("external result confirmation was supplied without a legacy result sink")
        args.remove(confirmation)

    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    sys.argv = ["amazon-crawler", *args]
    from amazon_crawler.interfaces.cli import main as crawler_main

    crawler_main()


if __name__ == "__main__":
    main()
