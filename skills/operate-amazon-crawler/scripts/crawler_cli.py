#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ALLOWED_COMMANDS = {
    "doctor",
    "capabilities",
    "run",
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
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def fail(message: str) -> None:
    print(
        json.dumps(
            {"ok": False, "error": {"type": "SkillPolicyError", "message": message}}
        )
    )
    raise SystemExit(2)


def load_project_dotenv(project_root: Path = PROJECT_ROOT) -> bool:
    """Load project CRAWLER_* values without executing the .env as shell code."""

    if os.getenv("CRAWLER_SKILL_SKIP_DOTENV", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False
    path = project_root / ".env"
    if not path.is_file():
        return False
    for line_number, source in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = source.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            fail(f"invalid .env assignment at line {line_number}")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not ENV_NAME.fullmatch(name):
            fail(f"invalid .env variable name at line {line_number}")
        if not name.startswith("CRAWLER_") or name in os.environ:
            continue
        try:
            lexer = shlex.shlex(raw_value, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            fail(f"invalid quoted .env value at line {line_number}")
        if len(tokens) > 1:
            fail(f".env values containing spaces must be quoted (line {line_number})")
        os.environ[name] = tokens[0] if tokens else ""
    return True


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
            fail(
                "external result confirmation was supplied without a legacy result sink"
            )

    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    load_project_dotenv()
    if args[0] in {"create", "run"}:
        from amazon_crawler.application.preflight import configuration_report
        from amazon_crawler.config import Settings

        report = configuration_report(Settings.from_env(project_root=PROJECT_ROOT))
        if not report["configuration_ready"]:
            report["ok"] = False
            report["error"] = {
                "type": "MissingConfiguration",
                "message": (
                    "Agent Skill 未创建任务：请按 blocking_issues 在 .env 或部署平台 "
                    "Secret 中补齐配置后重新调用 doctor；Skill 会自动重读 .env。"
                ),
            }
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            raise SystemExit(2)
    sys.argv = ["amazon-crawler", *args]
    from amazon_crawler.interfaces.cli import main as crawler_main

    crawler_main()


if __name__ == "__main__":
    main()
