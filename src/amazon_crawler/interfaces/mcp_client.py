from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

REQUIRED_SMOKE_TOOLS = {
    "crawler_doctor",
    "crawler_capabilities",
    "crawler_list_jobs",
    "crawler_get_job",
    "crawler_get_results",
    "crawler_get_events",
    "crawler_get_deliveries",
    "crawler_get_audit_events",
    "crawler_metrics",
    "crawler_create_job",
    "crawler_run_job",
    "crawler_pause_job",
    "crawler_resume_job",
    "crawler_cancel_job",
}


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, list):
        return [_model(item) for item in value]
    if isinstance(value, dict):
        return {key: _model(item) for key, item in value.items()}
    return value


def _stdio_target(args: argparse.Namespace) -> StdioServerParameters:
    server_args = [
        "-m",
        "amazon_crawler.interfaces.mcp_server",
        "--transport",
        "stdio",
    ]
    if args.db:
        server_args.extend(["--db", str(args.db.expanduser().resolve())])
    return StdioServerParameters(
        command=sys.executable,
        args=server_args,
        env=dict(os.environ),
        cwd=Path.cwd(),
    )


def _connection(client: Client) -> dict[str, Any]:
    return {
        "server_info": _model(client.server_info),
        "server_capabilities": _model(client.server_capabilities),
        "protocol_version": client.protocol_version,
        "instructions": client.instructions,
    }


def _arguments(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("--arguments must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("--arguments must be a JSON object")
    return value


def _tool_result(value: Any) -> dict[str, Any]:
    payload = {
        "is_error": bool(value.is_error),
        "structured_content": _model(value.structured_content),
    }
    if value.is_error or value.structured_content is None:
        payload["content"] = _model(value.content)
    return payload


def _safe_exception_message(exc: BaseException) -> str:
    if isinstance(exc, MCPError):
        return exc.message
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            message = _safe_exception_message(nested)
            if (
                message
                != "MCP connection or operation failed; verify the server command or URL"
            ):
                return message
    if isinstance(exc, (OSError, RuntimeError, ValueError)):
        return str(exc)
    return "MCP connection or operation failed; verify the server command or URL"


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    async with AsyncExitStack() as stack:
        if args.url:
            token = os.getenv("CRAWLER_MCP_ACCESS_TOKEN", "").strip()
            headers = {"Authorization": f"Bearer {token}"} if token else None
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(headers=headers)
            )
            transport = streamable_http_client(args.url, http_client=http_client)
            client = await stack.enter_async_context(
                Client(transport, read_timeout_seconds=args.read_timeout)
            )
        else:
            client = await stack.enter_async_context(
                Client(
                    _stdio_target(args),
                    read_timeout_seconds=args.read_timeout,
                )
            )
        connection = _connection(client)
        if args.command == "info":
            return {"ok": True, **connection}, True
        if args.command == "list-tools":
            listed = await client.list_tools()
            return {
                "ok": True,
                **connection,
                "tools": [_model(tool) for tool in listed.tools],
                "next_cursor": listed.next_cursor,
            }, True
        if args.command == "list-resources":
            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            return {
                "ok": True,
                **connection,
                "resources": [_model(item) for item in resources.resources],
                "resource_templates": [
                    _model(item) for item in templates.resource_templates
                ],
            }, True
        if args.command == "read-resource":
            result = await client.read_resource(args.uri)
            return {"ok": True, **connection, "result": _model(result)}, True
        if args.command == "call":
            result = await client.call_tool(
                args.tool_name,
                _arguments(args.arguments),
            )
            payload = {
                "ok": not result.is_error,
                **connection,
                "tool": args.tool_name,
                "result": _tool_result(result),
            }
            return payload, not result.is_error
        if args.command == "smoke":
            listed = await client.list_tools()
            tool_names = {tool.name for tool in listed.tools}
            missing = sorted(REQUIRED_SMOKE_TOOLS - tool_names)
            doctor = await client.call_tool("crawler_doctor", {})
            capabilities = await client.call_tool("crawler_capabilities", {})
            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            ok = not missing and not doctor.is_error and not capabilities.is_error
            return {
                "ok": ok,
                **connection,
                "checks": {
                    "handshake": True,
                    "listed_tool_count": len(listed.tools),
                    "missing_required_tools": missing,
                    "doctor_call_ok": not doctor.is_error,
                    "configuration_ready": (doctor.structured_content or {}).get(
                        "configuration_ready"
                    ),
                    "capabilities_call_ok": not capabilities.is_error,
                    "resource_count": len(resources.resources),
                    "resource_template_count": len(templates.resource_templates),
                },
            }, ok
    raise ValueError("unsupported command")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amazon-crawler-mcp-client")
    parser.add_argument(
        "--url",
        help="connect to a Streamable HTTP MCP URL instead of launching local stdio",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="override CRAWLER_DB_PATH for the locally launched stdio server",
    )
    parser.add_argument("--read-timeout", type=float, default=600.0)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info")
    sub.add_parser("list-tools")
    sub.add_parser("list-resources")
    resource = sub.add_parser("read-resource")
    resource.add_argument("uri")
    call = sub.add_parser("call")
    call.add_argument("tool_name")
    call.add_argument("--arguments", default="{}")
    sub.add_parser("smoke")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        payload, ok = asyncio.run(_run(args))
    except Exception as exc:
        message = _safe_exception_message(exc)
        _print(
            {
                "ok": False,
                "error": {
                    "type": exc.__class__.__name__,
                    "message": message,
                },
            }
        )
        raise SystemExit(2) from exc
    _print(payload)
    if not ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
