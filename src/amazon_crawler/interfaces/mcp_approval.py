from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from amazon_crawler.bootstrap import build_application
from amazon_crawler.config import Settings
from amazon_crawler.interfaces.mcp_policy import (
    ApprovalAuthority,
    MCPPrincipal,
    MCPRuntimeConfig,
    arguments_sha256,
)

APPROVABLE_ACTIONS = (
    "crawler_create_job",
    "crawler_run_job",
    "crawler_cancel_job",
)


def _arguments(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("--arguments must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("--arguments must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amazon-crawler-mcp-approval",
        description="Issue a short-lived, one-time MCP approval receipt.",
    )
    parser.add_argument("action", choices=APPROVABLE_ACTIONS)
    parser.add_argument(
        "--arguments", required=True, help="exact normalized action JSON"
    )
    parser.add_argument("--tenant")
    parser.add_argument("--actor")
    parser.add_argument("--client-id", default="operator-cli")
    parser.add_argument("--ttl", type=int)
    parser.add_argument("--db", type=Path)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="confirm that the operator reviewed this exact action",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if not args.confirm:
        raise SystemExit("approval issuance requires --confirm")
    runtime = MCPRuntimeConfig.from_env()
    arguments = _arguments(args.arguments)
    settings = Settings.from_env()
    if args.db is not None:
        settings = replace(settings, db_path=args.db.expanduser().resolve())
    app = build_application(settings)
    principal = MCPPrincipal(
        tenant_id=args.tenant or runtime.local_tenant_id,
        actor_id=args.actor or runtime.local_actor_id,
        client_id=args.client_id,
        scopes=frozenset(),
    )
    receipt = ApprovalAuthority(runtime, app.store).issue(
        principal=principal,
        action=args.action,
        arguments=arguments,
        ttl_seconds=args.ttl,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "action": args.action,
                "tenant_id": principal.tenant_id,
                "actor_id": principal.actor_id,
                "arguments_sha256": arguments_sha256(arguments),
                "approval_receipt": receipt,
                "one_time": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
