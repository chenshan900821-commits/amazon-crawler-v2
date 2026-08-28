from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from amazon_crawler.application.preflight import configuration_report
from amazon_crawler.application.scoped_runner import run_scoped_job
from amazon_crawler.bootstrap import Application, build_application
from amazon_crawler.config import Settings
from amazon_crawler.domain.errors import CrawlerError


SERVER_INSTRUCTIONS = (
    "Use crawler_doctor before crawler_create_job or crawler_run_job. "
    "Prefer crawler_run_job when the caller wants a completed result without a "
    "pre-running worker. Never request or pass Cookie, proxy, Redis, database, or "
    "other secrets as tool arguments; the server reads them from its environment. "
    "Treat created=true as persistence evidence, not crawl completion. Verify job "
    "status and result counts. Cancellation requires confirm=true."
)

ReadOnly = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
CreatesWork = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
ControlsWork = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
CancelsWork = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

TaskInput = str | dict[str, Any]
TaskInputs = Annotated[
    list[TaskInput],
    Field(min_length=1, max_length=500, description="Authorized crawl inputs."),
]
TaskStatus = Literal[
    "pending",
    "running",
    "pause_requested",
    "paused",
    "cancel_requested",
    "cancelled",
    "succeeded",
    "partial",
    "failed",
]


def _settings(db_override: Path | None) -> Settings:
    settings = Settings.from_env()
    if db_override is None:
        return settings
    return replace(settings, db_path=db_override.expanduser().resolve())


def _task_options(
    result_sinks: list[str] | None,
    tags: list[str] | None,
    requested_fields: list[str] | None,
) -> dict[str, Any] | None:
    options: dict[str, Any] = {}
    if result_sinks is not None:
        options["result_sinks"] = result_sinks
    if tags is not None:
        options["tags"] = tags
    if requested_fields is not None:
        options["requested_fields"] = requested_fields
    return options or None


def _require_runtime_configuration(application: Application) -> None:
    report = configuration_report(application.settings)
    if report["configuration_ready"]:
        return
    safe_details = {
        "error": "runtime configuration is not ready",
        "blocking_issues": report["blocking_issues"],
    }
    raise ToolError(json.dumps(safe_details, ensure_ascii=False, sort_keys=True))


def _raise_public_tool_error(exc: Exception) -> None:
    if isinstance(exc, CrawlerError):
        raise ToolError(str(exc)) from exc
    raise ToolError("internal crawler operation failed") from exc


def create_mcp_server(
    application: Application | None = None,
    *,
    settings: Settings | None = None,
) -> MCPServer:
    """Create an MCP adapter over one crawler application instance."""

    app = application or build_application(settings)
    server = MCPServer(
        name="amazon-crawler",
        title="Amazon Crawler",
        description="Resumable Amazon crawl jobs, status, evidence, and results.",
        instructions=SERVER_INSTRUCTIONS,
        version="0.1.0",
    )

    @server.tool(title="Check crawler configuration", annotations=ReadOnly)
    def crawler_doctor() -> dict[str, Any]:
        """Check required runtime configuration without contacting or disclosing secrets."""

        return configuration_report(app.settings)

    @server.tool(title="Describe crawler capabilities", annotations=ReadOnly)
    def crawler_capabilities() -> dict[str, Any]:
        """Return supported task kinds, marketplaces, controls, and result stores."""

        return {"ok": True, **app.service.capabilities()}

    @server.tool(title="List crawl jobs", annotations=ReadOnly)
    async def crawler_list_jobs(
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        status: TaskStatus | None = None,
    ) -> dict[str, Any]:
        """List recent crawl jobs, optionally filtered by lifecycle status."""

        jobs = await asyncio.to_thread(app.store.list_jobs, limit=limit, status=status)
        return {"ok": True, "jobs": jobs}

    @server.tool(title="Get a crawl job", annotations=ReadOnly)
    async def crawler_get_job(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
    ) -> dict[str, Any]:
        """Get one job and its durable item states."""

        try:
            job = await asyncio.to_thread(app.store.get_job, job_id)
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "job": job}

    @server.tool(title="Get crawl results", annotations=ReadOnly)
    async def crawler_get_results(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """Return persisted canonical results for one job."""

        try:
            values = await asyncio.to_thread(app.store.list_results, job_id, limit=limit)
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "results": values}

    @server.tool(title="Get crawl events", annotations=ReadOnly)
    async def crawler_get_events(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """Return the durable event timeline used to explain job behavior."""

        try:
            values = await asyncio.to_thread(app.store.list_events, job_id, limit=limit)
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "events": values}

    @server.tool(title="Get result deliveries", annotations=ReadOnly)
    async def crawler_get_deliveries(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """Return outbox delivery status for configured secondary result stores."""

        try:
            values = await asyncio.to_thread(
                app.store.list_deliveries,
                job_id,
                limit=limit,
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "deliveries": values}

    @server.tool(title="Get crawler metrics", annotations=ReadOnly)
    async def crawler_metrics() -> dict[str, Any]:
        """Return local queue, result, and delivery counts without secret values."""

        return {"ok": True, "metrics": await asyncio.to_thread(app.store.metrics)}

    @server.tool(title="Create a queued crawl job", annotations=CreatesWork)
    async def crawler_create_job(
        inputs: TaskInputs,
        kind: Annotated[str, Field(min_length=1, max_length=80)] = "amazon.product",
        marketplace_id: Annotated[str | None, Field(max_length=8)] = None,
        postal_code: Annotated[str | None, Field(max_length=32)] = None,
        execution_mode: Literal["standard", "overseas", "realtime"] = "standard",
        priority: Annotated[int, Field(ge=-100, le=100)] = 0,
        max_attempts: Annotated[int | None, Field(ge=1, le=20)] = None,
        idempotency_key: Annotated[str | None, Field(max_length=200)] = None,
        result_sinks: Annotated[list[str] | None, Field(max_length=8)] = None,
        tags: Annotated[list[str] | None, Field(max_length=20)] = None,
        requested_fields: Annotated[list[str] | None, Field(max_length=50)] = None,
    ) -> dict[str, Any]:
        """Persist an idempotent job for an already-running worker; it does not wait."""

        _require_runtime_configuration(app)
        try:
            job, created = await asyncio.to_thread(
                app.service.create_job,
                inputs=inputs,
                kind=kind,
                marketplace_id=marketplace_id,
                postal_code=postal_code,
                execution_mode=execution_mode,
                priority=priority,
                max_attempts=max_attempts,
                idempotency_key=idempotency_key,
                options=_task_options(result_sinks, tags, requested_fields),
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {
            "ok": True,
            "created": created,
            "completion_evidence": False,
            "job": job,
            "next_action": "call crawler_get_job until terminal, then crawler_get_results",
        }

    @server.tool(title="Run and verify a crawl job", annotations=CreatesWork)
    async def crawler_run_job(
        inputs: TaskInputs,
        kind: Annotated[str, Field(min_length=1, max_length=80)] = "amazon.product",
        marketplace_id: Annotated[str | None, Field(max_length=8)] = None,
        postal_code: Annotated[str | None, Field(max_length=32)] = None,
        execution_mode: Literal["standard", "overseas", "realtime"] = "standard",
        priority: Annotated[int, Field(ge=-100, le=100)] = 0,
        max_attempts: Annotated[int | None, Field(ge=1, le=20)] = None,
        idempotency_key: Annotated[str | None, Field(max_length=200)] = None,
        result_sinks: Annotated[list[str] | None, Field(max_length=8)] = None,
        tags: Annotated[list[str] | None, Field(max_length=20)] = None,
        requested_fields: Annotated[list[str] | None, Field(max_length=50)] = None,
        timeout_seconds: Annotated[float, Field(ge=1, le=3600)] = 600.0,
    ) -> dict[str, Any]:
        """Create, execute, deliver, and verify one root job and its follow-up lineage."""

        _require_runtime_configuration(app)
        try:
            job, created = await asyncio.to_thread(
                app.service.create_job,
                inputs=inputs,
                kind=kind,
                marketplace_id=marketplace_id,
                postal_code=postal_code,
                execution_mode=execution_mode,
                priority=priority,
                max_attempts=max_attempts,
                idempotency_key=idempotency_key,
                options=_task_options(result_sinks, tags, requested_fields),
            )
            report = await run_scoped_job(
                app,
                job["id"],
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": report["completed"], "created": created, **report}

    @server.tool(title="Pause a crawl job", annotations=ControlsWork)
    async def crawler_pause_job(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
    ) -> dict[str, Any]:
        """Request a reversible pause for one crawl job."""

        try:
            job = await asyncio.to_thread(app.store.request_pause, job_id)
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "job": job}

    @server.tool(title="Resume a crawl job", annotations=ControlsWork)
    async def crawler_resume_job(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
    ) -> dict[str, Any]:
        """Resume one paused crawl job."""

        try:
            job = await asyncio.to_thread(app.store.resume, job_id)
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "job": job}

    @server.tool(title="Cancel a crawl job", annotations=CancelsWork)
    async def crawler_cancel_job(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Cancel one job only after the user explicitly confirms the destructive action."""

        if not confirm:
            raise ToolError("cancellation requires explicit user confirmation: confirm=true")
        try:
            job = await asyncio.to_thread(app.store.request_cancel, job_id)
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "job": job}

    @server.resource(
        "crawler://capabilities",
        title="Crawler capabilities",
        description="Static public crawler capability description.",
        mime_type="application/json",
    )
    def capabilities_resource() -> str:
        return json.dumps(app.service.capabilities(), ensure_ascii=False, sort_keys=True)

    @server.resource(
        "crawler://jobs/{job_id}",
        title="Crawl job",
        description="One durable crawl job and its item states.",
        mime_type="application/json",
    )
    def job_resource(job_id: str) -> str:
        try:
            job = app.store.get_job(job_id)
        except Exception as exc:
            _raise_public_tool_error(exc)
        return json.dumps(job, ensure_ascii=False, sort_keys=True)

    @server.resource(
        "crawler://jobs/{job_id}/results",
        title="Crawl job results",
        description="Up to 100 canonical results for one crawl job.",
        mime_type="application/json",
    )
    def results_resource(job_id: str) -> str:
        try:
            values = app.store.list_results(job_id, limit=100)
        except Exception as exc:
            _raise_public_tool_error(exc)
        return json.dumps(values, ensure_ascii=False, sort_keys=True)

    return server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amazon-crawler-mcp")
    parser.add_argument("--db", type=Path, help="override CRAWLER_DB_PATH")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/mcp")
    parser.add_argument("--json-response", action="store_true")
    parser.add_argument("--stateless-http", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    server = create_mcp_server(settings=_settings(args.db))
    try:
        if args.transport == "stdio":
            server.run("stdio")
            return
        server.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
            json_response=args.json_response,
            stateless_http=args.stateless_http,
        )
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
